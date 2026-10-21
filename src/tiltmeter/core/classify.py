"""Severity rules: how much each kind of change matters to a consumer of the API.

The defaults take the point of view of code (or an agent) that reads the
responses: anything that can make a read fail or return something unexpected is
breaking, anything new but harmless is info.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from tiltmeter.core.models import Change, ChangeKind, RawChange, Severity

DEFAULT_SEVERITY: Final[Mapping[ChangeKind, Severity]] = MappingProxyType(
    {
        ChangeKind.FIELD_ADDED: Severity.INFO,
        ChangeKind.FIELD_REMOVED: Severity.BREAKING,
        ChangeKind.FIELD_NOW_OPTIONAL: Severity.BREAKING,
        ChangeKind.TYPE_CHANGED: Severity.BREAKING,
        ChangeKind.NULLABLE_INTRODUCED: Severity.BREAKING,
        ChangeKind.ENUM_VALUE_ADDED: Severity.WARNING,
        ChangeKind.STATUS_CHANGED: Severity.WARNING,
        ChangeKind.CONTENT_TYPE_CHANGED: Severity.BREAKING,
        ChangeKind.LATENCY_REGRESSION: Severity.WARNING,
    }
)


def default_severity(change: RawChange, *, strict: bool = False) -> Severity:
    """The built-in severity for a change.

    ``strict`` treats new fields as warnings, for consumers that reject unknown keys.
    Status changes depend on direction: success to failure is breaking, failure
    to success is info, anything else is a warning.
    """
    if change.kind is ChangeKind.FIELD_ADDED:
        return Severity.WARNING if strict else Severity.INFO
    if change.kind is ChangeKind.STATUS_CHANGED:
        return _status_severity(change.before, change.after)
    return DEFAULT_SEVERITY[change.kind]


def classify(
    change: RawChange,
    *,
    strict: bool = False,
    overrides: Mapping[ChangeKind, Severity] | None = None,
) -> Change:
    """Attach a severity to a raw change. ``overrides`` win over the defaults."""
    severity = overrides.get(change.kind) if overrides else None
    if severity is None:
        severity = default_severity(change, strict=strict)
    return Change(
        path=change.path,
        kind=change.kind,
        severity=severity,
        before=change.before,
        after=change.after,
        hint=change.hint,
    )


def _is_success(status: str | None) -> bool:
    return status is not None and status.isdigit() and 200 <= int(status) < 300


def _status_severity(before: str | None, after: str | None) -> Severity:
    was_ok, now_ok = _is_success(before), _is_success(after)
    if was_ok and not now_ok:
        return Severity.BREAKING
    if now_ok and not was_ok:
        return Severity.INFO
    return Severity.WARNING
