"""Shared pieces for notifiers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Protocol

import httpx

from driftwatch.core.models import CheckResult, Severity

MAX_LISTED_CHANGES: Final = 15
SEND_TIMEOUT: Final = 10.0


class NotificationError(Exception):
    """A notification could not be delivered. Messages never include the target URL."""


class Notifier(Protocol):
    """Somewhere check results are sent when they matter."""

    @property
    def name(self) -> str: ...

    def wants(self, result: CheckResult) -> bool: ...

    async def send(self, result: CheckResult) -> None: ...


class ThresholdNotifier:
    """Sends results at or above a severity, and failed checks when asked to."""

    def __init__(self, *, min_severity: Severity, on_error: bool) -> None:
        self.min_severity = min_severity
        self.on_error = on_error

    def wants(self, result: CheckResult) -> bool:
        if result.error:
            return self.on_error
        return result.drifted(self.min_severity)


def headline(result: CheckResult) -> str:
    """``BREAKING drift in stripe (2 changes)`` or ``check of stripe failed: ...``."""
    if result.error:
        return f"check of {result.endpoint_id} failed: {result.error}"
    count = len(result.changes)
    level = result.max_severity.value.upper() if result.max_severity else "NO"
    return f"{level} drift in {result.endpoint_id} ({count} change{'' if count == 1 else 's'})"


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: object,
    headers: Mapping[str, str] | None = None,
) -> None:
    try:
        response = await client.post(url, json=payload, headers=headers, timeout=SEND_TIMEOUT)
    except httpx.TimeoutException:
        raise NotificationError(f"timed out after {SEND_TIMEOUT:g}s") from None
    except httpx.TransportError as exc:
        raise NotificationError(f"request failed ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise NotificationError(f"HTTP {response.status_code}")
