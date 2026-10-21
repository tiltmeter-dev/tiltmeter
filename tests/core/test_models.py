from __future__ import annotations

import pytest

from tiltmeter.core.models import (
    Change,
    ChangeKind,
    CheckResult,
    Severity,
    describe_change,
    max_severity,
)


@pytest.mark.parametrize(
    ("kind", "path", "before", "after", "message"),
    [
        (ChangeKind.FIELD_ADDED, "$.a", None, "string", "New field $.a (string)."),
        (
            ChangeKind.FIELD_REMOVED,
            "$.a",
            "string",
            None,
            "Field $.a (string) is no longer returned.",
        ),
        (
            ChangeKind.FIELD_NOW_OPTIONAL,
            "$.a",
            "always present",
            "missing from 1 of 3 objects",
            "Field $.a used to be in every response and is now missing from 1 of 3 objects.",
        ),
        (
            ChangeKind.TYPE_CHANGED,
            "$",
            "object",
            "array",
            "The response body changed type from object to array.",
        ),
        (
            ChangeKind.NULLABLE_INTRODUCED,
            "$.a",
            "string",
            "string | null",
            "Field $.a can now be null (was string).",
        ),
        (
            ChangeKind.ENUM_VALUE_ADDED,
            "$.s",
            "open, closed",
            "merged",
            "Field $.s returned new value(s): merged. Known values: open, closed.",
        ),
        (ChangeKind.STATUS_CHANGED, "$", "200", "503", "HTTP status changed from 200 to 503."),
        (
            ChangeKind.CONTENT_TYPE_CHANGED,
            "$",
            "application/json",
            "text/html (not valid JSON)",
            "The response is no longer JSON: content type was application/json, "
            "now text/html (not valid JSON).",
        ),
        (
            ChangeKind.LATENCY_REGRESSION,
            "$",
            "100 ms",
            "900 ms",
            "Median latency rose from 100 ms to 900 ms.",
        ),
    ],
)
def test_every_kind_reads_as_a_sentence(
    kind: ChangeKind, path: str, before: str | None, after: str | None, message: str
) -> None:
    assert describe_change(kind, path, before, after, None) == message


def test_hints_are_appended_as_a_sentence() -> None:
    change = Change(
        path="$.phone",
        kind=ChangeKind.FIELD_REMOVED,
        severity=Severity.BREAKING,
        before="string",
        hint="possibly renamed to $.phone_number",
    )
    assert change.message.endswith("Possibly renamed to $.phone_number.")
    assert change.model_dump()["message"] == change.message


def test_severity_helpers() -> None:
    assert Severity.BREAKING.at_least(Severity.WARNING)
    assert not Severity.INFO.at_least(Severity.WARNING)
    assert (
        max_severity([None, Severity.INFO, Severity.BREAKING, Severity.WARNING])
        is Severity.BREAKING
    )
    assert max_severity([]) is None


@pytest.mark.parametrize(
    ("fields", "outcome"),
    [
        ({"error": "down"}, "error"),
        ({"max_severity": "breaking", "changes": []}, "drift"),
        (
            {
                "max_severity": "info",
                "changes": [{"path": "$.a", "kind": "field_added", "severity": "info"}],
            },
            "changed",
        ),
        ({"first_run": True}, "baseline"),
        ({}, "clean"),
    ],
)
def test_check_outcome(fields: dict[str, object], outcome: str) -> None:
    result = CheckResult.model_validate({"endpoint_id": "x", "checked_at": "t", **fields})
    assert result.outcome == outcome
    assert result.drifted() is (outcome == "drift")
