"""What ``tiltmeter fix`` hands to the agent, what the agent reports, and what happened."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tiltmeter.core.models import ChangeKind, Explanation, NoticeStatus, Severity
from tiltmeter.llm.explainer import AffectedObject
from tiltmeter.notices.models import Impact, SdkRelease, SpecInfo


class FixTarget(BaseModel):
    """One change to the responses, or to what requests may send, that code calling the API
    may have to follow."""

    model_config = ConfigDict(frozen=True)

    path: str
    kind: ChangeKind
    severity: Severity
    before: str | None = None
    after: str | None = None
    renamed_to: str | None = Field(
        default=None, description="Where a removed field probably lives on, when it was renamed."
    )
    moved_to: str | None = Field(
        default=None, description="The method and URL a removed endpoint lives on as."
    )
    replaced_by: str | None = Field(
        default=None, description="The value to send instead of a retired one."
    )
    field: str | None = Field(
        default=None, description="The property name at the end of path, in the response."
    )
    new_field: str | None = Field(
        default=None, description="The property name at the end of renamed_to."
    )
    note: str | None = Field(
        default=None, description="The provider's own words on the change, from a notice."
    )
    applies_to: str | None = Field(
        default=None,
        description="Who the change affects, when not every caller, from a notice.",
    )


type TestStatus = Literal["passed", "failed", "skipped"]


class TestRun(BaseModel):
    """The repository's tests, run on the agent's edits before they are committed."""

    __test__ = False  # not a pytest test class

    command: str
    status: TestStatus
    rounds: int = Field(default=1, description="How many times the agent edited the code.")
    output: str = Field(default="", description="The end of the test output.")
    reason: str | None = Field(default=None, description="Why the tests were skipped.")
    offline: bool = Field(default=True, description="Whether the network was cut for them.")


class FixApi(BaseModel):
    """One endpoint whose responses changed, and the changes code has to follow."""

    model_config = ConfigDict(frozen=True)

    endpoint_id: str | None = Field(default=None, description="The endpoint's id in the config.")
    method: str
    url: str
    description: str | None = None
    targets: list[FixTarget]
    affected: list[AffectedObject] = Field(default_factory=list)


class NoticeSource(BaseModel):
    """The provider's change notice a request was built from, or the spec diff behind it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["notice", "spec"] = Field(
        default="notice",
        description="notice: the provider published it; spec: Tiltmeter compared its spec.",
    )
    id: str
    provider: str
    summary: str
    published_at: str
    effective_at: str | None = None
    location: str = Field(description="The file or URL the notice was read from.")
    key: str | None = Field(
        default=None, description="The provider's key the signature was checked against."
    )
    sdks: list[SdkRelease] = Field(default_factory=list)
    spec: SpecInfo | None = None


class FixRequest(BaseModel):
    """Everything the agent learns about one API change. Never holds secrets or values.

    It comes from a stored check (``check_id``) or from a provider's notice (``notice``).
    """

    model_config = ConfigDict(frozen=True)

    check_id: int | None = None
    notice: NoticeSource | None = None
    apis: list[FixApi]
    explanation: Explanation | None = None
    search_terms: list[str] = Field(default_factory=list)
    candidate_files: list[str] = Field(
        default_factory=list, description="Files a text search found the API's use in."
    )
    test_command: str | None = Field(
        default=None, description="What Tiltmeter runs on the edits once the agent is done."
    )
    test_failure: TestRun | None = Field(
        default=None, description="The tests that failed on the agent's previous edits."
    )
    previous_report: FixReport | None = Field(
        default=None, description="What the agent reported about those edits."
    )
    commit_titles: list[str] = Field(
        default_factory=list,
        description="The repository's latest commit titles, which show its title convention.",
    )

    @property
    def targets(self) -> list[FixTarget]:
        return [target for api in self.apis for target in api.targets]

    def shown(self, total: int) -> list[list[FixTarget]]:
        """The first targets of each API, ``total`` in all, shared out evenly: what a
        prompt or a pull request lists when there are too many to show."""
        sizes = [len(api.targets) for api in self.apis]
        quota = [0] * len(sizes)
        left = total
        while left > 0 and any(q < s for q, s in zip(quota, sizes, strict=True)):
            for i, size in enumerate(sizes):
                if left > 0 and quota[i] < size:
                    quota[i] += 1
                    left -= 1
        return [api.targets[:q] for api, q in zip(self.apis, quota, strict=True)]

    @property
    def subject(self) -> str:
        """What changed, in a few words: the endpoint id or the provider."""
        if self.notice is not None:
            return self.notice.provider
        endpoint_id = self.apis[0].endpoint_id if self.apis else None
        return endpoint_id or "API"

    @property
    def source(self) -> str:
        """Where the change came from, for traces and notes."""
        if self.notice is not None and self.notice.kind == "spec":
            return f"spec diff {self.notice.id} for {self.notice.provider}"
        if self.notice is not None:
            return f"notice {self.notice.id} from {self.notice.provider}"
        return f"check #{self.check_id}"


def _every_field_required(schema: dict[str, Any]) -> None:
    # Strict structured output (Codex) needs every property listed as required, with no
    # defaults; commit_title stays optional in Python and is null when there is none.
    properties = schema.get("properties", {})
    schema["required"] = list(properties)
    for field in properties.values():
        field.pop("default", None)


class FixReport(BaseModel):
    """The agent's own account of its work. Also the structured-output schema it fills in."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_every_field_required)

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
    commit_title: str | None = Field(
        default=None,
        description=(
            "A one-line commit title in the convention the repository's contribution rules "
            "or recent commit titles show, such as 'fix(scope): ...'; null when there is none."
        ),
    )


class UnusedFile(BaseModel):
    """A changed source file that nothing else in the repository seems to import or name."""

    model_config = ConfigDict(frozen=True)

    path: str
    last_changed: str = Field(description="When a commit last changed it, in ISO 8601.")
    age_days: int = Field(description="How many days before the review that was.")


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
    tests: TestRun | None = None
    unused: list[UnusedFile] = Field(
        default_factory=list,
        description="Changed source files that nothing else in the repository imports or names.",
    )
    likely_dead: bool = Field(
        default=False,
        description=(
            "Whether the edits touch only such files, each unchanged for over 180 days, and "
            "documentation: no pull request goes out then unless asked for."
        ),
    )

    @property
    def empty(self) -> bool:
        return not self.files


type FixStatus = Literal["opened", "pushed", "committed", "discarded", "unchanged", "blocked"]


class FixOutcome(BaseModel):
    """How a ``tiltmeter fix`` run ended."""

    status: FixStatus
    check_id: int | None = None
    endpoint_id: str | None = None
    notice_id: str | None = None
    provider: str | None = None
    branch: str | None = None
    base: str | None = None
    pr_url: str | None = None
    commands: list[str] = Field(
        default_factory=list, description="What is left to run by hand, if anything."
    )
    notes: list[str] = Field(default_factory=list)
    error: str | None = None
    preview: FixPreview | None = None
    held_back: bool = Field(
        default=False,
        description="Whether the pull request was held back because the edits touch only code "
        "that looks unused.",
    )


class AppliedNotice(BaseModel):
    """What ``tiltmeter notices apply`` did about one notice."""

    provider: str
    notice_id: str
    status: NoticeStatus | Literal["discarded"]
    detail: str | None = None
    impact: Impact | None = None
    outcome: FixOutcome | None = None
