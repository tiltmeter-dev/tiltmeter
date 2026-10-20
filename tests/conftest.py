from __future__ import annotations

from collections.abc import Iterator

import pytest

from driftwatch.core.store import Store

_LIVE_SERVICE_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_TRACING_V2",
    "DRIFTWATCH_CONFIG",
)


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never reach Claude or LangSmith, whatever the developer has exported."""
    if request.node.get_closest_marker("integration") is None:
        for name in _LIVE_SERVICE_VARIABLES:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as db:
        yield db
