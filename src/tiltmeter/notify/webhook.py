"""Generic JSON webhook notifications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from tiltmeter import __version__
from tiltmeter.core.fetch import EnvLookup, Expander
from tiltmeter.core.models import CheckResult
from tiltmeter.errors import MissingVariableError
from tiltmeter.notify.base import NotificationError, ThresholdNotifier, post_json

if TYPE_CHECKING:
    from tiltmeter.config import WebhookNotifierConfig


def webhook_payload(result: CheckResult) -> dict[str, Any]:
    """``{"event": "drift.detected" | "check.failed", "result": {...}}``."""
    return {
        "event": "check.failed" if result.error else "drift.detected",
        "tiltmeter_version": __version__,
        "result": result.model_dump(mode="json"),
    }


class WebhookNotifier(ThresholdNotifier):
    """POSTs the full check result as JSON."""

    name = "webhook"

    def __init__(
        self, config: WebhookNotifierConfig, *, client: httpx.AsyncClient, env: EnvLookup
    ) -> None:
        super().__init__(min_severity=config.min_severity, on_error=config.on_error)
        self._config = config
        self._client = client
        self._env = env

    async def send(self, result: CheckResult) -> None:
        expander = Expander(self._env)
        try:
            url = expander.expand(self._config.url)
            headers = {name: expander.expand(value) for name, value in self._config.headers.items()}
        except MissingVariableError as exc:
            raise NotificationError(str(exc)) from None
        await post_json(self._client, url, webhook_payload(result), headers)
