"""Data models shared by the core and the outer layers (CLI, MCP, storage, notifications)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from tiltmeter.core.paths import Segment
from tiltmeter.core.shape import Shape


class Severity(StrEnum):
    """How dangerous a change is for code or agents that read the API."""

    INFO = "info"
    WARNING = "warning"
    BREAKING = "breaking"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self.value]

    def at_least(self, other: Severity) -> bool:
        return self.rank >= other.rank


_SEVERITY_RANK: Final = {"info": 0, "warning": 1, "breaking": 2}


def max_severity(severities: Iterable[Severity | None]) -> Severity | None:
    """The most severe value, ignoring ``None``; ``None`` when there is none."""
    worst: Severity | None = None
    for severity in severities:
        if severity is not None and (worst is None or severity.rank > worst.rank):
            worst = severity
    return worst


class ChangeKind(StrEnum):
    """What kind of difference a change is."""

    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    FIELD_NOW_OPTIONAL = "field_now_optional"
    TYPE_CHANGED = "type_changed"
    NULLABLE_INTRODUCED = "nullable_introduced"
    ENUM_VALUE_ADDED = "enum_value_added"
    STATUS_CHANGED = "status_changed"
    CONTENT_TYPE_CHANGED = "content_type_changed"
    LATENCY_REGRESSION = "latency_regression"


TRANSPORT_KINDS: Final = frozenset(
    {ChangeKind.STATUS_CHANGED, ChangeKind.CONTENT_TYPE_CHANGED, ChangeKind.LATENCY_REGRESSION}
)


@dataclass(frozen=True, slots=True)
class RawChange:
    """A difference found by the diff, before severity rules are applied."""

    path: str
    kind: ChangeKind
    before: str | None = None
    after: str | None = None
    hint: str | None = None
    segments: tuple[Segment, ...] = ()
    types: frozenset[str] = frozenset()


def describe_change(
    kind: ChangeKind, path: str, before: str | None, after: str | None, hint: str | None
) -> str:
    """One readable sentence for a change."""
    subject = "The response body" if path == "$" else f"Field {path}"
    match kind:
        case ChangeKind.FIELD_ADDED:
            text = f"New field {path} ({after})."
        case ChangeKind.FIELD_REMOVED:
            text = f"Field {path} ({before}) is no longer returned."
        case ChangeKind.FIELD_NOW_OPTIONAL:
            text = f"Field {path} used to be in every response and is now {after}."
        case ChangeKind.TYPE_CHANGED:
            text = f"{subject} changed type from {before} to {after}."
        case ChangeKind.NULLABLE_INTRODUCED:
            text = f"{subject} can now be null (was {before})."
        case ChangeKind.ENUM_VALUE_ADDED:
            text = f"{subject} returned new value(s): {after}. Known values: {before}."
        case ChangeKind.STATUS_CHANGED:
            text = f"HTTP status changed from {before} to {after}."
        case ChangeKind.CONTENT_TYPE_CHANGED:
            text = f"The response is no longer JSON: content type was {before}, now {after}."
        case ChangeKind.LATENCY_REGRESSION:
            text = f"Median latency rose from {before} to {after}."
    if hint:
        text += f" {hint[0].upper()}{hint[1:]}."
    return text


class Change(BaseModel):
    """One difference between what the API used to return and what it returns now."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(
        description="Location in the response body: $ is the root and [*] means every array item."
    )
    kind: ChangeKind
    severity: Severity
    before: str | None = Field(default=None, description="What the baseline showed.")
    after: str | None = Field(default=None, description="What the latest responses show.")
    hint: str | None = Field(default=None, description="Extra evidence, such as a probable rename.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def message(self) -> str:
        """The change as one readable sentence."""
        return describe_change(self.kind, self.path, self.before, self.after, self.hint)


class StoredChange(Change):
    """A change as recorded in history."""

    id: int
    check_id: int
    endpoint_id: str
    detected_at: str


class RenameHint(BaseModel):
    """A field that disappeared and the field that most likely replaces it."""

    model_config = ConfigDict(extra="forbid")

    before_path: str = Field(description="Path of the field that disappeared.")
    after_path: str = Field(description="Path of the field that most likely replaces it.")


class Explanation(BaseModel):
    """Claude's reading of a drift event. Also the structured-output schema sent to the model."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="What changed, in one or two plain sentences.")
    likely_cause: str = Field(
        description="The most plausible reason for the change, or 'unclear' when unknown."
    )
    impact: str = Field(
        description="What breaks or behaves differently for code and agents reading the responses."
    )
    suggested_action: str = Field(
        description="The concrete next step for whoever consumes this API."
    )
    probable_renames: list[RenameHint] = Field(
        description="Removed fields that probably live on under a new path; empty when none."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="How strongly the evidence supports likely_cause."
    )


type Outcome = Literal["error", "baseline", "drift", "changed", "clean"]


class CheckResult(BaseModel):
    """Everything one check of one endpoint produced."""

    endpoint_id: str
    check_id: int | None = None
    checked_at: str
    status_code: int | None = None
    media_type: str | None = None
    latency_ms: float | None = None
    first_run: bool = Field(
        default=False, description="True when this check recorded the first baseline."
    )
    changes: list[Change] = Field(default_factory=list)
    max_severity: Severity | None = None
    explanation: Explanation | None = None
    explanation_model: str | None = None
    error: str | None = Field(
        default=None, description="Why the check could not verify the API, when it failed."
    )
    notes: list[str] = Field(
        default_factory=list, description="Non-fatal problems, such as a failed notification."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outcome(self) -> Outcome:
        """error, drift (warning or worse), changed (info only), baseline or clean."""
        if self.error:
            return "error"
        if self.max_severity is not None and self.max_severity.at_least(Severity.WARNING):
            return "drift"
        if self.changes:
            return "changed"
        if self.first_run:
            return "baseline"
        return "clean"

    def drifted(self, min_severity: Severity = Severity.WARNING) -> bool:
        return self.max_severity is not None and self.max_severity.at_least(min_severity)


class DriftStatus(BaseModel):
    """The answer to "has this API drifted recently?"."""

    endpoint_id: str
    drifted: bool = Field(
        description="True when changes at warning level or above were detected since `since`."
    )
    max_severity: Severity | None = None
    since: str
    last_checked_at: str | None = None
    last_check_error: str | None = None
    changes: list[StoredChange] = Field(default_factory=list)
    summary: str


class EndpointSummary(BaseModel):
    """A configured endpoint and how its last check went."""

    id: str
    method: str
    url: str = Field(description="Request URL without its query string.")
    description: str | None = None
    last_checked_at: str | None = None
    last_outcome: Outcome | None = None
    last_severity: Severity | None = None


class SchemaReport(BaseModel):
    """The response shape Tiltmeter has learned for an endpoint."""

    endpoint_id: str
    observations: int = Field(description="How many responses the baseline is built from.")
    baseline_checks: int = Field(description="How many recent checks were merged.")
    json_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the response body; null before the first check."
    )


@dataclass(frozen=True, slots=True)
class Observation:
    """What one round of requests to an endpoint returned."""

    status_code: int | None = None
    media_type: str | None = None
    latency_ms: float | None = None
    json_ok: bool = False
    shape: Shape | None = None
    samples: int = 0
    error: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class Baseline:
    """What Tiltmeter learned about an endpoint from its recent history."""

    shape: Shape | None = None
    status_code: int | None = None
    media_type: str | None = None
    latency_ms: float | None = None
    latency_points: int = 0
    checks: int = 0
