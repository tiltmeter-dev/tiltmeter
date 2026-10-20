"""Builders and fakes shared by the test-suite."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from driftwatch.config import Config, EndpointConfig
from driftwatch.core.models import CheckResult, Explanation, RenameHint, Severity

if TYPE_CHECKING:
    from driftwatch.llm.explainer import ExplanationRequest

API = "https://api.test"


def make_endpoint(**overrides: Any) -> EndpointConfig:
    data: dict[str, Any] = {"id": "items", "url": f"{API}/v1/items", "retries": 0}
    data.update(overrides)
    return EndpointConfig.model_validate(data)


def make_config(*endpoints: EndpointConfig, **overrides: Any) -> Config:
    data: dict[str, Any] = {
        "endpoints": [e.model_dump(mode="json") for e in endpoints]
        or [make_endpoint().model_dump(mode="json")]
    }
    data.update(overrides)
    return Config.model_validate(data)


def explanation(**overrides: Any) -> Explanation:
    data: dict[str, Any] = {
        "summary": "The phone field was renamed.",
        "likely_cause": "The vendor renamed phone to phone_number.",
        "impact": "Readers of phone get nothing.",
        "suggested_action": "Read phone_number instead.",
        "probable_renames": [RenameHint(before_path="$.phone", after_path="$.phone_number")],
        "confidence": "high",
    }
    data.update(overrides)
    return Explanation.model_validate(data)


class FakeExplainer:
    """Records requests and answers with a canned explanation (or raises)."""

    model_name = "fake-model"

    def __init__(self, result: Explanation | None = None, error: Exception | None = None) -> None:
        self.result = result or explanation()
        self.error = error
        self.requests: list[ExplanationRequest] = []

    async def explain(self, request: ExplanationRequest) -> Explanation:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingNotifier:
    """A notifier that keeps what it was asked to send."""

    def __init__(
        self,
        name: str = "recording",
        min_severity: Severity = Severity.WARNING,
        on_error: bool = False,
        fail: bool = False,
    ) -> None:
        self.name = name
        self.min_severity = min_severity
        self.on_error = on_error
        self.fail = fail
        self.sent: list[CheckResult] = []

    def wants(self, result: CheckResult) -> bool:
        if result.error:
            return self.on_error
        return result.max_severity is not None and result.max_severity.at_least(self.min_severity)

    async def send(self, result: CheckResult) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(result)


class FakeClock:
    """A controllable clock: every call returns the current time, ``advance`` moves it."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def env_of(**values: str) -> Callable[[str], str | None]:
    return values.get


async def no_sleep(_: float) -> None:
    return None
