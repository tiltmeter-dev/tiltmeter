"""The nodes of the drift graph. Each returns a partial state update."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from langgraph.runtime import Runtime

from tiltmeter.config import EndpointConfig
from tiltmeter.core.analyze import Rules, analyze
from tiltmeter.core.fetch import observe_endpoint
from tiltmeter.core.models import CheckResult
from tiltmeter.core.paths import PathMatcher
from tiltmeter.core.timeutil import to_iso
from tiltmeter.graph.state import DriftContext, DriftState
from tiltmeter.llm.explainer import build_request

_MAX_NOTE = 300


def _endpoint(state: DriftState, runtime: Runtime[DriftContext]) -> EndpointConfig:
    return runtime.context.config.endpoint(state["endpoint_id"])


def _note(prefix: str, exc: BaseException) -> str:
    text = f"{prefix}: {exc}" if str(exc) else f"{prefix}: {type(exc).__name__}"
    return text if len(text) <= _MAX_NOTE else text[: _MAX_NOTE - 1] + "…"


async def observe(state: DriftState, runtime: Runtime[DriftContext]) -> dict[str, Any]:
    """Call the endpoint and infer the shape of what it returned."""
    ctx = runtime.context
    endpoint = _endpoint(state, runtime)
    keep_excerpt = endpoint.store_samples or (
        ctx.config.llm.include_samples and state.get("use_llm", False)
    )
    observation = await observe_endpoint(
        ctx.http, endpoint, env=ctx.env, keep_excerpt=keep_excerpt, sleep=ctx.sleep
    )
    return {"observation": observation, "checked_at": to_iso(ctx.clock())}


def after_observe(state: DriftState) -> Literal["load_baseline", "persist"]:
    return "persist" if state["observation"].error else "load_baseline"


async def load_baseline(state: DriftState, runtime: Runtime[DriftContext]) -> dict[str, Any]:
    """Rebuild the baseline from the endpoint's recent history."""
    endpoint = _endpoint(state, runtime)
    baseline = runtime.context.store.load_baseline(endpoint.id, window=endpoint.baseline_window)
    return {"baseline": baseline}


async def compare(state: DriftState, runtime: Runtime[DriftContext]) -> dict[str, Any]:
    """Diff the observation against the baseline and classify every change."""
    ctx = runtime.context
    endpoint = _endpoint(state, runtime)
    rules = Rules(
        ignore=PathMatcher(endpoint.ignore_paths),
        strict=endpoint.strict,
        overrides=ctx.config.overrides_for(endpoint),
        latency_factor=endpoint.latency_factor,
    )
    analysis = analyze(state["baseline"], state["observation"], rules)
    notes: list[str] = []
    wants_explanation = False
    worth_explaining = analysis.max_severity is not None and analysis.max_severity.at_least(
        ctx.config.llm.min_severity
    )
    if worth_explaining and state.get("use_llm", False):
        if ctx.explainer is not None:
            wants_explanation = True
        elif ctx.explainer_unavailable:
            notes.append(ctx.explainer_unavailable)
    return {
        "changes": analysis.changes,
        "max_severity": analysis.max_severity,
        "error": analysis.error,
        "first_run": analysis.first_run,
        "wants_explanation": wants_explanation,
        "notes": notes,
    }


def after_compare(state: DriftState) -> Literal["explain", "persist"]:
    return "explain" if state.get("wants_explanation") else "persist"


async def explain(state: DriftState, runtime: Runtime[DriftContext]) -> dict[str, Any]:
    """Ask the LLM what the changes mean. Failures become notes, never failed checks."""
    ctx = runtime.context
    explainer = ctx.explainer
    if explainer is None:  # pragma: no cover - routing prevents it
        return {}
    observation = state["observation"]
    request = build_request(
        _endpoint(state, runtime),
        state.get("changes", []),
        baseline=state["baseline"].shape,
        observed=observation.shape,
        excerpt=observation.excerpt if ctx.config.llm.include_samples else None,
    )
    try:
        explanation = await explainer.explain(request)
    except Exception as exc:
        return {"notes": [_note("LLM explanation failed", exc)]}
    return {"explanation": explanation, "explanation_model": explainer.model_name}


async def persist(state: DriftState, runtime: Runtime[DriftContext]) -> dict[str, Any]:
    """Record the check; its shape joins the baseline window for the next checks."""
    ctx = runtime.context
    endpoint = _endpoint(state, runtime)
    observation = state["observation"]
    latency = observation.latency_ms
    result = CheckResult(
        endpoint_id=endpoint.id,
        checked_at=state["checked_at"],
        status_code=observation.status_code,
        media_type=observation.media_type,
        latency_ms=round(latency, 1) if latency is not None else None,
        first_run=state.get("first_run", False),
        changes=state.get("changes", []),
        max_severity=state.get("max_severity"),
        explanation=state.get("explanation"),
        explanation_model=state.get("explanation_model"),
        error=observation.error or state.get("error"),
        notes=state.get("notes", []),
    )
    check_id = ctx.store.save_check(
        result,
        shape=observation.shape,
        sample=observation.excerpt if endpoint.store_samples else None,
    )
    ctx.store.prune(endpoint.id, keep=max(ctx.config.keep_checks, endpoint.baseline_window))
    result = result.model_copy(update={"check_id": check_id})
    wants_notification = state.get("send_notifications", False) and any(
        notifier.wants(result) for notifier in ctx.notifiers
    )
    return {"result": result, "wants_notification": wants_notification}


def after_persist(state: DriftState) -> Literal["notify", "__end__"]:
    return "notify" if state.get("wants_notification") else "__end__"  # "__end__" is END


async def notify(state: DriftState, runtime: Runtime[DriftContext]) -> dict[str, Any]:
    """Send the result to every interested notifier, in parallel."""
    ctx = runtime.context
    result = state["result"]
    targets = [notifier for notifier in ctx.notifiers if notifier.wants(result)]
    outcomes = await asyncio.gather(
        *(notifier.send(result) for notifier in targets), return_exceptions=True
    )
    failures = [
        _note(f"{notifier.name} notification failed", outcome)
        for notifier, outcome in zip(targets, outcomes, strict=True)
        if isinstance(outcome, BaseException)
    ]
    if not failures:
        return {}
    if result.check_id is not None:
        ctx.store.append_notes(result.check_id, failures)
    updated = result.model_copy(update={"notes": [*result.notes, *failures]})
    return {"result": updated, "notes": failures}
