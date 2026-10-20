"""Rich rendering of check results for the terminal."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from driftwatch.core.models import Change, CheckResult, Severity

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
