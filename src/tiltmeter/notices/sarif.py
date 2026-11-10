"""A scan as SARIF 2.1.0, the format GitHub code scanning and most CI systems read.

Each catalog entry the folder uses becomes a rule, and each line that counts a result:
an error once the change is in effect, a warning while it is still ahead, a note when it
is further away than ``--within`` allows. Places in tests and docs are left out, as they
never make a folder affected.

The summary, the headline and the line's detail come from a notice, which is a third
party's text. A SARIF message is plain text in which ``[text](target)`` makes a link, so
brackets and backslashes are escaped, and nothing here is written as Markdown.
"""

from __future__ import annotations

from typing import Any, Final

from tiltmeter.notices.scan import ScanFinding, ScanMatch, ScanReport

SARIF_VERSION: Final = "2.1.0"
SARIF_SCHEMA: Final = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME: Final = "Tiltmeter"
# Paths are relative to the repository's root, which is what this names.
SOURCE_ROOT: Final = "%SRCROOT%"

_ESCAPED: Final = str.maketrans({"\\": "\\\\", "[": "\\[", "]": "\\]"})


def plain(text: str) -> str:
    """Text for a SARIF plain-text message: no part of it is read as a link."""
    return text.translate(_ESCAPED)


def rule_id(finding: ScanFinding) -> str:
    """Unique in a catalog, where ids are unique per provider."""
    return f"{finding.provider}/{finding.id}"


def level(finding: ScanFinding) -> str:
    """error: in effect, or undated; warning: ahead, and counted; note: ahead, and further
    away than ``--within`` allows."""
    if not finding.due:
        return "note"
    return "warning" if finding.days_left is not None and finding.days_left > 0 else "error"


def _when(finding: ScanFinding) -> str:
    if finding.effective_at is None:
        return ""
    day = finding.effective_at.date().isoformat()
    if finding.days_left is not None and finding.days_left > 0:
        days = "1 day" if finding.days_left == 1 else f"{finding.days_left} days"
        return f" Takes effect on {day}, in {days}."
    return f" In effect since {day}."


def _help(finding: ScanFinding) -> str:
    parts = [plain(finding.summary)]
    if finding.signed:
        parts.append("Signed by the provider, and the signature was checked.")
    elif not finding.drafted:  # a drafted entry's summary says so itself
        parts.append("Not signed by the provider.")
    if finding.fix_command:
        parts.append(f"To fix it: {plain(finding.fix_command)}")
    return "\n\n".join(parts)


def _rule(finding: ScanFinding) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "tags": ["api-change", finding.provider],
        "provider": finding.provider,
        "notice": finding.id,
        "signed": finding.signed,
    }
    if finding.effective_at is not None:
        properties["effective_at"] = finding.effective_at.isoformat()
    return {
        "id": rule_id(finding),
        "shortDescription": {"text": plain(finding.headline)},
        "fullDescription": {"text": plain(finding.summary)},
        "help": {"text": _help(finding)},
        "defaultConfiguration": {"level": level(finding)},
        "properties": properties,
    }


def _result(finding: ScanFinding, match: ScanMatch, index: int, prefix: str) -> dict[str, Any]:
    text = f"{plain(match.term)}: {plain(match.detail)}. {plain(finding.headline)}"
    if match.applies_to:
        text += f" Applies to: {plain(match.applies_to)}"
    properties: dict[str, Any] = {"kind": match.kind, "term": match.term}
    if match.possibly_unused:
        properties["possibly_unused"] = True
    return {
        "ruleId": rule_id(finding),
        "ruleIndex": index,
        "level": level(finding),
        "message": {"text": text + _when(finding)},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": prefix + match.file, "uriBaseId": SOURCE_ROOT},
                    "region": {"startLine": match.line},
                }
            }
        ],
        "properties": properties,
    }


def sarif(report: ScanReport, *, version: str, url: str, prefix: str = "") -> dict[str, Any]:
    """The report as a SARIF log with one run. ``prefix`` is where the scanned folder lies
    in its repository, such as ``services/api/``, so that paths start at the repository's
    root, where code scanning looks for them. A catalog that could not be read gives a run
    that says it failed, with no results."""
    invocation: dict[str, Any] = {"executionSuccessful": report.error is None}
    if report.error is not None:
        invocation["toolExecutionNotifications"] = [
            {"level": "error", "message": {"text": plain(report.error)}}
        ]
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": version,
                        "informationUri": url,
                        "rules": [_rule(finding) for finding in report.affected],
                    }
                },
                "invocations": [invocation],
                "results": [
                    _result(finding, match, index, prefix)
                    for index, finding in enumerate(report.affected)
                    for match in finding.matches
                ],
            }
        ],
    }
