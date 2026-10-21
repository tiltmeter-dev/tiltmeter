"""A DriftService wired to fakes: mocked HTTP, in-memory store, fake LLM and notifier."""

from __future__ import annotations

from typing import Any

import httpx

from tests.helpers import (
    FakeClock,
    FakeExplainer,
    RecordingNotifier,
    env_of,
    make_config,
    make_endpoint,
    no_sleep,
)
from tiltmeter.config import Config, EndpointConfig
from tiltmeter.core.store import Store
from tiltmeter.service import DriftService


def make_service(
    *endpoints: EndpointConfig,
    config: Config | None = None,
    explainer: FakeExplainer | None = None,
    notifier: RecordingNotifier | None = None,
    env: dict[str, str] | None = None,
    clock: FakeClock | None = None,
    detect_explainer: bool = False,
    **config_overrides: Any,
) -> DriftService:
    if config is None:
        config = make_config(*(endpoints or (make_endpoint(),)), **config_overrides)
    return DriftService(
        config,
        state_dir=__import__("pathlib").Path("/nonexistent"),
        store=Store(":memory:"),
        http=httpx.AsyncClient(),
        explainer=explainer,
        detect_explainer=detect_explainer,
        notifiers=[notifier] if notifier is not None else [],
        env=env_of(**(env or {})),
        clock=clock or FakeClock(),
        sleep=no_sleep,
    )
