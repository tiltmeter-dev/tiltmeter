"""The ``tiltmeter`` command.

Exit codes, so CI can gate on drift:

* 0: no drift (info-only changes and new baselines included)
* 1: warning-level drift
* 2: breaking drift
* 3: a check could not run (network error, bad status, invalid JSON) and nothing broke
* 4: usage or configuration error

``tiltmeter fix`` reuses them: 0 when the edits were committed, discarded on
purpose or not needed, 1 when the agent left work for a human, 3 when the agent
or a git step failed, 4 for usage errors.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Coroutine, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, NoReturn

import typer
from rich.console import Console
from rich.markup import escape

from tiltmeter import __version__
from tiltmeter.config import DEFAULT_CONFIG_FILE, EXAMPLE_CONFIG, FixAgentKind
from tiltmeter.core.models import CheckResult, Severity, max_severity
from tiltmeter.errors import HarError, TiltmeterError
from tiltmeter.har import ImportResult, endpoints_from_har, load_har, render_config

if TYPE_CHECKING:
    from tiltmeter.fix.models import FixOutcome, FixPreview
    from tiltmeter.fix.runner import FixSession
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


def _decide(
    session: FixSession,
    preview: FixPreview,
    *,
    model_name: str,
    yes: bool,
    no_pr: bool,
    dry_run: bool,
    as_json: bool,
) -> FixOutcome:
    """Commit, push and open the pull request, or throw the edits away."""
    from tiltmeter.fix.git import GhCli

    if preview.ignored:
        return session.outcome("blocked", preview)
    if preview.empty:
        return session.outcome("unchanged", preview)
    if dry_run:
        return session.outcome("discarded", preview, notes=["--dry-run: nothing was committed"])
    if not yes:
        if as_json:
            note = "pass --yes to commit when printing JSON"
            return session.outcome("discarded", preview, notes=[note])
        question = "Commit to a local branch?" if no_pr else "Commit, push and open a pull request?"
        try:
            approved = typer.confirm(question, default=False)
        except typer.Abort:
            approved = False
        if not approved:
            return session.outcome("discarded", preview)
    return session.deliver(
        preview, push=not no_pr, github=None if no_pr else GhCli(), model_name=model_name
    )


@app.command()
def fix(
    check_id: Annotated[int, typer.Argument(help="Check id, as listed by `history`.")],
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
    """Let an agent adapt your code to the drift a check found, in a pull request.

    The default agent needs ANTHROPIC_API_KEY and the fix extra (pip install
    "tiltmeter\\[fix]"). --agent claude-code runs on your Claude Code login instead.
    """
    from tiltmeter.console import render_fix_outcome, render_fix_preview, render_result
    from tiltmeter.fix.agent import build_fix_agent
    from tiltmeter.fix.models import FixOutcome
    from tiltmeter.fix.runner import FixSession

    with _opened(config) as service:
        check = service.get_check(check_id)
        try:
            request = service.fix_request(check_id)
        except TiltmeterError as exc:
            _fail(str(exc))
        if check is None or not request.targets:
            note = f"check #{check_id} found no changes that code has to follow"
            outcome = FixOutcome(
                status="unchanged", check_id=check_id, endpoint_id=request.endpoint_id, notes=[note]
            )
        else:
            settings = service.config.fix
            if agent_kind is not None:
                settings = settings.model_copy(update={"agent": agent_kind})
            try:
                agent = build_fix_agent(settings)
                session = FixSession.open(
                    repo, request, branch=branch, prefix=settings.branch_prefix, base=base
                )
            except TiltmeterError as exc:
                _fail(str(exc))
            with session:
                if not as_json:
                    render_result(out, check, show_id=True)
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
                except Exception as exc:  # API, agent or git errors: no traceback for users
                    _fail(f"fix failed: {exc}", EXIT_CHECK_FAILED)
                try:
                    preview = session.review(report, ignore=settings.ignore)
                    if not as_json:
                        render_fix_preview(out, preview)
                    outcome = _decide(
                        session,
                        preview,
                        model_name=agent.model_name,
                        yes=yes,
                        no_pr=no_pr,
                        dry_run=dry_run,
                        as_json=as_json,
                    )
                except TiltmeterError as exc:
                    _fail(f"fix failed: {exc}", EXIT_CHECK_FAILED)
            if outcome.pr_url or outcome.branch:
                where = f"pull request {outcome.pr_url}" if outcome.pr_url else outcome.branch
                service.store.append_notes(check_id, [f"fix: {where}"])
    if as_json:
        _print_json(outcome.model_dump(mode="json"))
    else:
        render_fix_outcome(out, outcome)
    raise typer.Exit(fix_exit_code(outcome))


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
