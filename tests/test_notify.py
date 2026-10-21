from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.helpers import env_of, explanation
from tiltmeter.config import SlackNotifierConfig, WebhookNotifierConfig
from tiltmeter.core.models import Change, ChangeKind, CheckResult, Severity
from tiltmeter.notify import NotificationError, build_notifiers
from tiltmeter.notify.base import ThresholdNotifier, headline
from tiltmeter.notify.slack import SlackNotifier, slack_text
from tiltmeter.notify.webhook import WebhookNotifier, webhook_payload

HOOK = "https://hooks.test/T000/B000/secret-token"


def drift(severity: Severity = Severity.BREAKING, changes: int = 1, **extra: object) -> CheckResult:
    data: dict[str, object] = {
        "endpoint_id": "stripe",
        "check_id": 7,
        "checked_at": "2026-10-20T12:00:00Z",
        "status_code": 200,
        "changes": [
            Change(path=f"$.f{i}", kind=ChangeKind.FIELD_REMOVED, severity=severity, before="a<b>")
            for i in range(changes)
        ],
        "max_severity": severity,
    }
    data.update(extra)
    return CheckResult.model_validate(data)


def failed() -> CheckResult:
    return CheckResult(endpoint_id="stripe", checked_at="2026-10-20T12:00:00Z", error="timed out")


def test_threshold_logic() -> None:
    notifier = ThresholdNotifier(min_severity=Severity.WARNING, on_error=False)
    assert notifier.wants(drift(Severity.BREAKING))
    assert notifier.wants(drift(Severity.WARNING))
    assert not notifier.wants(drift(Severity.INFO))
    assert not notifier.wants(failed())
    assert ThresholdNotifier(min_severity=Severity.BREAKING, on_error=True).wants(failed())
    clean = CheckResult(endpoint_id="x", checked_at="t")
    assert not notifier.wants(clean)


def test_headline() -> None:
    assert headline(drift(changes=2)) == "BREAKING drift in stripe (2 changes)"
    assert headline(drift(Severity.WARNING)) == "WARNING drift in stripe (1 change)"
    assert headline(failed()) == "check of stripe failed: timed out"


def test_slack_text_escapes_and_truncates() -> None:
    text = slack_text(drift(changes=20, explanation=explanation(summary="a & b")))
    assert text.startswith(":rotating_light: *Tiltmeter*")
    assert "a&lt;b&gt;" in text
    assert "…and 5 more" in text
    assert "> a &amp; b" in text
    assert "> *Action:* Read phone_number instead." in text
    assert text.endswith("_check #7 · 2026-10-20T12:00:00Z_")
    assert slack_text(failed()).startswith(":x:")
    assert slack_text(drift(Severity.WARNING)).startswith(":warning:")


@respx.mock
async def test_slack_posts_to_the_expanded_url() -> None:
    route = respx.post(HOOK).respond(200)
    config = SlackNotifierConfig(type="slack", webhook_url="${SLACK_URL}")
    async with httpx.AsyncClient() as client:
        await SlackNotifier(config, client=client, env=env_of(SLACK_URL=HOOK)).send(drift())
    assert "BREAKING drift in stripe" in json.loads(route.calls.last.request.content)["text"]


@respx.mock
async def test_send_errors_never_reveal_the_url() -> None:
    respx.post(HOOK).respond(404)
    config = SlackNotifierConfig(type="slack", webhook_url=HOOK)
    async with httpx.AsyncClient() as client:
        notifier = SlackNotifier(config, client=client, env=env_of())
        with pytest.raises(NotificationError, match="HTTP 404") as caught:
            await notifier.send(drift())
        assert "secret-token" not in str(caught.value)
        respx.post(HOOK).mock(side_effect=httpx.ConnectError(f"cannot connect to {HOOK}"))
        with pytest.raises(NotificationError) as caught:
            await notifier.send(drift())
        assert "secret-token" not in str(caught.value)
        respx.post(HOOK).mock(side_effect=httpx.ConnectTimeout("slow"))
        with pytest.raises(NotificationError, match="timed out"):
            await notifier.send(drift())
        missing = SlackNotifier(
            SlackNotifierConfig(type="slack", webhook_url="${NOPE}"), client=client, env=env_of()
        )
        with pytest.raises(NotificationError, match="NOPE"):
            await missing.send(drift())


@respx.mock
async def test_webhook_payload_and_headers() -> None:
    route = respx.post("https://hooks.test/dw").respond(204)
    config = WebhookNotifierConfig(
        type="webhook",
        url="https://hooks.test/dw",
        headers={"Authorization": "Bearer ${HOOK_TOKEN}"},
    )
    async with httpx.AsyncClient() as client:
        await WebhookNotifier(config, client=client, env=env_of(HOOK_TOKEN="tok")).send(drift())
        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["event"] == "drift.detected"
        assert body["result"]["changes"][0]["message"].startswith("Field $.f0")
        broken = WebhookNotifier(
            WebhookNotifierConfig(type="webhook", url="${MISSING}"), client=client, env=env_of()
        )
        with pytest.raises(NotificationError, match="MISSING"):
            await broken.send(drift())
    assert webhook_payload(failed())["event"] == "check.failed"


async def test_build_notifiers() -> None:
    configs = [
        SlackNotifierConfig(type="slack", webhook_url=HOOK),
        WebhookNotifierConfig(type="webhook", url="https://x.test", min_severity=Severity.INFO),
    ]
    async with httpx.AsyncClient() as client:
        notifiers = build_notifiers(configs, client=client, env=env_of())
    assert [n.name for n in notifiers] == ["slack", "webhook"]
    assert notifiers[1].wants(drift(Severity.INFO))
