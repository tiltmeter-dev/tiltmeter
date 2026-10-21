"""What ``tiltmeter fix`` hands to the agent, what the agent reports, and what happened."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tiltmeter.core.models import ChangeKind, Explanation, Severity
from tiltmeter.llm.explainer import AffectedObject


class FixTarget(BaseModel):
    """One change in the responses that code reading them may have to follow."""

    model_config = ConfigDict(frozen=True)

    path: str
    kind: ChangeKind
    severity: Severity
    before: str | None = None
    after: str | None = None
    renamed_to: str | None = Field(
        default=None, description="Where a removed field probably lives on, when it was renamed."
    )
    field: str | None = Field(default=None, description="The property name at the end of path.")
    new_field: str | None = Field(
        default=None, description="The property name at the end of renamed_to."
    )


class FixRequest(BaseModel):
    """Everything the agent learns about one drift event. Never holds secrets or values."""

    model_config = ConfigDict(frozen=True)

    check_id: int
    endpoint_id: str
    method: str
    url: str
    description: str | None = None
    targets: list[FixTarget]
    affected: list[AffectedObject] = Field(default_factory=list)
    explanation: Explanation | None = None
    search_terms: list[str] = Field(default_factory=list)


class FixReport(BaseModel):
    """The agent's own account of its work. Also the structured-output schema it fills in."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="What you changed and why, in one to three sentences for the pull request."
    )
    files_changed: list[str] = Field(description="Repository paths you edited.")
    unresolved: list[str] = Field(
        description=(
            "Each place or change you could not handle safely, with the reason; "
            "empty when there is none."
        )
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="How sure you are that the edits are complete and correct."
    )


class FixPreview(BaseModel):
    """The agent's edits as a reviewable diff, before anything is committed."""

    branch: str
    base: str | None
    files: list[str]
    additions: int
    deletions: int
    diff: str
    report: FixReport
    ignored: list[str] = Field(
        default_factory=list, description="Changed files that match fix.ignore."
    )

    @property
    def empty(self) -> bool:
        return not self.files


type FixStatus = Literal["opened", "pushed", "committed", "discarded", "unchanged", "blocked"]


class FixOutcome(BaseModel):
    """How a ``tiltmeter fix`` run ended."""

    status: FixStatus
    check_id: int
    endpoint_id: str
    branch: str | None = None
    base: str | None = None
    pr_url: str | None = None
    commands: list[str] = Field(
        default_factory=list, description="What is left to run by hand, if anything."
    )
    notes: list[str] = Field(default_factory=list)
    error: str | None = None
    preview: FixPreview | None = None
