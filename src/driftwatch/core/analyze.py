"""Turn a baseline and a fresh observation into classified changes.

This is the single entry point of the deterministic core: no network, no
database, no LLM. Everything the outer layers decide (notify, explain, fail the
CI job) is based on what ``analyze`` returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from driftwatch.core.classify import classify
from driftwatch.core.diff import diff_shapes
from driftwatch.core.models import (
    Baseline,
    Change,
    ChangeKind,
    Observation,
    RawChange,
    Severity,
    max_severity,
)
from driftwatch.core.paths import PathMatcher


@dataclass(frozen=True, slots=True)
class Rules:
    """Per-endpoint knobs that change what counts as drift."""

    ignore: PathMatcher = field(default_factory=PathMatcher)
    strict: bool = False
    overrides: Mapping[ChangeKind, Severity] = field(default_factory=dict)
    latency_factor: float | None = 3.0
    latency_floor_ms: float = 250.0
    min_latency_points: int = 3


@dataclass(frozen=True, slots=True)
class Analysis:
    """Outcome of comparing one observation with the baseline."""

    changes: list[Change]
    max_severity: Severity | None
    error: str | None
    first_run: bool


def analyze(baseline: Baseline, observation: Observation, rules: Rules | None = None) -> Analysis:
    """Compare ``observation`` with ``baseline`` and classify every difference."""
    rules = rules or Rules()
    if observation.error is not None:
        return Analysis([], None, observation.error, first_run=False)

    raw: list[RawChange] = []
    error: str | None = None
    first_run = False
    status = observation.status_code
    if status is not None and baseline.status_code is not None and status != baseline.status_code:
        raw.append(
            RawChange(
                "$",
                ChangeKind.STATUS_CHANGED,
                before=str(baseline.status_code),
                after=str(status),
            )
        )

    if status is None or not 200 <= status < 300:
        if not raw:
            error = f"unexpected HTTP status {status}"
    elif not observation.json_ok or observation.shape is None:
        if baseline.shape is not None:
            raw.append(
                RawChange(
                    "$",
                    ChangeKind.CONTENT_TYPE_CHANGED,
                    before=baseline.media_type or "application/json",
                    after=f"{observation.media_type or 'unknown'} (not valid JSON)",
                )
            )
        else:
            error = "the response body is not valid JSON"
    else:
        if baseline.shape is None:
            first_run = True
        else:
            raw.extend(diff_shapes(baseline.shape, observation.shape, ignore=rules.ignore))
        latency = _latency_change(baseline, observation, rules)
        if latency is not None:
            raw.append(latency)

    changes = [classify(change, strict=rules.strict, overrides=rules.overrides) for change in raw]
    changes.sort(key=lambda change: -change.severity.rank)
    return Analysis(
        changes=changes,
        max_severity=max_severity(change.severity for change in changes),
        error=error,
        first_run=first_run,
    )


def _latency_change(baseline: Baseline, observation: Observation, rules: Rules) -> RawChange | None:
    factor = rules.latency_factor
    before, after = baseline.latency_ms, observation.latency_ms
    if factor is None or before is None or after is None:
        return None
    if baseline.latency_points < rules.min_latency_points:
        return None
    if after <= before * factor or after - before < rules.latency_floor_ms:
        return None
    return RawChange(
        "$",
        ChangeKind.LATENCY_REGRESSION,
        before=f"{before:.0f} ms",
        after=f"{after:.0f} ms",
    )
