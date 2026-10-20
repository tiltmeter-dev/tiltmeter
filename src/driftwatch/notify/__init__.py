"""Where check results go when they matter: Slack and generic webhooks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import httpx

from driftwatch.core.fetch import EnvLookup
from driftwatch.notify.base import NotificationError, Notifier
from driftwatch.notify.slack import SlackNotifier
from driftwatch.notify.webhook import WebhookNotifier

if TYPE_CHECKING:
    from driftwatch.config import NotifierConfig

__all__ = [
    "NotificationError",
    "Notifier",
    "SlackNotifier",
    "WebhookNotifier",
    "build_notifiers",
]


def build_notifiers(
    configs: Sequence[NotifierConfig], *, client: httpx.AsyncClient, env: EnvLookup
) -> list[Notifier]:
    """One notifier per entry of the ``notify`` config section."""
    notifiers: list[Notifier] = []
    for config in configs:
        if config.type == "slack":
            notifiers.append(SlackNotifier(config, client=client, env=env))
        else:
            notifiers.append(WebhookNotifier(config, client=client, env=env))
    return notifiers
