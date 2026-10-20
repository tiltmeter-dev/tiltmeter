"""DriftService: the one object the CLI and the MCP server talk to.

It owns the config, the history store and the HTTP client, runs the LangGraph
check for each endpoint (with bounded concurrency), and answers the read-only
questions agents and humans ask afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Final

import httpx

from driftwatch.config import Config, load_config, resolve_state_dir
from driftwatch.core.fetch import EnvLookup, Sleep
from driftwatch.core.models import (
    CheckResult,
    DriftStatus,
    EndpointSummary,
    SchemaReport,
    Severity,
    StoredChange,
    max_severity,
)
from driftwatch.core.shape import to_json_schema
from driftwatch.core.store import Store
from driftwatch.core.timeutil import parse_since, to_iso, utcnow
from driftwatch.errors import DriftwatchError
from driftwatch.graph import DriftContext, build_graph
from driftwatch.llm.explainer import Explainer, build_request, has_credentials
from driftwatch.notify import Notifier, build_notifiers

DEFAULT_DRIFT_WINDOW: Final = timedelta(days=7)
NO_API_KEY: Final = "LLM explanation skipped: set ANTHROPIC_API_KEY to explain drift with Claude"


class DriftService:
    """Checks endpoints and answers questions about their history."""

    def __init__(
        self,
        config: Config,
        *,
        state_dir: Path,
        store: Store | None = None,
        http: httpx.AsyncClient | None = None,
        explainer: Explainer | None = None,
        detect_explainer: bool = True,
        notifiers: Sequence[Notifier] | None = None,
        env: EnvLookup = os.environ.get,
        clock: Callable[[], datetime] = utcnow,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.store = store if store is not None else Store.in_dir(state_dir)
        self._owns_http = http is None
        self.http = http if http is not None else httpx.AsyncClient(follow_redirects=True)
        self._explainer = explainer
        self._detect_explainer = detect_explainer and explainer is None
        self.notifiers: list[Notifier] = (
            list(notifiers)
            if notifiers is not None
            else build_notifiers(config.notify, client=self.http, env=env)
        )
        self._env = env
        self._clock = clock
        self._sleep = sleep
        self._graph = build_graph()

    @classmethod
    def from_config_file(cls, path: Path, **kwargs: Any) -> DriftService:
        config = load_config(path)
        return cls(config, state_dir=resolve_state_dir(config, path), **kwargs)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()
        self.store.close()

    async def __aenter__(self) -> DriftService:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- explanations ---------------------------------------------------------

    def _resolve_explainer(self) -> tuple[Explainer | None, str | None]:
        """The explainer to use and, when there is none, why."""
        if self._explainer is not None:
            return self._explainer, None
        if not self.config.llm.enabled:
            return None, "LLM explanations are disabled in the config (llm.enabled: false)"
        if not self._detect_explainer:
            return None, None
        if not has_credentials(self._env):
            return None, NO_API_KEY
        from driftwatch.llm.explainer import ClaudeExplainer

        self._explainer = ClaudeExplainer.from_config(self.config.llm)
        return self._explainer, None

    # -- checks ---------------------------------------------------------------

    async def check(
        self,
        endpoint_ids: Sequence[str] | None = None,
        *,
        explain: bool = True,
        notify: bool = True,
    ) -> list[CheckResult]:
        """Check endpoints (all of them by default), at most ``concurrency`` at a time."""
        endpoints = self.config.select(endpoint_ids)
        use_llm = explain and self.config.llm.enabled
        explainer, unavailable = self._resolve_explainer() if use_llm else (None, None)
        context = DriftContext(
            config=self.config,
            store=self.store,
            http=self.http,
            env=self._env,
            explainer=explainer,
            explainer_unavailable=unavailable,
            notifiers=self.notifiers,
            clock=self._clock,
            sleep=self._sleep,
        )
        limit = asyncio.Semaphore(self.config.concurrency)

        async def run(endpoint_id: str) -> CheckResult:
            async with limit:
                state = await self._graph.ainvoke(
                    {
                        "endpoint_id": endpoint_id,
                        "use_llm": use_llm,
                        "send_notifications": notify,
                        "notes": [],
                    },
                    config={
                        "run_name": f"driftwatch check {endpoint_id}",
                        "tags": ["driftwatch"],
                        "metadata": {"endpoint_id": endpoint_id},
                    },
                    context=context,
                )
                result: CheckResult = state["result"]
                return result

        return list(await asyncio.gather(*(run(endpoint.id) for endpoint in endpoints)))

    async def check_endpoint(
        self, endpoint_id: str, *, explain: bool = True, notify: bool = True
    ) -> CheckResult:
        [result] = await self.check([endpoint_id], explain=explain, notify=notify)
        return result

    async def explain_check(self, check_id: int) -> CheckResult:
        """(Re)write the explanation of a stored check and save it."""
        check = self.store.get_check(check_id)
        if check is None:
            raise DriftwatchError(f"no check with id {check_id}")
        if not check.changes:
            raise DriftwatchError(f"check #{check_id} found no changes to explain")
        explainer, unavailable = self._resolve_explainer()
        if explainer is None:
            raise DriftwatchError(unavailable or "no LLM explainer is configured")
        endpoint = self.config.endpoint(check.endpoint_id)
        baseline = self.store.load_baseline(
            endpoint.id, window=endpoint.baseline_window, before_check_id=check_id
        )
        request = build_request(
            endpoint,
            check.changes,
            baseline=baseline.shape,
            observed=self.store.check_shape(check_id),
        )
        explanation = await explainer.explain(request)
        self.store.update_explanation(check_id, explanation, explainer.model_name)
        return check.model_copy(
            update={"explanation": explanation, "explanation_model": explainer.model_name}
        )

    # -- questions ------------------------------------------------------------

    def history(self, endpoint_id: str | None = None, *, limit: int = 20) -> list[CheckResult]:
        return self.store.list_checks(endpoint_id, limit=limit)

    def get_check(self, check_id: int) -> CheckResult | None:
        return self.store.get_check(check_id)

    def changes(
        self,
        endpoint_id: str | None = None,
        *,
        since: str | None = None,
        min_severity: Severity | None = None,
        limit: int = 100,
    ) -> list[StoredChange]:
        since_iso = parse_since(since, now=self._clock()) if since else None
        return self.store.list_changes(
            endpoint_id, since=since_iso, min_severity=min_severity, limit=limit
        )

    def schema(self, endpoint_id: str) -> SchemaReport:
        """The JSON Schema of the responses, as learned from the baseline window."""
        endpoint = self.config.endpoint(endpoint_id)
        baseline = self.store.load_baseline(endpoint.id, window=endpoint.baseline_window)
        shape = baseline.shape
        return SchemaReport(
            endpoint_id=endpoint.id,
            observations=shape.count if shape is not None else 0,
            baseline_checks=baseline.checks,
            json_schema=to_json_schema(shape, title=endpoint.id) if shape is not None else None,
        )

    def drift_status(self, endpoint_id: str, *, since: str | None = None) -> DriftStatus:
        """Has the endpoint changed (warning or worse) since ``since`` (default: 7 days)?"""
        endpoint = self.config.endpoint(endpoint_id)
        now = self._clock()
        since_iso = parse_since(since, now=now) if since else to_iso(now - DEFAULT_DRIFT_WINDOW)
        changes = self.store.list_changes(
            endpoint.id, since=since_iso, min_severity=Severity.WARNING, limit=50
        )
        changes.sort(key=lambda change: (-change.severity.rank, -change.id))
        latest = self.store.latest_check(endpoint.id)
        worst = max_severity(change.severity for change in changes)
        if latest is None:
            summary = f"{endpoint.id} has never been checked; call check_api first."
        elif worst is not None:
            summary = (
                f"{len(changes)} change(s) at warning level or above since {since_iso}, "
                f"worst {worst.value}. {changes[0].message}"
            )
        else:
            summary = f"No drift since {since_iso}. Last checked {latest.checked_at}."
        if latest is not None and latest.error:
            summary += f" The last check failed: {latest.error}."
        return DriftStatus(
            endpoint_id=endpoint.id,
            drifted=worst is not None,
            max_severity=worst,
            since=since_iso,
            last_checked_at=latest.checked_at if latest else None,
            last_check_error=latest.error if latest else None,
            changes=changes[:20],
            summary=summary,
        )

    def endpoints(self) -> list[EndpointSummary]:
        """Every configured endpoint with the outcome of its last check."""
        summaries = []
        for endpoint in self.config.endpoints:
            latest = self.store.latest_check(endpoint.id)
            summaries.append(
                EndpointSummary(
                    id=endpoint.id,
                    method=endpoint.method,
                    url=endpoint.display_url(),
                    description=endpoint.description,
                    last_checked_at=latest.checked_at if latest else None,
                    last_outcome=latest.outcome if latest else None,
                    last_severity=latest.max_severity if latest else None,
                )
            )
        return summaries
