"""The ``driftwatch`` command.

Exit codes, so CI can gate on drift:

* 0: no drift (info-only changes and new baselines included)
* 1: warning-level drift
* 2: breaking drift
* 3: a check could not run (network error, bad status, invalid JSON) and nothing broke
* 4: usage or configuration error
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

from driftwatch import __version__
from driftwatch.config import DEFAULT_CONFIG_FILE, EXAMPLE_CONFIG
from driftwatch.core.models import CheckResult, Severity, max_severity
from driftwatch.errors import DriftwatchError

if TYPE_CHECKING:
    from driftwatch.service import DriftService

EXIT_OK: Final = 0
EXIT_WARNING: Final = 1
EXIT_BREAKING: Final = 2
EXIT_CHECK_FAILED: Final = 3
EXIT_USAGE: Final = 4

STATE_IGNORE_LINE: Final = ".driftwatch/"

app = typer.Typer(
    name="driftwatch",
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
        "--config", "-c", envvar="DRIFTWATCH_CONFIG", help="Path to the configuration file."
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
    err.print(f"[bold red]error:[/] {message}", markup=True, highlight=False)
    raise typer.Exit(code)


def _service(config: Path) -> DriftService:
    from driftwatch.service import DriftService  # deferred: keeps --help fast

    try:
        return DriftService.from_config_file(config)
    except DriftwatchError as exc:
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
    except DriftwatchError as exc:
        _fail(str(exc))


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _emit(results: Sequence[CheckResult], as_json: bool) -> None:
    if as_json:
        _print_json([result.model_dump(mode="json") for result in results])
    else:
        from driftwatch.console import render_results

        render_results(out, results)


def _version(value: bool) -> None:
    if value:
        out.print(f"driftwatch {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """Catch API schema drift before your AI agents do.

    driftwatch calls the APIs you declare in driftwatch.yaml, learns the shape of
    their JSON responses, and tells you (or your agents) when it changes.
    """


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Where to write the config.")] = Path(
        DEFAULT_CONFIG_FILE
    ),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter driftwatch.yaml."""
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    out.print(f"[green]✓[/] wrote {path}")
    gitignore = path.parent / ".gitignore"
    if gitignore.exists() or (path.parent / ".git").exists():
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if STATE_IGNORE_LINE not in existing.splitlines():
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with gitignore.open("a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{STATE_IGNORE_LINE}\n")
            out.print(f"[green]✓[/] added {STATE_IGNORE_LINE} to {gitignore}")
    out.print("Next: edit the endpoints, then run [bold]driftwatch check[/].")


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
                    from driftwatch.console import render_results

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
        out.print("No checks yet. Run [bold]driftwatch check[/].")
        return
    from driftwatch.console import history_table

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
        from driftwatch.console import render_result

        render_result(out, result, show_id=True)


@app.command()
def schema(
    endpoint_id: Annotated[str, typer.Argument(help="Endpoint id from the config.")],
    config: ConfigOption = Path(DEFAULT_CONFIG_FILE),
) -> None:
    """Print the JSON Schema driftwatch has learned for an endpoint."""
    with _opened(config) as service:
        try:
            report = service.schema(endpoint_id)
        except DriftwatchError as exc:
            _fail(str(exc))
    if report.json_schema is None:
        _fail(f"no baseline for {endpoint_id} yet; run `driftwatch check` first", EXIT_CHECK_FAILED)
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
        from driftwatch.console import render_result

        render_result(out, result, show_id=True)


@app.command()
def serve(config: ConfigOption = Path(DEFAULT_CONFIG_FILE)) -> None:
    """Run the MCP server on stdio, for Claude Code, Cursor and other MCP clients."""
    from driftwatch.mcp_server.server import run_stdio

    try:
        run_stdio(config)
    except DriftwatchError as exc:
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
