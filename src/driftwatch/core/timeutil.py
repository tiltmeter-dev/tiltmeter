"""UTC timestamps in one sortable text format."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

_RELATIVE: Final = re.compile(r"(\d+)\s*([smhdw])")
_UNITS: Final = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def utcnow() -> datetime:
    """Current time in UTC."""
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Format as ``2026-10-20T21:46:00Z``. Naive datetimes are taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_since(value: str, *, now: datetime | None = None) -> str:
    """Turn ``"24h"``, ``"7d"``, ``"30m"`` or an ISO 8601 timestamp into ``to_iso`` form."""
    text = value.strip()
    match = _RELATIVE.fullmatch(text.lower())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        base = now if now is not None else utcnow()
        return to_iso(base - timedelta(**{_UNITS[unit]: amount}))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"expected a duration like 24h or 7d, or an ISO 8601 timestamp, got {value!r}"
        ) from exc
    return to_iso(moment)
