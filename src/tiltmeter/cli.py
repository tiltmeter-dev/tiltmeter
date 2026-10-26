"""The ``tiltmeter`` command.

Exit codes, so CI can gate on drift:

* 0: no drift (info-only changes and new baselines included)
* 1: warning-level drift
* 2: breaking drift
* 3: a check could not run (network error, bad status, invalid JSON) and nothing broke
* 4: usage or configuration error

``tiltmeter fix`` and ``tiltmeter notices apply`` reuse them: 0 when the edits
were committed, discarded on purpose or not needed, 1 when the agent left work
for a human, 3 when the agent, a git step or a feed failed, 4 for usage errors.
``tiltmeter notices check`` exits 1 when there are new notices to act on.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from collections.abc import Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from tiltmeter import __version__
from tiltmeter.config import DEFAULT_CONFIG_FILE, EXAMPLE_CONFIG, FixAgentKind
from tiltmeter.core.models import CheckResult, NoticeStatus, Severity, max_severity
from tiltmeter.core.timeutil import utcnow
from tiltmeter.errors import FixError, HarError, NoticeError, TiltmeterError
from tiltmeter.har import ImportResult, endpoints_from_har, load_har, render_config

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tiltmeter.config import FixConfig
    from tiltmeter.fix.agent import FixAgent
    from tiltmeter.fix.git import Git, GitHub
    from tiltmeter.fix.models import (
        AppliedNotice,
        FixOutcome,
        FixPreview,
        FixReport,
        FixRequest,
        TestRun,
    )
    from tiltmeter.fix.runner import FixSession
    from tiltmeter.notices.impact import Candidate, RepoIndex
    from tiltmeter.notices.models import ApiChangeNotice, FeedEntry, FeedReport, Impact, SdkName
    from tiltmeter.notices.reader import LoadedNotice
    from tiltmeter.notices.spec import Chooser
    from tiltmeter.service import DriftService

EXIT_OK: Final = 0
EXIT_WARNING: Final = 1
EXIT_BREAKING: Final = 2
EXIT_CHECK_FAILED: Final = 3
EXIT_USAGE: Final = 4

STATE_IGNORE_LINE: Final = ".tiltmeter/"

app = typer.Typer(
    name="tiltmeter",
    help="Catch API schema drift before your AI agents do.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config", "-c", envvar="TILTMETER_CONFIG", help="Path to the configuration file."
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of tables.")]
EndpointsOption = Annotated[
    list[str] | None,
    typer.Option("--endpoint", "-e", help="Only these endpoint ids (repeatable)."),
]
NoLLMOption = Annotated[bool, typer.Option("--no-llm", help="Skip Claude explanations.")]
NoNotifyOption = Annotated[bool, typer.Option("--no-notify", help="Do not send notifications.")]


def exit_code_for(results: Sequence[CheckResult]) -> int:
    """Breaking beats a failed check, which beats a warning."""
    worst = max_severity(result.max_severity for result in results)
    if worst is Severity.BREAKING:
        return EXIT_BREAKING
    if any(result.error for result in results):
        return EXIT_CHECK_FAILED
    if worst is Severity.WARNING:
        return EXIT_WARNING
    return EXIT_OK


def _fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    err.print(f"[bold red]error:[/] {escape(message)}", markup=True, highlight=False)
    raise typer.Exit(code)


def _service(config: Path) -> DriftService:
    from tiltmeter.service import DriftService  # deferred: keeps --help fast

    try:
        return DriftService.from_config_file(config)
    except TiltmeterError as exc:
        _fail(str(exc))


@contextmanager
def _opened(config: Path) -> Iterator[DriftService]:
    service = _service(config)
    try:
        yield service
    finally:
        asyncio.run(service.aclose())


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coroutine)
    except TiltmeterError as exc:
        _fail(str(exc))


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _emit(results: Sequence[CheckResult], as_json: bool) -> None:
    if as_json:
        _print_json([result.model_dump(mode="json") for result in results])
    else:
        from tiltmeter.console import render_results

        render_results(out, results)


def _version(value: bool) -> None:
    if value:
        out.print(f"tiltmeter {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """Catch API schema drift before your AI agents do.

    Tiltmeter calls the APIs you declare in tiltmeter.yaml, learns the shape of
    their JSON responses, and tells you (or your agents) when it changes.
    """


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Where to write the config.")] = Path(
        DEFAULT_CONFIG_FILE
    ),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
    from_har: Annotated[
        Path | None,
        typer.Option(
            "--from-har",
            exists=True,
            dir_okay=False,
            help="Build the endpoints from a HAR file exported from the browser's Network tab.",
        ),
    ] = None,
    host: Annotated[
        list[str] | None,
        typer.Option("--host", help="With --from-har: only this host and its subdomains."),
    ] = None,
    include_writes: Annotated[
        bool,
        typer.Option(
            "--include-writes",
            help="With --from-har: keep POST, PUT, PATCH and DELETE requests too.",
        ),
    ] = False,
) -> None:
    """Write a starter tiltmeter.yaml, or build one from a browser HAR export."""
    if from_har is None and (host or include_writes):
        _fail("--host and --include-writes need --from-har")
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite it)")
    imported = None
    text = EXAMPLE_CONFIG
    if from_har is not None:
        imported = _import_har(from_har, host or [], include_writes)
        text = render_config(imported, from_har.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    out.print(f"[green]✓[/] wrote {escape(str(path))}")
    if imported is not None:
        _report_import(imported)
    _ignore_state_dir(path)
    if imported is None:
        out.print("Next: edit the endpoints, then run [bold]tiltmeter check[/].")
        return
    if imported.variables:
        out.print("Credentials were replaced by placeholders. Set these before checking:")
        for name in imported.variables:
            out.print(f"  {name}")
    out.print("Next: review the endpoints, then run [bold]tiltmeter check[/].")


def _import_har(source: Path, hosts: Sequence[str], include_writes: bool) -> ImportResult:
    try:
        imported = endpoints_from_har(load_har(source), hosts, include_writes=include_writes)
    except HarError as exc:
        _fail(str(exc))
    if not imported.endpoints:
        hint = "drop --host or " if hosts else ""
        _fail(
            f"no JSON API calls found in {source} ({hint}record the HAR while the page loads"
            " the data you care about)"
        )
    return imported


def _report_import(imported: ImportResult) -> None:
    """Counts and host names only: never a header, parameter or body value."""
    count = len(imported.endpoints)
    out.print(f"  {count} endpoint{'' if count == 1 else 's'} from {imported.requests} requests")
    hosts = ", ".join(f"{name} ({n})" for name, n in imported.hosts.items())
    out.print(f"  hosts: {escape(hosts)}")
    if imported.other_hosts:
        out.print(f"  left out {imported.other_hosts} requests to other hosts")
    if imported.writes:
        out.print(
            f"  left out {imported.writes} POST/PUT/PATCH/DELETE requests, which a monitor"
            " would replay on every check (keep them with --include-writes)"
        )


def _ignore_state_dir(path: Path) -> None:
    """Add the state folder to the .gitignore next to the config, inside a repository."""
    gitignore = path.parent / ".gitignore"
    if gitignore.exists() or (path.parent / ".git").exists():
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if STATE_IGNORE_LINE not in existing.splitlines():
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with gitignore.open("a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{STATE_IGNORE_LINE}\n")
            out.print(f"[green]✓[/] added {STATE_IGNORE_LINE} to {gitignore}")


@app.command()
def check(
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    endpoint: EndpointsOption = None,
    as_json: JsonOption = False,
    no_llm: NoLLMOption = False,
    no_notify: NoNotifyOption = False,
) -> None:
    """Check endpoints once. The exit code tells CI how bad the drift is."""

    async def run() -> list[CheckResult]:
        async with _service(config) as service:
            return await service.check(endpoint, explain=not no_llm, notify=not no_notify)

    results = _run(run())
    _emit(results, as_json)
    raise typer.Exit(exit_code_for(results))


@app.command()
def watch(
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    interval: Annotated[
        float, typer.Option("--interval", "-i", min=5, help="Seconds between rounds.")
    ] = 300.0,
    endpoint: EndpointsOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one JSON line per check (JSON Lines).")
    ] = False,
    no_llm: NoLLMOption = False,
    no_notify: NoNotifyOption = False,
) -> None:
    """Check endpoints forever, every --interval seconds. Stop with Ctrl+C."""

    async def run() -> None:
        async with _service(config) as service:
            while True:
                results = await service.check(endpoint, explain=not no_llm, notify=not no_notify)
                if as_json:
                    for result in results:
                        sys.stdout.write(result.model_dump_json() + "\n")
                    sys.stdout.flush()
                else:
                    from tiltmeter.console import render_results

                    render_results(out, results)
                await _pause(interval)

    try:
        _run(run())
    except KeyboardInterrupt:
        err.print("stopped")


@app.command()
def history(
    endpoint_id: Annotated[str | None, typer.Argument(help="Only this endpoint.")] = None,
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=10_000)] = 20,
    as_json: JsonOption = False,
) -> None:
    """List recent checks, newest first."""
    with _opened(config) as service:
        checks = service.history(endpoint_id, limit=limit)
    if as_json:
        _print_json([check.model_dump(mode="json") for check in checks])
        return
    if not checks:
        out.print("No checks yet. Run [bold]tiltmeter check[/].")
        return
    from tiltmeter.console import history_table

    out.print(history_table(checks))


@app.command()
def show(
    check_id: Annotated[int, typer.Argument(help="Check id, as listed by `history`.")],
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    as_json: JsonOption = False,
) -> None:
    """Show one check: its changes and explanation."""
    with _opened(config) as service:
        result = service.get_check(check_id)
    if result is None:
        _fail(f"no check with id {check_id}")
    if as_json:
        _print_json(result.model_dump(mode="json"))
    else:
        from tiltmeter.console import render_result

        render_result(out, result, show_id=True)


@app.command()
def schema(
    endpoint_id: Annotated[str, typer.Argument(help="Endpoint id from the config.")],
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
) -> None:
    """Print the JSON Schema Tiltmeter has learned for an endpoint."""
    with _opened(config) as service:
        try:
            report = service.schema(endpoint_id)
        except TiltmeterError as exc:
            _fail(str(exc))
    if report.json_schema is None:
        _fail(f"no baseline for {endpoint_id} yet; run `tiltmeter check` first", EXIT_CHECK_FAILED)
    _print_json(report.json_schema)


@app.command()
def explain(
    check_id: Annotated[int, typer.Argument(help="Check id, as listed by `history`.")],
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    as_json: JsonOption = False,
) -> None:
    """Ask Claude to explain the changes a past check found (needs ANTHROPIC_API_KEY)."""

    async def run() -> CheckResult:
        async with _service(config) as service:
            return await service.explain_check(check_id)

    try:
        result = _run(run())
    except typer.Exit:
        raise
    except Exception as exc:  # API errors from the model provider: no traceback for users
        _fail(f"explanation failed: {exc}", EXIT_CHECK_FAILED)
    if as_json:
        _print_json(result.model_dump(mode="json"))
    else:
        from tiltmeter.console import render_result

        render_result(out, result, show_id=True)


def fix_exit_code(outcome: FixOutcome) -> int:
    """A failed step beats work left for a human, which beats success."""
    if outcome.error:
        return EXIT_CHECK_FAILED
    if outcome.status == "blocked":
        return EXIT_WARNING
    if outcome.status == "unchanged" and outcome.preview and outcome.preview.report.unresolved:
        return EXIT_WARNING
    return EXIT_OK


@dataclass(frozen=True, slots=True)
class _Delivery:
    """Where a fix happens and what to do with the agent's edits."""

    repo: Path
    branch: str | None = None
    base: str | None = None
    yes: bool = False
    no_pr: bool = False
    dry_run: bool = False
    as_json: bool = False


