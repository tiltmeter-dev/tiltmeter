from __future__ import annotations

import pytest

from tiltmeter.core.classify import DEFAULT_SEVERITY, classify, default_severity
from tiltmeter.core.models import ChangeKind, RawChange, Severity


def raw(kind: ChangeKind, before: str | None = None, after: str | None = None) -> RawChange:
    return RawChange("$.x", kind, before=before, after=after)


def test_every_kind_has_a_default() -> None:
    assert set(DEFAULT_SEVERITY) == set(ChangeKind)


@pytest.mark.parametrize(
    ("kind", "severity"),
    [
        (ChangeKind.FIELD_ADDED, Severity.INFO),
        (ChangeKind.FIELD_REMOVED, Severity.BREAKING),
        (ChangeKind.FIELD_NOW_OPTIONAL, Severity.BREAKING),
        (ChangeKind.TYPE_CHANGED, Severity.BREAKING),
        (ChangeKind.NULLABLE_INTRODUCED, Severity.BREAKING),
        (ChangeKind.ENUM_VALUE_ADDED, Severity.WARNING),
        (ChangeKind.CONTENT_TYPE_CHANGED, Severity.BREAKING),
        (ChangeKind.LATENCY_REGRESSION, Severity.WARNING),
    ],
)
def test_defaults(kind: ChangeKind, severity: Severity) -> None:
    assert default_severity(raw(kind)) is severity


def test_strict_mode_makes_new_fields_warnings() -> None:
    assert default_severity(raw(ChangeKind.FIELD_ADDED), strict=True) is Severity.WARNING


@pytest.mark.parametrize(
    ("before", "after", "severity"),
    [
        ("200", "500", Severity.BREAKING),
        ("200", "404", Severity.BREAKING),
        ("404", "200", Severity.INFO),
        ("200", "201", Severity.WARNING),
        ("500", "404", Severity.WARNING),
        (None, "200", Severity.INFO),
    ],
)
def test_status_severity_depends_on_direction(
    before: str | None, after: str | None, severity: Severity
) -> None:
    assert default_severity(raw(ChangeKind.STATUS_CHANGED, before, after)) is severity


def test_classify_applies_overrides_and_keeps_fields() -> None:
    change = RawChange("$.a", ChangeKind.FIELD_REMOVED, before="string", hint="possibly renamed")
    plain = classify(change)
    assert plain.severity is Severity.BREAKING
    assert (plain.path, plain.before, plain.after, plain.hint) == (
        "$.a",
        "string",
        None,
        "possibly renamed",
    )
    relaxed = classify(change, overrides={ChangeKind.FIELD_REMOVED: Severity.WARNING})
    assert relaxed.severity is Severity.WARNING
    other = classify(change, overrides={ChangeKind.FIELD_ADDED: Severity.BREAKING})
    assert other.severity is Severity.BREAKING
