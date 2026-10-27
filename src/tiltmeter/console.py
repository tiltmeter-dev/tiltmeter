"""Rich rendering of check results, fixes and change notices for the terminal.

Text that comes from third parties, such as a notice's summary, is always passed as
``Text`` objects, never as markup, so it cannot style or rewrite the output.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from tiltmeter.core.models import Change, CheckResult, Severity

if TYPE_CHECKING:
    from tiltmeter.fix.models import AppliedNotice, FixOutcome, FixPreview
    from tiltmeter.notices.models import (
        ApiChangeNotice,
        FeedEntry,
        FeedReport,
        Impact,
        NoticeEndpoint,
        SpecInfo,
    )
    from tiltmeter.notices.scan import ScanFinding, ScanMatch, ScanMiss, ScanReport

MAX_DIFF_LINES: Final = 400
MAX_SCAN_LINES: Final = 5

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
    tests = preview.tests
    if tests is not None:
        again = f" after {tests.rounds} rounds" if tests.rounds > 1 else ""
        if tests.status == "passed":
            console.print(Text(f"✓ {tests.command} passed{again}", style="green"))
        elif tests.status == "failed":
            console.print(Text(f"✗ {tests.command} fails{again}", style="red"))
            tail = tests.output.strip().splitlines()[-12:]
            console.print(Padding(Text("\n".join(tail), style="dim"), (0, 0, 0, 2)))
        else:
            console.print(Text(f"tests skipped: {tests.reason}", style="yellow"))
    for unused in preview.unused:
        console.print(
            Text.assemble(
                ("! possibly unused code: ", "yellow"),
                (unused.path, "bold"),
                (
                    " — nothing else in the repository imports or names it; last changed "
                    f"{unused.last_changed[:10]} ({_ago(unused.age_days)})",
                    "yellow",
                ),
            )
        )


def _ago(days: int) -> str:
    if days < 1:
        return "today"
    if days < 60:
        return f"{days} day{'' if days == 1 else 's'} ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


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
        case "committed" if outcome.held_back:
            line = Text(
                f"! committed to branch {outcome.branch}; the pull request was held back",
                style="yellow",
            )
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


# -- notices ----------------------------------------------------------------------


def notice_line(
    notice: ApiChangeNotice, *, verified: bool, spec: SpecInfo | None = None, icon: str = "●"
) -> Text:
    if spec is not None:
        total = spec.endpoints_changed
        meta = [
            "spec diff",
            f"{spec.old_version or '?'} → {spec.new_version or '?'}",
            f"{len(notice.endpoints)} of {total} endpoint{'' if total == 1 else 's'}",
        ]
    else:
        meta = ["signed" if verified else "unsigned", f"published {notice.published_at:%Y-%m-%d}"]
    if notice.effective_at is not None:
        meta.append(f"effective {notice.effective_at:%Y-%m-%d}")
    return Text.assemble(
        (f"{icon} ", "blue"),
        (notice.provider, "bold"),
        f"  {notice.id}",
        (f"  {' · '.join(meta)}", "dim"),
    )


def notice_changes_table(endpoint: NoticeEndpoint) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False)
    table.add_column("kind", no_wrap=True, style="dim")
    table.add_column("path", overflow="fold")
    table.add_column("change", overflow="fold")
    for change in endpoint.changes:
        if change.before or change.after:
            detail = Text(f"{change.before or '—'} → {change.after or '—'}")
        else:
            detail = Text("")
        if change.renamed_to:
            detail.append(f"{', ' if detail else ''}renamed to {change.renamed_to}", style="italic")
        if change.moved_to:
            moved = f"{change.moved_to.method} {change.moved_to.url}"
            detail.append(f"{', ' if detail else ''}moved to {moved}", style="italic")
        if change.replaced_by:
            detail.append(f"{', ' if detail else ''}use {change.replaced_by}", style="italic")
        if change.note:
            detail.append(f"  {change.note}", style="dim")
        table.add_row(change.kind.value, Text(change.path, style="bold"), detail)
    return table


def render_notice(
    console: Console, notice: ApiChangeNotice, *, verified: bool, spec: SpecInfo | None = None
) -> None:
    """A notice: who announced what, when, and the changes per endpoint."""
    console.print(notice_line(notice, verified=verified, spec=spec))
    console.print(Padding(Text(notice.summary), (0, 0, 0, 4)))
    for endpoint in notice.endpoints:
        console.print(Padding(Text(f"{endpoint.method} {endpoint.url}"), (0, 0, 0, 4)))
        console.print(Padding(notice_changes_table(endpoint), (0, 0, 0, 6)))


def impact_line(impact: Impact) -> Text:
    if not impact.affected:
        return Text(f"not affected: {impact.reason}", style="dim")
    line = Text(f"affects this repository: {impact.reason}", style="yellow")
    if impact.files:
        shown = ", ".join(impact.files[:5])
        more = f" (+{len(impact.files) - 5} more)" if len(impact.files) > 5 else ""
        line.append(f"\n{shown}{more}", style="dim")
    return line


def _entry_line(entry: FeedEntry) -> Text:
    name = entry.id or "(no id)"
    match entry.state:
        case "rejected":
            return Text.assemble(("✗ ", "red"), name, (f"  rejected: {entry.problem}", "red"))
        case "skipped":
            return Text.assemble(("- ", "dim"), name, (f"  skipped: {entry.problem}", "dim"))
        case "done" if entry.record is not None:
            record = entry.record
            detail = record.status + (f" {record.detail}" if record.detail else "")
            return Text.assemble(("✓ ", "green"), name, (f"  {detail}", "dim"))
        case _:
            return Text(name)


def render_feed_reports(
    console: Console, reports: Sequence[FeedReport], *, show_all: bool = False
) -> None:
    """Each subscribed feed, its new and rejected notices, and a count of the rest."""
    for report in reports:
        console.print(Text.assemble((report.subscription, "bold"), (f"  {report.feed}", "dim")))
        if report.error:
            console.print(Padding(Text(f"✗ {report.error}", style="red"), (0, 0, 0, 2)))
        if report.note:
            console.print(Padding(Text(report.note, style="dim"), (0, 0, 0, 2)))
        if report.error and not report.entries:
            continue
        counts: dict[str, int] = {}
        for entry in report.entries:
            counts[entry.state] = counts.get(entry.state, 0) + 1
            if entry.state == "new" and entry.notice is not None:
                line = notice_line(entry.notice, verified=True, spec=entry.spec)
                console.print(Padding(line, (0, 0, 0, 2)))
                console.print(Padding(Text(entry.notice.summary), (0, 0, 0, 6)))
                if entry.impact is not None:
                    console.print(Padding(impact_line(entry.impact), (0, 0, 0, 6)))
            elif entry.state == "rejected" or show_all:
                console.print(Padding(_entry_line(entry), (0, 0, 0, 2)))
        words = {
            "new": "new",
            "done": "handled before",
            "skipped": "skipped",
            "rejected": "rejected",
        }
        parts = [f"{counts[state]} {word}" for state, word in words.items() if counts.get(state)]
        if parts or not report.note:
            summary = Text(" · ".join(parts) or "no notices", style="dim")
            console.print(Padding(summary, (0, 0, 0, 2)))


def render_feed_problems(console: Console, reports: Sequence[FeedReport]) -> None:
    """Only what went wrong: feeds that could not be read and notices that were rejected."""
    for report in reports:
        if report.error:
            console.print(
                Text.assemble(
                    ("✗ ", "red"), (report.subscription, "bold"), (f"  {report.error}", "red")
                )
            )
        for entry in report.entries:
            if entry.state == "rejected":
                console.print(
                    Text.assemble(
                        ("✗ ", "red"),
                        (report.subscription, "bold"),
                        f"  {entry.id or '(no id)'}",
                        (f"  rejected: {entry.problem}", "red"),
                    )
                )


def applied_line(applied: AppliedNotice) -> Text:
    name = Text.assemble((applied.provider, "bold"), f"  {applied.notice_id}")
    match applied.status:
        case "unaffected":
            return Text.assemble(("- ", "dim"), name, (f"  not affected: {applied.detail}", "dim"))
        case "exists":
            return Text.assemble(("✓ ", "green"), name, (f"  {applied.detail}", "dim"))
        case "failed":
            return Text.assemble(("✗ ", "red"), name, (f"  failed: {applied.detail}", "red"))
        case _:
            detail = f"  {applied.status}" + (f": {applied.detail}" if applied.detail else "")
            return Text.assemble(("● ", "blue"), name, (detail, "dim"))


# -- scan -------------------------------------------------------------------------


def _when(finding: ScanFinding, now: datetime) -> str:
    if finding.effective_at is None:
        return f"published {finding.published_at:%Y-%m-%d}"
    verb = "took" if finding.effective_at <= now else "takes"
    return f"{verb} effect {finding.effective_at:%Y-%m-%d}"


def _origin(finding: ScanFinding) -> str:
    if finding.drafted:
        return "unsigned: drafted by Tiltmeter from the provider's public documentation"
    return "signed by the provider" if finding.signed else "unsigned"


def scan_match_text(match: ScanMatch) -> Text:
    """Where a line is and what changes for it, then the line itself."""
    text = Text.assemble(
        (f"{match.file}:{match.line}", "bold"), "  ", (f"{match.term}: {match.detail}", "yellow")
    )
    if match.possibly_unused:
        text.append("  possibly unused: nothing else imports or names this file", style="dim")
    text.append("\n    ")
    text.append(match.text, style="dim")
    return text


def render_scan_finding(console: Console, finding: ScanFinding, now: datetime) -> None:
    """One change the folder uses: a line on what and when, a few places, and the fix."""
    ahead = finding.effective_at is not None and finding.effective_at > now
    icon, style = ("!", "yellow") if ahead else ("✗", "red")
    console.print(
        Text.assemble(
            (f"{icon} ", style),
            (finding.provider, "bold"),
            "  ",
            finding.headline,
            (f"  {_when(finding, now)}", "dim"),
        )
    )
    console.print(Padding(Text(f"{finding.id} · {_origin(finding)}", style="dim"), (0, 0, 0, 4)))
    shown = finding.matches[:MAX_SCAN_LINES]
    for match in shown:
        console.print(Padding(scan_match_text(match), (0, 0, 0, 4)))
    rest = finding.count - len(shown)
    if rest > 0:
        files = f", {finding.files} files in all" if finding.files > 1 else ""
        more = Text(f"… and {rest} more line{'' if rest == 1 else 's'}{files}", style="dim")
        console.print(Padding(more, (0, 0, 0, 4)))
    if finding.tests:
        also = Text(f"also in tests: {_places(finding.tests)}", style="dim")
        console.print(Padding(also, (0, 0, 0, 4)))
    if finding.fix_command is not None:
        # Not wrapped, so the command copies and pastes whole even with a long path.
        command = Text.assemble("    ", ("fix: ", "dim"), (finding.fix_command, "bold"))
        console.print(command, soft_wrap=True)
    else:
        command = Text(
            "fix: only signed notices are read from URLs; save the catalog to a file and pass "
            "that to --catalog",
            style="dim",
        )
        console.print(Padding(command, (0, 0, 0, 4)))


def _places(matches: Sequence[ScanMatch], limit: int = 3) -> str:
    shown = ", ".join(f"{match.file}:{match.line}" for match in matches[:limit])
    return shown + (f" and {len(matches) - limit} more" if len(matches) > limit else "")


def _miss_text(miss: ScanMiss) -> Text:
    text = Text.assemble(
        ("- ", "dim"),
        (miss.provider, "bold"),
        f"  {miss.id}",
        (f"  not affected: {miss.reason}", "dim"),
    )
    if miss.tests:
        text.append(f"\n    used only in tests, which do not count: {_places(miss.tests)}", "dim")
    if miss.docs:
        text.append(f"\n    mentioned in docs, which do not count: {_places(miss.docs)}", "dim")
    return text


def scan_summary(report: ScanReport, *, show_all: bool = False) -> Text:
    if not report.affected:
        entries = f"{report.entries} catalog entr{'y' if report.entries == 1 else 'ies'}"
        text = Text(
            f"No known API change affects {report.path}: {entries} checked against "
            f"{report.files} files.",
            style="green",
        )
        apart = sum(1 for miss in report.unaffected if miss.tests or miss.docs)
        if apart and not show_all:
            text.append(
                f" {apart} {'is' if apart == 1 else 'are'} mentioned in tests or docs only: "
                "--all shows where.",
                style="dim",
            )
        return text
    lines = sum(finding.count for finding in report.affected)
    files = {match.file for finding in report.affected for match in finding.matches}
    unused = {m.file for finding in report.affected for m in finding.matches if m.possibly_unused}
    count = len(report.affected)
    words = (
        f"{count} of {report.entries} known API changes affect{'s' if count == 1 else ''} "
        f"{report.path}: {lines} line{'' if lines == 1 else 's'} in {len(files)} "
        f"file{'' if len(files) == 1 else 's'}"
    )
    if unused:
        words += f", {len(unused)} file{'' if len(unused) == 1 else 's'} possibly unused"
    return Text(f"{words}.", style="bold")


def render_scan(
    console: Console, report: ScanReport, *, show_all: bool = False, now: datetime | None = None
) -> None:
    """Each catalog entry the folder uses, where, and the command that fixes it, then a
    summary line. With ``show_all``, also the entries that do not affect it, and why."""
    source = "the catalog bundled with Tiltmeter" if report.bundled else report.catalog
    if report.error:
        console.print(Text(f"✗ cannot read {source}: {report.error}", style="red"))
        return
    moment = now or datetime.now(UTC)
    for finding in report.affected:
        render_scan_finding(console, finding, moment)
        console.print()
    if show_all and report.unaffected:
        for miss in report.unaffected:
            console.print(_miss_text(miss))
        console.print()
    for problem in report.rejected:
        name = problem.id or "(no id)"
        console.print(Text.assemble(("✗ ", "red"), name, (f"  rejected: {problem.problem}", "red")))
    console.print(scan_summary(report, show_all=show_all), soft_wrap=True)
    if report.affected and not report.git:
        note = f"note: {report.path} is not a git checkout, and tiltmeter fix needs one"
        console.print(Text(note, style="dim"))
    if report.bundled:
        console.print(
            Text(
                "The catalog's entries are drafted by Tiltmeter from each provider's public "
                "documentation; no provider signed or endorsed them.",
                style="dim",
            )
        )