class _FixFailed(Exception):
    """A fix that could not start or finish. The message is for the user."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _decide(
    session: FixSession, preview: FixPreview, *, model_name: str, delivery: _Delivery
) -> FixOutcome:
    """Commit, push and open the pull request, or throw the edits away."""
    from tiltmeter.fix.git import GhCli

    if preview.ignored:
        return session.outcome("blocked", preview)
    if preview.empty:
        return session.outcome("unchanged", preview)
    if delivery.dry_run:
        return session.outcome("discarded", preview, notes=["--dry-run: nothing was committed"])
    if not delivery.yes:
        if delivery.as_json:
            note = "pass --yes to commit when printing JSON"
            return session.outcome("discarded", preview, notes=[note])
        question = (
            "Commit to a local branch?"
            if delivery.no_pr
            else "Commit, push and open a pull request?"
        )
        try:
            approved = typer.confirm(question, default=False)
        except typer.Abort:
            approved = False
        if not approved:
            return session.outcome("discarded", preview)
    return session.deliver(
        preview,
        push=not delivery.no_pr,
        github=None if delivery.no_pr else GhCli(),
        model_name=model_name,
    )


def _fix_settings(settings: FixConfig, agent_kind: FixAgentKind | None) -> FixConfig:
    return settings if agent_kind is None else settings.model_copy(update={"agent": agent_kind})


def _build_agent(settings: FixConfig) -> FixAgent:
    from tiltmeter.fix.agent import build_fix_agent

    try:
        return build_fix_agent(settings)
    except TiltmeterError as exc:
        _fail(str(exc))


def _attempt_fix(
    request: FixRequest,
    agent: FixAgent,
    settings: FixConfig,
    delivery: _Delivery,
    intro: Callable[[], None],
) -> FixOutcome:
    """Let the agent work on a fresh worktree, show the diff and deliver it as asked."""
    from tiltmeter.console import render_fix_preview
    from tiltmeter.fix.runner import FixSession

    if settings.test_command:
        request = request.model_copy(update={"test_command": shlex.join(settings.test_command)})
    try:
        session = FixSession.open(
            delivery.repo,
            request,
            branch=delivery.branch,
            prefix=settings.branch_prefix,
            base=delivery.base,
        )
    except TiltmeterError as exc:
        raise _FixFailed(str(exc), EXIT_USAGE) from exc
    with session:
        if not delivery.as_json:
            intro()
            start = f"{session.base} at " if session.base else ""
            out.print(
                f"Working on a new branch, [bold]{session.branch}[/], "
                f"from {start}{session.repo.head()}."
            )
            if session.repo.has_uncommitted_changes():
                out.print("[dim]note: uncommitted changes in your checkout are left out[/]")
        try:
            with err.status(f"{agent.model_name} is adapting the code..."):
                report = asyncio.run(agent.run(request, session.root))
            tests = None
            if settings.test_command:
                report, tests = _test_and_retry(session, request, report, agent, settings)
        except Exception as exc:  # API, agent or git errors: no traceback for users
            raise _FixFailed(f"fix failed: {exc}", EXIT_CHECK_FAILED) from exc
        try:
            preview = session.review(report, ignore=settings.ignore, tests=tests)
            if not delivery.as_json:
                render_fix_preview(out, preview)
            return _decide(session, preview, model_name=agent.model_name, delivery=delivery)
        except TiltmeterError as exc:
            raise _FixFailed(f"fix failed: {exc}", EXIT_CHECK_FAILED) from exc


def _test_and_retry(
    session: FixSession,
    request: FixRequest,
    report: FixReport,
    agent: FixAgent,
    settings: FixConfig,
) -> tuple[FixReport, TestRun]:
    """Run the tests on the agent's edits; on a failure, show it the output and let it
    edit again, up to ``fix.test_rounds`` more times."""
    from tiltmeter.fix.verify import run_tests

    command = settings.test_command or []
    rounds = 1
    while True:
        with err.status(f"running {command[0]} on the edits..."):
            tests = run_tests(
                session.worktree,
                command,
                timeout=settings.test_timeout,
                env_names=settings.test_env,
                network=settings.test_network,
                home=session.scratch / "home",
                project=session.repo.root,
            )
        tests = tests.model_copy(update={"rounds": rounds})
        if tests.status != "failed" or rounds > settings.test_rounds:
            return report, tests
        rounds += 1
        retry = request.model_copy(update={"test_failure": tests, "previous_report": report})
        with err.status(f"the tests fail; {agent.model_name} is editing again..."):
            report = asyncio.run(agent.run(retry, session.root))


def _fix_check(
    check_id: int, config: Path, agent_kind: FixAgentKind | None, delivery: _Delivery
) -> FixOutcome:
    from tiltmeter.console import render_result
    from tiltmeter.fix.models import FixOutcome

    with _opened(config) as service:
        check = service.get_check(check_id)
        try:
            request = service.fix_request(check_id)
        except TiltmeterError as exc:
            _fail(str(exc))
        if check is None or not request.targets:
            note = f"check #{check_id} found no changes that code has to follow"
            return FixOutcome(
                status="unchanged", check_id=check_id, endpoint_id=request.subject, notes=[note]
            )
        settings = _fix_settings(service.config.fix, agent_kind)
        agent = _build_agent(settings)
        try:
            outcome = _attempt_fix(
                request,
                agent,
                settings,
                delivery,
                partial(render_result, out, check, show_id=True),
            )
        except _FixFailed as exc:
            _fail(str(exc), exc.code)
        if outcome.pr_url or outcome.branch:
            where = f"pull request {outcome.pr_url}" if outcome.pr_url else outcome.branch
            service.store.append_notes(check_id, [f"fix: {where}"])
    return outcome


_RECORDED: Final[dict[str, NoticeStatus]] = {
    "opened": "opened",
    "pushed": "pushed",
    "committed": "committed",
    "unchanged": "unchanged",
    "blocked": "blocked",
}


def _notice_status(outcome: FixOutcome, delivery: _Delivery) -> NoticeStatus | None:
    """What to remember about a notice's fix; None when it stays new (discarded).

    A commit that was meant to be pushed and was not counts as failed, so it is tried
    again: on a CI runner the commit is gone once the job ends.
    """
    if outcome.status == "committed" and not delivery.no_pr:
        return "failed"
    return _RECORDED.get(outcome.status)


def _detail(outcome: FixOutcome) -> str | None:
    if outcome.status == "committed" and outcome.error:
        return f"committed to {outcome.branch} but not pushed: {outcome.error}"
    return outcome.pr_url or outcome.branch or outcome.error


def _record_fix(
    service: DriftService, notice: ApiChangeNotice, outcome: FixOutcome, delivery: _Delivery
) -> None:
    status = _notice_status(outcome, delivery)
    if status is not None:
        service.record_notice(notice, status, _detail(outcome))


async def _load_notice(source: str, *, allow_unsigned: bool) -> LoadedNotice:
    import httpx

    from tiltmeter.notices.reader import NoticeReader

    async with httpx.AsyncClient() as http:
        return await NoticeReader(http).load(source, allow_unsigned=allow_unsigned)


def _repo_files(repo: Path) -> tuple[Path, list[str]]:
    from tiltmeter.fix.git import Git

    try:
        git = Git.open(repo)
        return git.root, git.tracked_files()
    except TiltmeterError as exc:
        _fail(str(exc))


def _repo_index(repo: Path, ignore: Sequence[str]) -> tuple[Path, RepoIndex]:
    from tiltmeter.notices.impact import RepoIndex

    root, files = _repo_files(repo)
    return root, RepoIndex.read(root, files, ignore=ignore)


def _chooser(index: RepoIndex) -> Chooser:
    """Picks the endpoints of a spec change that this repository uses."""
    from tiltmeter.notices.impact import choose_endpoints, fields_near

    def choose(
        candidates: Sequence[Candidate], hosts: Sequence[str], sdks: Sequence[SdkName]
    ) -> list[int]:
        names = [name for candidate in candidates for name in candidate.fields]
        near = fields_near(index, hosts, sdks, names)
        return choose_endpoints(index, candidates, near=near, hosts=hosts, sdks=sdks)

    return choose


def _impact(
    index: RepoIndex, entry: FeedEntry, notice: ApiChangeNotice, sdks: Sequence[SdkName]
) -> Impact:
    """Whether the repository uses what a notice changes, and opts in to a new version."""
    from tiltmeter.notices.impact import assess
    from tiltmeter.notices.models import Impact

    spec = entry.spec
    if spec is not None and spec.new_api_version and not index.mentions(spec.new_version or ""):
        reason = (
            f"{notice.provider} published API version {spec.new_version}; clients pinned to "
            f"{spec.old_version} keep their responses until they upgrade"
        )
        return Impact(affected=False, reason=reason)
    return assess(index, notice, sdks=sdks)


def _fix_notice(
    source: str,
    config: Path,
    *,
    allow_unsigned: bool,
    agent_kind: FixAgentKind | None,
    delivery: _Delivery,
) -> FixOutcome:
    from tiltmeter.config import FixConfig
    from tiltmeter.console import render_notice
    from tiltmeter.fix.models import FixOutcome
    from tiltmeter.fix.request import build_notice_request
    from tiltmeter.notices.impact import assess

    service = _service(config) if config.is_file() else None
    try:
        if service is None and not delivery.as_json:
            err.print(
                f"[dim]note: no {escape(str(config))}: the default fix settings apply and the "
                "outcome is not recorded[/]"
            )
        settings = _fix_settings(service.config.fix if service else FixConfig(), agent_kind)
        loaded = _run(_load_notice(source, allow_unsigned=allow_unsigned))
        notice = loaded.notice
        _, index = _repo_index(delivery.repo, settings.ignore)
        impact = assess(index, notice)
        request = build_notice_request(
            notice,
            location=loaded.location,
            verified=loaded.verified,
            candidate_files=impact.files,
            endpoints=impact.endpoints or None,
            read=impact.fields,
        )
        if not request.targets:
            return FixOutcome(
                status="unchanged",
                notice_id=notice.id,
                provider=notice.provider,
                notes=["the notice has no changes that code has to follow"],
            )
        agent = _build_agent(settings)

        def intro() -> None:
            render_notice(out, notice, verified=loaded.verified)
            if not impact.affected:
                out.print(
                    Text(f"note: {impact.reason}; the agent searches on its own", style="dim")
                )

        try:
            outcome = _attempt_fix(request, agent, settings, delivery, intro)
        except _FixFailed as exc:
            _fail(str(exc), exc.code)
        # Only a verified notice is recorded: an unsigned file must not stand in for the
        # provider's notice with the same id.
        if service is not None and loaded.verified:
            _record_fix(service, notice, outcome, delivery)
        return outcome
    finally:
        if service is not None:
            asyncio.run(service.aclose())


@app.command()
def fix(
    check_id: Annotated[
        int | None, typer.Argument(help="Check id, as listed by `history`.")
    ] = None,
    notice: Annotated[
        str | None,
        typer.Option(
            "--notice", help="Fix what a provider's change notice announces: a file or https URL."
        ),
    ] = None,
    allow_unsigned: Annotated[
        bool,
        typer.Option(
            "--allow-unsigned", help="With --notice FILE: accept a notice with no signature."
        ),
    ] = False,
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="The git repository whose code reads the API.")
    ] = Path("."),
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    branch: Annotated[str | None, typer.Option("--branch", help="Name of the new branch.")] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", help="Branch the pull request targets (default: the current one)."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Commit and open the pull request without asking.")
    ] = False,
    no_pr: Annotated[
        bool, typer.Option("--no-pr", help="Commit to a local branch only; push nothing.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the agent's edits, then discard them.")
    ] = False,
    agent_kind: Annotated[
        FixAgentKind | None,
        typer.Option("--agent", help="The agent that edits the code (default: fix.agent)."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Let an agent adapt your code to an API change, in a pull request.

    The change is the drift a check found (`tiltmeter fix 12`) or what a provider
    announced in a change notice (`tiltmeter fix --notice URL`). The default agent
    needs ANTHROPIC_API_KEY and the fix extra (pip install "tiltmeter\\[fix]").
    --agent claude-code or --agent codex runs on your Claude Code or Codex login instead.
    """
    delivery = _Delivery(
        repo=repo, branch=branch, base=base, yes=yes, no_pr=no_pr, dry_run=dry_run, as_json=as_json
    )
    if notice is None:
        if check_id is None:
            _fail("pass a check id, or --notice with a notice file or URL")
        if allow_unsigned:
            _fail("--allow-unsigned goes with --notice")
        outcome = _fix_check(check_id, config, agent_kind, delivery)
    else:
        if check_id is not None:
            _fail("pass a check id or --notice, not both")
        outcome = _fix_notice(
            notice, config, allow_unsigned=allow_unsigned, agent_kind=agent_kind, delivery=delivery
        )
    if as_json:
        _print_json(outcome.model_dump(mode="json"))
    else:
        from tiltmeter.console import render_fix_outcome

        render_fix_outcome(out, outcome)
    raise typer.Exit(fix_exit_code(outcome))


