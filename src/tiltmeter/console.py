"""Rich rendering of check results for the terminal."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from tiltmeter.core.models import Change, CheckResult, Severity

if TYPE_CHECKING:
    from tiltmeter.fix.models import FixOutcome, FixPreview

MAX_DIFF_LINES: Final = 400

SEVERITY_STYLE: Final = {
    Severity.BREAKING: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


def _outcome(result: CheckResult) -> tuple[str, str, str]:
    """(icon, style, words) describing how a check went."""
    count = len(result.changes)
    plural = "" if count == 1 else "s"
    match result.outcome:
        case "error":
            return "✗", "red", f"error: {result.error}"
        case "drift":
            severity = result.max_severity or Severity.WARNING
            icon = "✗" if severity is Severity.BREAKING else "!"
            return (
                icon,
                SEVERITY_STYLE[severity],
                f"{severity.value.upper()} · {count} change{plural}",
            )
        case "changed":
            return "✓", "green", f"{count} info change{plural}"
        case "baseline":
            return "●", "blue", "baseline recorded (first check)"
        case _:
            return "✓", "green", "no drift"


def result_line(result: CheckResult, *, show_id: bool = False) -> Text:
    icon, style, words = _outcome(result)
    meta = []
    if result.status_code is not None:
        meta.append(str(result.status_code))
    if result.latency_ms is not None:
        meta.append(f"{result.latency_ms:.0f} ms")
    line = Text.assemble((f"{icon} ", style), (result.endpoint_id, "bold"))
    if show_id and result.check_id is not None:
        line.append(f"  #{result.check_id}", style="dim")
    if meta:
        line.append(f"  {' · '.join(meta)}", style="dim")
    line.append(f"  {words}", style=style)
    return line


def changes_table(changes: Sequence[Change]) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False)
    table.add_column("severity", no_wrap=True)
    table.add_column("kind", no_wrap=True, style="dim")
    table.add_column("path", overflow="fold")
    table.add_column("change", overflow="fold")
    for change in changes:
        before = change.before or "(absent)"
        after = change.after or "(absent)"
        detail = Text(f"{before} → {after}")
        if change.hint:
            detail.append(f"  {change.hint}", style="italic")
        table.add_row(
            Text(change.severity.value, style=SEVERITY_STYLE[change.severity]),
            change.kind.value,
            Text(change.path, style="bold"),
            detail,
        )
    return table


def explanation_panel(result: CheckResult) -> Panel | None:
    explanation = result.explanation
    if explanation is None:
        return None
    body = Text()
    body.append(explanation.summary + "\n", style="bold")
    for label, value in (
        ("Cause", explanation.likely_cause),
        ("Impact", explanation.impact),
        ("Action", explanation.suggested_action),
    ):
        body.append(f"{label}: ", style="bold")
        body.append(value + "\n")
    footer = f"confidence {explanation.confidence}"
    if result.explanation_model:
        footer += f" · {result.explanation_model}"
    body.append(footer, style="dim")
    return Panel(body, title="explanation", title_align="left", border_style="dim", expand=False)


def render_result(console: Console, result: CheckResult, *, show_id: bool = False) -> None:
    console.print(result_line(result, show_id=show_id))
    if result.changes:
        console.print(Padding(changes_table(result.changes), (0, 0, 0, 4)))
    panel = explanation_panel(result)
    if panel is not None:
        console.print(Padding(panel, (0, 0, 0, 4)))
    for note in result.notes:
        console.print(Padding(Text(f"note: {note}", style="dim"), (0, 0, 0, 4)))


def summary_line(results: Sequence[CheckResult]) -> Text:
    counts = {"breaking": 0, "warning": 0, "error": 0, "baseline": 0}
    for result in results:
        if result.outcome == "error":
            counts["error"] += 1
        elif result.outcome == "baseline":
            counts["baseline"] += 1
        elif result.max_severity is Severity.BREAKING:
            counts["breaking"] += 1
        elif result.max_severity is Severity.WARNING:
            counts["warning"] += 1
    total = len(results)
    parts = [f"{total} endpoint{'' if total == 1 else 's'} checked"]
    parts += [f"{value} {name}" for name, value in counts.items() if value]
    return Text(" · ".join(parts), style="dim")


def render_results(console: Console, results: Sequence[CheckResult]) -> None:
    for result in results:
        render_result(console, result)
    console.print(summary_line(results))


def history_table(checks: Sequence[CheckResult]) -> Table:
    table = Table(box=None, header_style="bold dim", padding=(0, 2, 0, 0))
    for column in ("check", "endpoint", "checked at", "status", "latency", "result"):
        table.add_column(column, no_wrap=column != "result")
    for check in checks:
        _, style, words = _outcome(check)
        table.add_row(
            f"#{check.check_id}",
            check.endpoint_id,
            check.checked_at,
            str(check.status_code) if check.status_code is not None else "-",
            f"{check.latency_ms:.0f} ms" if check.latency_ms is not None else "-",
            Text(words, style=style),
        )
    return table


def render_fix_preview(console: Console, preview: FixPreview) -> None:
    """The agent's edits as a coloured diff, with its own summary underneath."""
    if not preview.empty:
        count = len(preview.files)
        console.print(
            Text.assemble(
                (f"{count} file{'' if count == 1 else 's'} changed", "bold"),
                "  ",
                (f"+{preview.additions}", "green"),
                " ",
                (f"-{preview.deletions}", "red"),
            )
        )
        lines = preview.diff.splitlines()
        shown = "\n".join(lines[:MAX_DIFF_LINES])
        console.print(Syntax(shown, "diff", theme="ansi_dark", background_color="default"))
        if len(lines) > MAX_DIFF_LINES:
            console.print(Text(f"... {len(lines) - MAX_DIFF_LINES} more diff lines", style="dim"))
    report = preview.report
    body = Text(report.summary + "\n")
    if report.unresolved:
        body.append("Left for a human:\n", style="bold yellow")
        for item in report.unresolved:
            body.append(f"  - {item}\n", style="yellow")
    body.append(f"confidence {report.confidence}", style="dim")
    console.print(Panel(body, title="agent", title_align="left", border_style="dim", expand=False))


def render_fix_outcome(console: Console, outcome: FixOutcome) -> None:
    """One line on how the fix ended, then whatever is left to do by hand."""
    preview = outcome.preview
    match outcome.status:
        case "opened":
            line = Text.assemble(
                ("✓ ", "green"), ("opened ", "green"), (outcome.pr_url or "", "bold")
            )
        case "pushed":
            line = Text.assemble(("✓ ", "green"), f"pushed branch {outcome.branch}")
        case "committed":
            line = Text.assemble(("✓ ", "green"), f"committed to branch {outcome.branch}")
        case "discarded":
            line = Text("discarded the edits; nothing was committed", style="dim")
        case "unchanged" if preview is None:
            line = Text("nothing to fix", style="green")
        case "unchanged":
            line = Text("the agent changed nothing", style="yellow")
        case _:
            ignored = ", ".join(preview.ignored) if preview else ""
            line = Text(f"✗ the agent touched protected files ({ignored}); discarded", style="red")
    console.print(line)
    for note in outcome.notes:
        console.print(Padding(Text(f"note: {note}", style="dim"), (0, 0, 0, 2)))
    if outcome.error:
        console.print(Padding(Text(f"error: {outcome.error}", style="red"), (0, 0, 0, 2)))
    if outcome.commands:
        console.print(Padding(Text("to finish by hand:", style="dim"), (0, 0, 0, 2)))
        for command in outcome.commands:
            console.print(Padding(Text(command, style="bold"), (0, 0, 0, 4)))
