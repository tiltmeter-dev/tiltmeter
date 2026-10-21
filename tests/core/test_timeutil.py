from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tiltmeter.core.timeutil import parse_since, to_iso, utcnow

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def test_to_iso_normalizes_to_utc() -> None:
    assert to_iso(NOW) == "2026-10-20T12:00:00Z"
    assert to_iso(datetime(2026, 10, 20, 9, 0, tzinfo=timezone(timedelta(hours=-3)))) == (
        "2026-10-20T12:00:00Z"
    )
    assert to_iso(datetime(2026, 10, 20, 12, 0)) == "2026-10-20T12:00:00Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30m", "2026-10-20T11:30:00Z"),
        ("24h", "2026-10-19T12:00:00Z"),
        ("7d", "2026-10-13T12:00:00Z"),
        ("2w", "2026-10-06T12:00:00Z"),
        (" 10 S ", "2026-10-20T11:59:50Z"),
        ("2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
        ("2026-10-01", "2026-10-01T00:00:00Z"),
    ],
)
def test_parse_since(value: str, expected: str) -> None:
    assert parse_since(value, now=NOW) == expected


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_since("yesterday")


def test_utcnow_is_aware() -> None:
    assert utcnow().tzinfo is UTC