# -- provider notices, as a subscriber --------------------------------------------

notices_app = typer.Typer(
    name="notices",
    help="Follow the change notices of the API providers you subscribe to.",
    no_args_is_help=True,
)
app.add_typer(notices_app)


def notices_exit_code(reports: Sequence[FeedReport]) -> int:
    """A feed that could not be read beats new notices, which beat nothing new."""
    if any(report.error for report in reports):
        return EXIT_CHECK_FAILED
    for report in reports:
        for entry in report.entries:
            if entry.state == "new" and (entry.impact is None or entry.impact.affected):
                return EXIT_WARNING
    return EXIT_OK


def _new_notices(reports: Sequence[FeedReport]) -> list[tuple[FeedEntry, ApiChangeNotice]]:
    """The new notices of every feed, oldest first."""
    pairs = [
        (entry, entry.notice)
        for report in reports
        for entry in report.entries
        if entry.state == "new" and entry.notice is not None
    ]
    return sorted(pairs, key=lambda pair: pair[1].published_at)


@notices_app.command("check")
def notices_check(
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo", "-r", help="Also tell whether each new notice affects this repository."
        ),
    ] = None,
    show_all: Annotated[
        bool, typer.Option("--all", help="Also list the notices handled before or skipped.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """List new notices and spec changes, with every signature checked.

    Nothing is recorded and nothing runs. Exit code 0: nothing new; 1: new notices
    (that affect --repo, when given); 3: a feed or spec could not be read.
    """
    from tiltmeter.service import by_severity

    with _opened(config) as service:
        index = _repo_index(repo, service.config.fix.ignore)[1] if repo is not None else None
        followed = _run(service.read_notices(chooser=_chooser(index) if index else by_severity))
        sdks = _subscription_sdks(service)
    reports = followed.reports
    if index is not None:
        for entry, notice in _new_notices(reports):
            entry.impact = _impact(index, entry, notice, sdks.get(entry.feed, []))
    if as_json:
        _print_json([report.model_dump(mode="json") for report in reports])
    else:
        from tiltmeter.console import render_feed_reports

        render_feed_reports(out, reports, show_all=show_all)
    raise typer.Exit(notices_exit_code(reports))


def _subscription_sdks(service: DriftService) -> dict[str, list[SdkName]]:
    return {sub.feed_url: list(sub.sdks) for sub in service.config.subscriptions}


def applied_exit_code(reports: Sequence[FeedReport], applied: Sequence[AppliedNotice]) -> int:
    """A failure beats work left for a human, which beats success."""
    codes = [EXIT_CHECK_FAILED if report.error else EXIT_OK for report in reports]
    for item in applied:
        if item.status == "failed":
            codes.append(EXIT_CHECK_FAILED)
        elif item.outcome is not None:
            codes.append(fix_exit_code(item.outcome))
    if EXIT_CHECK_FAILED in codes:
        return EXIT_CHECK_FAILED
    return max(codes, default=EXIT_OK)


@notices_app.command("apply")
def notices_apply(
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="The git repository whose code reads the APIs.")
    ] = Path("."),
    base: Annotated[
        str | None,
        typer.Option("--base", help="Branch the pull requests target (default: the current one)."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Commit and open the pull requests without asking.")
    ] = False,
    no_pr: Annotated[
        bool, typer.Option("--no-pr", help="Commit to local branches only; push nothing.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would change, then discard it; record nothing."),
    ] = False,
    agent_kind: Annotated[
        FixAgentKind | None,
        typer.Option("--agent", help="The agent that edits the code (default: fix.agent)."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Fix every new notice or spec change that affects the repository, one pull request each.

    A plain text search decides first whether the repository uses what a notice
    changes; when it does not, the notice is recorded as not affected and no agent
    runs. Each notice is handled once: the outcome is kept in the state folder, along
    with the last version of every followed spec.
    """
    from tiltmeter.console import (
        applied_line,
        render_feed_problems,
        render_fix_outcome,
        render_notice,
    )
    from tiltmeter.fix.git import GhCli, Git
    from tiltmeter.fix.models import AppliedNotice
    from tiltmeter.fix.request import build_notice_request
    from tiltmeter.fix.runner import default_branch

    delivery = _Delivery(
        repo=repo, base=base, yes=yes, no_pr=no_pr, dry_run=dry_run, as_json=as_json
    )
    applied: list[AppliedNotice] = []
    with _opened(config) as service:
        settings = _fix_settings(service.config.fix, agent_kind)
        root, index = _repo_index(repo, settings.ignore)
        followed = _run(service.read_notices(chooser=_chooser(index)))
        reports = followed.reports
        sdks = _subscription_sdks(service)
        if not dry_run:
            # Before any fix: a change found in a spec is frozen as it was found.
            service.remember_subscriptions()
            for spec in followed.specs:
                service.keep_spec(spec)
        git = Git(root)
        if not as_json:
            render_feed_problems(out, reports)
        agent: FixAgent | None = None

        def finish(item: AppliedNotice, notice: ApiChangeNotice) -> None:
            applied.append(item)
            if not dry_run and item.status != "discarded":
                service.record_notice(notice, item.status, item.detail)
            if not as_json and item.outcome is None:
                out.print(applied_line(item))

        for entry, notice in _new_notices(reports):
            ref = partial(AppliedNotice, provider=notice.provider, notice_id=notice.id)
            impact = _impact(index, entry, notice, sdks.get(entry.feed, []))
            if not impact.affected:
                finish(
                    ref(status="unaffected", detail=impact.reason, impact=impact),
                    notice,
                )
                continue
            request = build_notice_request(
                notice,
                location=entry.feed if entry.spec else f"{entry.feed}#{notice.id}",
                verified=entry.source == "notice",
                candidate_files=impact.files,
                endpoints=impact.endpoints or None,
                spec=entry.spec,
                read=impact.fields,
            )
            branch = default_branch(request, settings.branch_prefix)
            remote = git.remote()
            on_remote = remote is not None and git.remote_branch_exists(remote, branch)
            here = git.branch_exists(branch)
            if on_remote or here:
                # An earlier run got this far: finish its work instead of redoing it.
                where = f"already on {remote}" if on_remote else "already here"
                if dry_run:
                    detail = f"branch {branch} is {where}"
                    finish(ref(status="exists", detail=detail, impact=impact), notice)
                elif no_pr:
                    status: NoticeStatus = "committed" if here else "exists"
                    finish(ref(status=status, detail=f"branch {branch}", impact=impact), notice)
                else:
                    pushed = (
                        _push_branch(ref, git, remote, branch, impact) if not on_remote else None
                    )
                    finish(
                        pushed
                        or _ensure_pull_request(
                            ref,
                            GhCli(),
                            git,
                            remote or "origin",
                            branch,
                            base,
                            impact,
                            confirm=partial(
                                _confirm_pull_request, branch, yes=yes, as_json=as_json
                            ),
                        ),
                        notice,
                    )
                continue
            if agent is None:
                agent = _build_agent(settings)
            try:
                outcome = _attempt_fix(
                    request,
                    agent,
                    settings,
                    delivery,
                    partial(
                        render_notice,
                        out,
                        notice,
                        verified=entry.source == "notice",
                        spec=entry.spec,
                    ),
                )
            except _FixFailed as exc:
                finish(ref(status="failed", detail=str(exc), impact=impact), notice)
                continue
            if not as_json:
                render_fix_outcome(out, outcome)
            finish(
                ref(
                    status=_notice_status(outcome, delivery) or "discarded",
                    detail=_detail(outcome),
                    impact=impact,
                    outcome=outcome,
                ),
                notice,
            )
    if as_json:
        _print_json(
            {
                "feeds": [report.model_dump(mode="json") for report in reports],
                "applied": [item.model_dump(mode="json") for item in applied],
            }
        )
    elif not applied:
        out.print("No new notices.")
    raise typer.Exit(applied_exit_code(reports, applied))


REOPENED: Final = "Opened by Tiltmeter for a branch an earlier run pushed."


def _confirm_pull_request(branch: str, *, yes: bool, as_json: bool) -> bool:
    if yes:
        return True
    if as_json:
        return False
    try:
        return typer.confirm(f"Open the pull request for branch {branch}?", default=False)
    except typer.Abort:
        return False


def _push_branch(
    ref: Callable[..., AppliedNotice], git: Git, remote: str | None, branch: str, impact: Impact
) -> AppliedNotice | None:
    """Push a branch an earlier run committed but could not push; None when it worked."""
    try:
        if remote is None:
            raise FixError("the repository has no remote to push to")
        git.push(remote, branch)
    except TiltmeterError as exc:
        detail = f"branch {branch} is committed here, but could not be pushed: {exc}"
        return ref(status="failed", detail=detail, impact=impact)
    return None


def _ensure_pull_request(
    ref: Callable[..., AppliedNotice],
    github: GitHub,
    git: Git,
    remote: str,
    branch: str,
    base: str | None,
    impact: Impact,
    *,
    confirm: Callable[[], bool],
) -> AppliedNotice:
    """The pull request of a branch already on the remote, opened from its commit if needed.

    The commit is fetched first: a fresh CI checkout has the branch on the remote only.
    """
    problem = github.problem(git.root)
    try:
        if problem is not None:
            raise FixError(problem)
        url = github.find_pull_request(cwd=git.root, head=branch)
        if url is None:
            if not confirm():
                detail = f"branch {branch} is pushed; pass --yes to open its pull request"
                return ref(status="discarded", detail=detail, impact=impact)
            git.run("fetch", "--quiet", remote, f"refs/heads/{branch}")
            message = git.run("log", "-1", "--format=%B", "FETCH_HEAD").strip()
            title, _, body = message.partition("\n")
            url = github.create_pull_request(
                cwd=git.root,
                head=branch,
                base=base or git.current_branch(),
                title=title.strip() or "Adapt to an API change",
                body=f"{body.strip()}\n\n---\n{REOPENED}\n",
            )
    except TiltmeterError as exc:
        detail = f"branch {branch} is pushed, but its pull request could not be opened: {exc}"
        return ref(status="failed", detail=detail, impact=impact)
    return ref(status="opened", detail=url, impact=impact)


# -- provider notices, as a provider ----------------------------------------------

notice_app = typer.Typer(
    name="notice",
    help="Write, sign and publish API change notices, as an API provider.",
    no_args_is_help=True,
)
app.add_typer(notice_app)


def _moment(text: str) -> datetime:
    """A date (midnight UTC) or an ISO 8601 time (UTC when it has no offset)."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        _fail(f"expected a date such as 2026-12-01 or an ISO 8601 time, got {text!r}")
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _read_notice_file(path: Path) -> tuple[dict[str, Any], ApiChangeNotice]:
    from tiltmeter.notices.reader import parse_notice, read_json_file

    try:
        raw = read_json_file(path)
        return raw, parse_notice(raw)
    except TiltmeterError as exc:
        _fail(str(exc))


def _write_output(data: Any, output: Path | None, *, force: bool = True) -> None:
    from tiltmeter.notices.create import write_json

    if output is None:
        _print_json(data)
        return
    if output.exists() and not force:
        _fail(f"{output} already exists (use --force to overwrite it)")
    write_json(output, data)


@notice_app.command("create")
def notice_create(
    from_check: Annotated[
        int | None,
        typer.Option("--from-check", help="A check of your own API that found the change."),
    ] = None,
    from_openapi: Annotated[
        tuple[Path, Path] | None,
        typer.Option(
            "--from-openapi", metavar="OLD NEW", help="The old and the new spec, YAML or JSON."
        ),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="With --from-check: the URL your customers call."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="With --from-openapi: the URL the paths start from."),
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Your domain (default: the API's host).")
    ] = None,
    notice_id: Annotated[
        str | None, typer.Option("--id", help="The notice id (default: <date>-<resource>).")
    ] = None,
    summary: Annotated[
        str | None, typer.Option("--summary", help="One or two sentences for your customers.")
    ] = None,
    effective: Annotated[
        str | None,
        typer.Option("--effective", help="When the old responses stop: a date or ISO time."),
    ] = None,
    sdk: Annotated[
        list[str] | None,
        typer.Option(
            "--sdk", help="An SDK release that follows the change, as pypi:name@3.0.0 (repeatable)."
        ),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write here instead of stdout.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite --output.")] = False,
    if_changed: Annotated[
        bool,
        typer.Option(
            "--if-changed",
            help="With --from-openapi: when no response changed, write nothing and exit 0.",
        ),
    ] = False,
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
) -> None:
    """Draft a notice from a check or from two versions of an OpenAPI spec.

    The draft is unsigned. Review the summary, add a note where a new format matters,
    then run `tiltmeter notice sign`.
    """
    from tiltmeter.notices import create

    if from_check is not None and from_openapi is not None:
        _fail("pass --from-check or --from-openapi, not both")
    if url is not None and from_check is None:
        _fail("--url goes with --from-check")
    if base_url is not None and from_openapi is None:
        _fail("--base-url goes with --from-openapi")
    try:
        sdks = [create.parse_sdk(text) for text in sdk or []]
    except (TiltmeterError, ValueError) as exc:
        _fail(str(exc))
    notes: list[str] = []
    try:
        if from_openapi is not None:
            old, new = (_load_spec(path) for path in from_openapi)
            try:
                drafted = create.from_openapi(old, new, base=base_url)
            except create.NoChanges:
                if not if_changed:
                    raise
                err.print(Text("no response changed: nothing to announce", style="dim"))
                raise typer.Exit(EXIT_OK) from None
            endpoints = drafted.endpoints
            if drafted.omitted_endpoints:
                notes.append(
                    f"{drafted.omitted_endpoints} more changed endpoints are left out: a "
                    "notice holds 50; announce them in a second one"
                )
            if drafted.omitted_changes:
                notes.append(f"{drafted.omitted_changes} changes are left out past the limits")
            if unread := create.unresolved_note(drafted.unresolved):
                notes.append(unread)
        elif from_check is not None:
            with _opened(config) as service:
                check = service.get_check(from_check)
                if check is None:
                    _fail(f"no check with id {from_check}")
                endpoint = service.config.endpoint(check.endpoint_id)
            endpoints = [create.from_check(check, endpoint, url=url)]
        else:
            _fail("pass --from-check or --from-openapi")
        notice = create.build_notice(
            endpoints,
            provider=provider,
            notice_id=notice_id,
            summary=summary,
            published_at=utcnow(),
            effective_at=_moment(effective) if effective else None,
            sdks=sdks,
        )
    except TiltmeterError as exc:
        _fail(str(exc))
    _write_output(notice, output, force=force)
    changes = sum(len(endpoint["changes"]) for endpoint in notice["endpoints"])
    err.print(
        Text(
            f"✓ drafted notice {notice['id']} with {changes} change{'' if changes == 1 else 's'}",
            style="green",
        )
    )
    for note in notes:
        err.print(Text(f"note: {note}", style="dim"))
    target = str(output) if output else "NOTICE.json"
    err.print(
        Text(f"Next: review it, then run tiltmeter notice sign {target} --key KEY", style="dim")
    )


def _load_spec(path: Path) -> dict[str, Any]:
    from tiltmeter.notices.spec import parse_spec

    try:
        data = path.read_bytes()
    except OSError as exc:
        _fail(f"cannot read {path}: {exc.strerror}")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(f"{path} is not UTF-8 text")
    try:
        return parse_spec(data)
    except NoticeError as exc:
        _fail(f"{path}: {exc}")


@notice_app.command("keygen")
def notice_keygen(
    key: Annotated[
        Path, typer.Argument(help="Where to write the private key. Keep it secret, out of git.")
    ],
    public: Annotated[
        Path,
        typer.Option(
            "--public",
            help="The key file to serve at https://<your domain>/.well-known/api-change-key.",
        ),
    ] = Path(".well-known/api-change-key"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite the private key.")] = False,
) -> None:
    """Create a signing key pair: a private key, and the public key file to serve."""
    from tiltmeter.notices.signing import generate_key, key_line, write_private_key

    if key.exists() and not force:
        _fail(f"{key} already exists (use --force to overwrite it)")
    private = generate_key()
    write_private_key(key, private)
    line = key_line(private.public_key())
    existing = public.read_text(encoding="utf-8") if public.is_file() else ""
    if line not in existing.splitlines():
        public.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        public.write_text(f"{existing}{prefix}{line}\n", encoding="utf-8")
    out.print(f"[green]✓[/] wrote the private key to {escape(str(key))} (readable by you only)")
    out.print(f"[green]✓[/] added the public key to {escape(str(public))}:")
    out.print(f"  {escape(line)}")
    out.print(
        "Serve that file at https://<your domain>/.well-known/api-change-key, "
        "then sign notices with [bold]tiltmeter notice sign NOTICE.json --key "
        f"{escape(str(key))}[/]."
    )


KeyOption = Annotated[
    str | None,
    typer.Option(
        "--key",
        "-k",
        envvar="TILTMETER_SIGNING_KEY",
        show_envvar=False,
        help="The private key: a PEM file, or the PEM itself (TILTMETER_SIGNING_KEY).",
    ),
]


def _signing_key(value: str | None) -> Ed25519PrivateKey:
    from tiltmeter.notices.signing import load_signing_key

    if not value:
        _fail("pass --key, or set TILTMETER_SIGNING_KEY")
    try:
        return load_signing_key(value)
    except TiltmeterError as exc:
        _fail(str(exc))


@notice_app.command("sign")
def notice_sign(
    notice_files: Annotated[list[Path], typer.Argument(help="The notices to sign, each in place.")],
    key: KeyOption = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="With one notice: write the signed one here instead."),
    ] = None,
) -> None:
    """Sign notices with Ed25519, replacing any earlier signature.

    Customers check them against https://<provider>/.well-known/api-change-key.
    """
    from tiltmeter.notices.models import default_key_url
    from tiltmeter.notices.reader import parse_notice
    from tiltmeter.notices.signing import key_line, sign

    if output is not None and len(notice_files) != 1:
        _fail("--output goes with a single notice")
    private = _signing_key(key)
    for notice_file in notice_files:
        raw, notice = _read_notice_file(notice_file)
        try:
            signed = sign(raw, private, key_url=default_key_url(notice.provider))
            parse_notice(signed)
        except TiltmeterError as exc:
            _fail(f"{notice_file}: {exc}")
        _write_output(signed, output or notice_file)
        url = escape(signed["signature"]["key"])
        out.print(f"[green]✓[/] signed {escape(notice.id)} for {url}")
    out.print(f"  the key file there must hold: {escape(key_line(private.public_key()))}")


@notice_app.command("publish")
def notice_publish(
    notice_files: Annotated[list[Path], typer.Argument(help="The notices to publish.")],
    feed: Annotated[
        Path,
        typer.Option(
            "--feed",
            help="The feed file, served at https://<your domain>/.well-known/api-changes.json.",
        ),
    ] = Path(".well-known/api-changes.json"),
    key: Annotated[
        str | None,
        typer.Option(
            "--key",
            "-k",
            envvar="TILTMETER_SIGNING_KEY",
            show_envvar=False,
            help="Sign the notices into the feed with this key: a PEM file, or the PEM itself.",
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Build the feed from these notices only."),
    ] = False,
) -> None:
    """Add signed notices to your feed, a static file any web host can serve.

    With --key, the notices are signed on the way in, so the files themselves can stay
    unsigned drafts. When the key file (api-change-key) sits next to the feed, every
    signature is checked against it first.
    """
    from tiltmeter.notices.create import add_to_feed, write_json
    from tiltmeter.notices.models import FEED_SPEC, default_key_url
    from tiltmeter.notices.reader import parse_notice, read_json_file
    from tiltmeter.notices.signing import parse_key_file, sign, verify

    private = _signing_key(key) if key is not None else None
    key_file = feed.parent / "api-change-key"
    keys = None
    if key_file.is_file():
        try:
            keys = parse_key_file(key_file.read_text(encoding="utf-8"))
        except NoticeError as exc:
            _fail(f"{key_file}: {exc}")
    current = None
    if feed.exists() and not replace:
        try:
            current = read_json_file(feed)
            if not isinstance(current, dict) or current.get("spec") != FEED_SPEC:
                raise NoticeError(f"{feed} is not an api-change-feed/v1 document")
            for item in current.get("notices", []):
                parse_notice(item)
        except TiltmeterError as exc:
            _fail(str(exc))
    published = []
    for notice_file in notice_files:
        raw, notice = _read_notice_file(notice_file)
        if private is not None:
            raw = sign(raw, private, key_url=default_key_url(notice.provider))
            notice = parse_notice(raw)
        if notice.signature is None:
            _fail(f"{notice_file} is not signed; run `tiltmeter notice sign` or pass --key")
        if keys is not None and not verify(raw, notice.signature.value, keys):
            _fail(f"the signature of {notice_file} does not match the keys in {key_file}")
        current = add_to_feed(current, raw)
        published.append(notice)
    if current is None:
        current = {"spec": FEED_SPEC, "notices": []}
    write_json(feed, current)
    count = len(current["notices"])
    checked = (
        f"signatures checked against {key_file}"
        if keys is not None
        else f"signatures not checked: no {key_file.name} next to the feed"
    )
    for notice in published:
        out.print(f"[green]✓[/] published {escape(notice.id)}")
    out.print(
        f"{escape(str(feed))} holds {count} notice{'' if count == 1 else 's'} ({escape(checked)})."
    )
    providers = sorted({notice.provider for notice in published})
    for provider in providers:
        out.print(f"Serve it at https://{escape(provider)}/.well-known/api-changes.json.")


@notice_app.command("verify")
def notice_verify(
    source: Annotated[str, typer.Argument(help="A notice file or https URL.")],
    key_file: Annotated[
        Path | None,
        typer.Option("--key-file", help="Check against this key file instead of fetching it."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Validate a notice and check its signature against the provider's published key.

    Exit code 0: signed and verified; 1: valid but unsigned; 3: invalid, or the
    signature does not match.
    """
    import httpx

    from tiltmeter.notices.reader import NoticeReader
    from tiltmeter.notices.signing import parse_key_file

    async def run() -> LoadedNotice:
        async with httpx.AsyncClient() as http:
            reader = NoticeReader(http)
            keys = None
            if key_file is not None:
                try:
                    keys = parse_key_file(key_file.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise NoticeError(f"cannot read {key_file}: {exc.strerror}") from exc
            return await reader.check(await reader.raw(source), source, keys=keys)

    try:
        loaded = asyncio.run(run())
    except TiltmeterError as exc:
        if as_json:
            _print_json({"valid": False, "verified": False, "error": str(exc)})
        else:
            err.print(Text(f"✗ {exc}", style="red"))
        raise typer.Exit(EXIT_CHECK_FAILED) from None
    if as_json:
        _print_json(
            {
                "valid": True,
                "verified": loaded.verified,
                "notice": loaded.notice.model_dump(mode="json", exclude_defaults=True),
            }
        )
    else:
        from tiltmeter.console import render_notice

        render_notice(out, loaded.notice, verified=loaded.verified)
        if loaded.verified and loaded.notice.signature is not None:
            out.print(
                Text(f"✓ signature verified against {loaded.notice.signature.key}", style="green")
            )
        else:
            out.print(Text("! valid, but not signed", style="yellow"))
    raise typer.Exit(EXIT_OK if loaded.verified else EXIT_WARNING)


@notice_app.command("schema")
def notice_schema(
    feed: Annotated[bool, typer.Option("--feed", help="The feed's schema instead.")] = False,
) -> None:
    """Print the JSON Schema of a notice (api-change-notice/v1) or of a feed."""
    from tiltmeter.notices.models import feed_json_schema, notice_json_schema

    _print_json(feed_json_schema() if feed else notice_json_schema())


@app.command()
def serve(config: ConfigOption = Path(DEFAULT_CONFIG_FILE)) -> None:
    """Run the MCP server on stdio, for Claude Code, Cursor and other MCP clients."""
    from tiltmeter.mcp_server.server import run_stdio

    try:
        run_stdio(config)
    except TiltmeterError as exc:
        _fail(str(exc))


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point. Maps usage errors to exit code 4 so they never look like drift."""
    try:
        result = app(args=list(argv) if argv is not None else None, standalone_mode=False)
    except typer.TyperException as exc:
        show_error = getattr(exc, "show", None)
        if callable(show_error):
            show_error()
        else:
            err.print(str(exc))
        no_arguments = type(exc).__name__ == "NoArgsIsHelpError"
        sys.exit(EXIT_OK if no_arguments else EXIT_USAGE)
    except typer.Abort:
        err.print("aborted")
        sys.exit(130)
    sys.exit(result if isinstance(result, int) else EXIT_OK)
