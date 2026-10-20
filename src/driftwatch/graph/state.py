"""State and runtime context for one check of one endpoint."""

from __future__ import annotations

import asyncio
import operator
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, TypedDict

import httpx

from driftwatch.config import Config
from driftwatch.core.fetch import EnvLookup, Sleep
from driftwatch.core.models import (
    Baseline,
    Change,
    CheckResult,
    Explanation,
    Observation,
    Severity,
)
from driftwatch.core.store import Store
from driftwatch.core.timeutil import utcnow
from driftwatch.llm.explainer import Explainer
from driftwatch.notify import Notifier


class DriftState(TypedDict, total=False):
    """What flows between the nodes. It never holds credentials or raw response bodies
    (unless ``store_samples``/``include_samples`` is on), so LangSmith traces are safe."""

    endpoint_id: str
    use_llm: bool
    send_notifications: bool
    checked_at: str
    observation: Observation
    baseline: Baseline
    changes: list[Change]
    max_severity: Severity | None
    first_run: bool
    error: str | None
    wants_explanation: bool
    explanation: Explanation | None
    explanation_model: str | None
    wants_notification: bool
    result: CheckResult
    notes: Annotated[list[str], operator.add]


@dataclass
class DriftContext:
    """Dependencies handed to the nodes through LangGraph's runtime context."""

    config: Config
    store: Store
    http: httpx.AsyncClient
    env: EnvLookup
    explainer: Explainer | None = None
    explainer_unavailable: str | None = None
    notifiers: Sequence[Notifier] = ()
    clock: Callable[[], datetime] = utcnow
    sleep: Sleep = asyncio.sleep
