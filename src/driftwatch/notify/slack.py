"""Slack incoming-webhook notifications."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from driftwatch.core.fetch import EnvLookup, Expander
from driftwatch.core.models import CheckResult, Severity
from driftwatch.errors import MissingVariableError
from driftwatch.notify.base import (
    MAX_LISTED_CHANGES,
    NotificationError,
    ThresholdNotifier,
    headline,
    post_json,
)

if TYPE_CHECKING:
    from driftwatch.config import SlackNotifierConfig

_ICONS = {
    Severity.BREAKING: ":rotating_light:",
    Severity.WARNING: ":warning:",
    Severity.INFO: ":information_source:",
}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_text(result: CheckResult) -> str:
    """The message body, in Slack mrkdwn."""
    if result.error:
        icon = ":x:"
    else:
        icon = _ICONS.get(result.max_severity or Severity.INFO, ":information_source:")
    lines = [f"{icon} *driftwatch* · {_escape(headline(result))}"]
    for change in result.changes[:MAX_LISTED_CHANGES]:
        lines.append(f"• *{change.severity.value}* {_escape(change.message)}")
    hidden = len(result.changes) - MAX_LISTED_CHANGES
    if hidden > 0:
        lines.append(f"…and {hidden} more")
    if result.explanation is not None:
        lines.append(f"> {_escape(result.explanation.summary)}")
        lines.append(f"> *Action:* {_escape(result.explanation.suggested_action)}")
    footer = f"check #{result.check_id}" if result.check_id is not None else "check"
    lines.append(f"_{footer} · {result.checked_at}_")
    return "\n".join(lines)


class SlackNotifier(ThresholdNotifier):
    """Posts a short summary to a Slack incoming webhook."""

    name = "slack"

    def __init__(
        self, config: SlackNotifierConfig, *, client: httpx.AsyncClient, env: EnvLookup
    ) -> None:
        super().__init__(min_severity=config.min_severity, on_error=config.on_error)
        self._config = config
        self._client = client
        self._env = env

    async def send(self, result: CheckResult) -> None:
        try:
            url = Expander(self._env).expand(self._config.webhook_url)
        except MissingVariableError as exc:
            raise NotificationError(str(exc)) from None
        await post_json(self._client, url, {"text": slack_text(result)})
