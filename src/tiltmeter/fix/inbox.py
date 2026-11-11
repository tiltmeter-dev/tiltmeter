"""``tiltmeter prs``: which of the pull requests Tiltmeter opened need you?

A fix that gets a review and no answer stays open, and whoever opened dozens of them has
to look through every one for a maintainer's comment, a failed check or a branch its base
left behind. This module does the looking. It asks GitHub's search for the pull requests
whose description ends with the footer :func:`tiltmeter.fix.runner.pull_request_body`
writes, reads the facts of the open ones with GraphQL, a few per request, and says what
each one waits for. The search only nominates: a description that merely quotes the footer
is left out.

It only reads. Nothing is commented, pushed, merged, closed or rerun on GitHub: answering
is ``tiltmeter fix --follow-up``, which a person runs. Titles, logins, check names and
comments are third-party text: they are made printable here, cut short, and the terminal
shows them as plain text.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from subprocess import CompletedProcess
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tiltmeter.errors import FixError, InboxError
from tiltmeter.fix._proc import failure_reason, run_tool
from tiltmeter.fix.git import _FAILED
from tiltmeter.fix.runner import FOOTER, GENERATED_BY
from tiltmeter.notices.hunt import FULL_NAME, GITHUB, NO_CLI, NOT_LOGGED_IN, _moment
from tiltmeter.notices.models import printable
from tiltmeter.notices.reader import bundled_feed
from tiltmeter.notices.scan import shell_join

PER_PAGE: Final = 100
# GitHub serves the first 1,000 results of a search and no more.
MAX_RESULTS: Final = 1000
# Pull requests per GraphQL request. Whether one merges and how its checks went are slow
# for GitHub to work out, and a request that takes over ten seconds is cut off.
BATCH: Final = 10
# The latest comments and the latest reviews read of each pull request.
MAX_VOICES: Final = 30
MAX_CHECKS: Final = 50
MAX_FAILED_CHECKS: Final = 10
MAX_TITLE: Final = 100
MAX_EXCERPT: Final = 140
GH_TIMEOUT: Final = 60.0
# GitHub works out whether a branch merges in the background, once someone asks. This is
# how long it gets before the pull requests it had no answer for are asked about again.
MERGE_PAUSE: Final = 3.0

# A login as GitHub gives them. It becomes part of a search query, so nothing else gets
# through: no space, no colon, no quote.
LOGIN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,38}")
# What stands for the notice in a command, when the footer does not name one Tiltmeter has.
NOTICE: Final = "NOTICE"

RATE_LIMITED: Final = "GitHub's rate limit was reached; wait a minute and run it again"

type PullState = Literal["open", "merged", "closed"]
type StateFilter = Literal["open", "merged", "closed", "all"]
type Waiting = Literal[
    "changes_requested", "answer", "checks_failed", "behind_or_conflicting", "ready", "maintainers"
]
type Decision = Literal["changes_requested", "approved", "review_required"]
type Checks = Literal["passed", "failed", "pending"]
type VoiceKind = Literal["comment", "review"]
type Stance = Literal["approved", "changes_requested", "commented", "dismissed"]
type ChangeKind = Literal["check", "catalog", "unsigned", "signed"]
type Progress = Callable[[str], None]
type Sleep = Callable[[float], None]

# What an open pull request can wait for, the surest sign first. The first four are the
# author's move; the last two are the maintainers'.
WAITING: Final[tuple[Waiting, ...]] = (
    "changes_requested",
    "answer",
    "checks_failed",
    "behind_or_conflicting",
    "ready",
    "maintainers",
)
NEEDS_YOU: Final[frozenset[Waiting]] = frozenset(WAITING[:4])
# What ``tiltmeter fix --follow-up`` can take up; a branch to update is not among them.
ANSWERABLE: Final[frozenset[Waiting]] = frozenset(WAITING[:3])


# -- the report -------------------------------------------------------------------


class InboxFilters(BaseModel):
    """Which pull requests to look for."""

    # Python's own engine: the pattern of a repository's name looks ahead.
    model_config = ConfigDict(frozen=True, regex_engine="python-re")

    author: str | None = Field(
        default=None,
        pattern=rf"\A{LOGIN.pattern}\Z",
        description="Whose pull requests; null for the login the GitHub CLI holds.",
    )
    state: StateFilter = "all"
    repositories: list[Annotated[str, Field(pattern=rf"\A{FULL_NAME.pattern}\Z")]] = Field(
        default_factory=list, description="Only pull requests to these, as OWNER/NAME."
    )
    limit: int = Field(
        default=200, ge=1, le=MAX_RESULTS, description="Read at most this many, the newest first."
    )


class LastWord(BaseModel):
    """The latest thing a person other than the author said on a pull request."""

    author: str
    role: str = Field(
        description=(
            "Their tie to the repository, as GitHub gives it: owner, member, collaborator, "
            "contributor, none and a few more."
        )
    )
    kind: VoiceKind
    stance: Stance | None = Field(default=None, description="For a review, how it ended.")
    at: AwareDatetime
    excerpt: str = Field(description="The start of what they wrote, on one line; may be empty.")


class InboxPull(BaseModel):
    """A pull request Tiltmeter opened and, when it is open, what it waits for."""

    repository: str
    number: int
    title: str
    url: str
    author: str
    state: PullState
    draft: bool = False
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = Field(
        default=None, description="When it was merged or closed."
    )
    waits_for: Waiting | None = Field(
        default=None,
        description=(
            "What an open pull request waits for, the surest sign first. changes_requested: "
            "a review asked for changes and no commit followed; answer: a person other than "
            "the author, and not a bot, commented or reviewed after the author's last "
            "comment and last commit; checks_failed: on its latest commit; "
            "behind_or_conflicting: with its base branch; ready: approved with no check "
            "failed or running; maintainers: none of those. Null for one that is merged, "
            "closed or could not be read."
        ),
    )
    needs_you: bool = Field(
        default=False,
        description=(
            "It waits for changes_requested, answer, checks_failed or behind_or_conflicting."
        ),
    )
    detail: str | None = Field(
        default=None,
        description=(
            "More on it. For one that needs you: the branch it conflicts with or is behind, "
            "whatever else it waits for. For the others: where the reviews and checks stand."
        ),
    )
    last_word: LastWord | None = None
    failed_checks: list[str] = Field(
        default_factory=list, description=f"At most {MAX_FAILED_CHECKS} names."
    )
    mergeable: bool | None = Field(
        default=None,
        description="Whether it merges without conflicts; null when GitHub has not worked it out.",
    )
    change: str | None = Field(
        default=None,
        description="The notice id or the check id its footer says the fix was made from.",
    )
    follow_up: str | None = Field(
        default=None,
        description=(
            "The command that answers it, for one that waits for changes_requested, answer "
            f"or checks_failed. {NOTICE} stands for the notice when the footer names none "
            "that Tiltmeter's catalog has."
        ),
    )
    error: str | None = Field(default=None, description="Why an open one could not be read.")


class InboxCounts(BaseModel):
    """How many pull requests were found, by state."""

    opened: int = Field(default=0, description="All of them: merged, closed and open.")
    merged: int = 0
    closed: int = Field(default=0, description="Closed without a merge.")
    open: int = 0
    need_you: int = Field(default=0, description="Open ones that wait for their author.")
    unread: int = Field(default=0, description="Open ones GitHub did not describe.")


class InboxReport(BaseModel):
    """What ``tiltmeter prs`` found."""

    filters: InboxFilters
    query: str = Field(description="The search as GitHub received it.")
    found: int = Field(default=0, description="Results GitHub counted for it.")
    not_read: int = Field(default=0, description="Results past --limit, which were not read.")
    unrelated: int = Field(
        default=0,
        description="Results left out: their description does not end with Tiltmeter's footer.",
    )
    incomplete: bool = Field(
        default=False, description="GitHub's search timed out, so results are missing."
    )
    counts: InboxCounts = Field(default_factory=InboxCounts)
    pulls: list[InboxPull] = Field(
        default_factory=list,
        description=(
            "Those that need you first, in the order of waits_for, then the other open ones, "
            "then merged and closed ones; within each, the latest update first."
        ),
    )
    error: str | None = Field(default=None, description="Why GitHub could not be asked.")


# -- what a pull request was made from --------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """What a fix was made from, as the footer of its pull request names it."""

    kind: ChangeKind
    name: str


_ID: Final = r"[A-Za-z0-9][A-Za-z0-9._\-]{0,99}"
# The ends pull_request_body gives the footer, each with the id it names.
_ORIGINS: Final[tuple[tuple[ChangeKind, re.Pattern[str]], ...]] = (
    ("check", re.compile(r" with `tiltmeter fix (\d{1,9})`\. ")),
    ("catalog", re.compile(rf" from `({_ID})` in its catalog of known API changes\. ")),
    ("unsigned", re.compile(rf" from the unsigned notice `({_ID})` about ")),
    ("signed", re.compile(rf" from \S{{1,253}}'s change notice `({_ID})`\. ")),
)


def footer_of(body: str) -> str | None:
    """What follows Tiltmeter's footer in a pull request's description, or None when no
    line starts with it. A description that quotes the footer mid-sentence is not one of
    Tiltmeter's."""
    ends = [line for line in body.splitlines() if line.startswith(FOOTER)]
    return ends[-1][len(FOOTER) :] if ends else None


def change_of(footer: str) -> Change | None:
    """The check or the notice a footer says the fix was made from. None for a spec diff,
    which has no id, and for a footer someone rewrote."""
    for kind, pattern in _ORIGINS:
        if (named := pattern.match(footer)) is not None:
            return Change(kind, named[1])
    return None


@cache
def _catalog_ids() -> frozenset[str]:
    entries = [item for item in bundled_feed() if isinstance(item, dict)]
    return frozenset(name for item in entries if isinstance(name := item.get("id"), str))


def follow_up_command(url: str, change: Change | None) -> str:
    """``tiltmeter fix --follow-up`` for one pull request, as docs/fix.md spells it: the
    change the fix was made from, then the pull request.

    A check is named by its id. A notice is named when the bundled catalog has an entry
    with its id, which ``fix --notice`` takes as it is; otherwise :data:`NOTICE` stands
    for the file or URL it was read from, which a footer does not say. The id comes from
    a description anyone may have edited: it is matched against the shape of an id, and
    the command is quoted for the shell it will be pasted in.
    """
    if change is not None and change.kind == "check":
        return shell_join(["tiltmeter", "fix", change.name, "--follow-up", url])
    notice, unsigned = NOTICE, False
    if change is not None and change.kind in ("catalog", "unsigned"):
        unsigned = True
        if change.name in _catalog_ids():
            notice = change.name
    command = ["tiltmeter", "fix", "--notice", notice]
    if unsigned:
        command.append("--allow-unsigned")
    return shell_join([*command, "--follow-up", url])


# -- GitHub -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Found:
    """A pull request as the search shows it."""

    repository: str
    number: int
    title: str
    author: str
    state: PullState
    footer: str | None = None
    draft: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def url(self) -> str:
        return f"https://{GITHUB}/{self.repository}/pull/{self.number}"


@dataclass(frozen=True, slots=True)
class FoundPage:
    """One page of search results, in GitHub's order."""

    total: int
    pulls: Sequence[Found]
    results: int
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class Voice:
    """Something said on a pull request: a comment, or a review with how it ended."""

    author: str
    at: datetime
    kind: VoiceKind = "comment"
    bot: bool = False
    role: str = "none"
    stance: Stance | None = None
    text: str = ""


@dataclass(frozen=True, slots=True)
class PullFacts:
    """What telling who a pull request waits for takes, as GitHub gives it."""

    state: PullState = "open"
    draft: bool = False
    base: str | None = None
    decision: Decision | None = None
    voices: Sequence[Voice] = ()
    committed_at: datetime | None = None
    checks: Checks | None = None
    failed_checks: Sequence[str] = ()
    mergeable: bool | None = None
    behind: bool = False


class PullSource(Protocol):
    """Where pull requests are searched and described."""

    def search(self, query: str, *, page: int) -> FoundPage:
        """One page of the pull requests that match ``query``, the newest first. Raises
        :class:`InboxError` when GitHub cannot be asked."""
        ...

    def describe(self, pulls: Sequence[Found]) -> list[PullFacts | None]:
        """The facts of each of these pull requests, in their order; None for one GitHub
        no longer shows."""
        ...


class GhPulls:
    """GitHub through its CLI, which already holds the user's login. Only ``gh api``
    requests that read: an issue search, and a GraphQL query for the facts of the open
    pull requests it found."""

    def search(self, query: str, *, page: int) -> FoundPage:
        fields = [f"q={query}", "sort=created", "order=desc"]
        fields += [f"per_page={PER_PAGE}", f"page={page}"]
        completed = _gh("-X", "GET", "search/issues", *(arg for f in fields for arg in ("-f", f)))
        if completed.returncode != 0:
            raise _failure(completed, "search/issues")
        return parse_search(_json(completed, "search/issues"))

    def describe(self, pulls: Sequence[Found]) -> list[PullFacts | None]:
        facts: list[PullFacts | None] = []
        batches = deque(pulls[start : start + BATCH] for start in range(0, len(pulls), BATCH))
        while batches:
            batch = batches.popleft()
            completed = _gh("graphql", "-f", f"query={facts_query(batch)}")
            if _cut_off(completed) and len(batch) > 1:
                # Too much to work out in one request: ask for each half on its own.
                half = len(batch) // 2
                batches.extendleft((batch[half:], batch[:half]))
                continue
            answer = _json(completed, "graphql")
            # A pull request that is gone makes gh fail, with the others in its answer.
            partial = isinstance(answer, dict) and isinstance(answer.get("data"), dict)
            if completed.returncode != 0 and (_rate_limited(completed) or not partial):
                raise _failure(completed, "graphql")
            facts += parse_facts(answer, len(batch))
        return facts


def _gh(*args: str) -> CompletedProcess[str]:
    """``gh api`` on github.com, whatever host the user's ``gh`` defaults to."""
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    command = ["gh", "api", "--hostname", GITHUB, *args]
    try:
        return run_tool(command, cwd=Path.cwd(), env=env, timeout=GH_TIMEOUT)
    except FixError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            raise InboxError(NO_CLI) from exc
        raise InboxError(f"{exc}; check the connection and run it again") from exc


def _said(completed: CompletedProcess[str]) -> str:
    """What gh and GitHub said about a request that failed: gh's own words and the error
    messages of the answer. Never the rest of the answer, which quotes what people wrote
    on pull requests."""
    said = [completed.stderr]
    try:
        answer = json.loads(completed.stdout)
    except ValueError:
        answer = None
    if isinstance(answer, dict):
        errors = answer.get("errors")
        problems = [answer, *(errors if isinstance(errors, list) else [])]
        for problem in (item for item in problems if isinstance(item, dict)):
            said += [_text(problem.get("type")).replace("_", " "), _text(problem.get("message"))]
    return "\n".join(said).lower()


def _rate_limited(completed: CompletedProcess[str]) -> bool:
    said = _said(completed)
    return "rate limit" in said or "http 429" in said


def _cut_off(completed: CompletedProcess[str]) -> bool:
    """Whether GitHub gave up on a request that took too long, as a gateway error or as
    an error in the answer."""
    said = _said(completed)
    gave_up = ("http 502", "http 504", "timeout", "timed out")
    return completed.returncode != 0 and any(sign in said for sign in gave_up)


def _refusal(completed: CompletedProcess[str]) -> str | None:
    """Why GitHub refused a search, in its own words."""
    try:
        answer = json.loads(completed.stdout)
    except ValueError:
        return None
    errors = answer.get("errors") if isinstance(answer, dict) else None
    first = errors[0] if isinstance(errors, list) and errors else None
    message = first.get("message") if isinstance(first, dict) else None
    return message if isinstance(message, str) else None


def _failure(completed: CompletedProcess[str], what: str) -> InboxError:
    """Why ``gh api`` failed, and what to do about it."""
    said = _said(completed)
    reason = printable(failure_reason(completed))
    if _rate_limited(completed):
        return InboxError(RATE_LIMITED)
    # gh exits with 4 when it has no login at all.
    if completed.returncode == 4 or "http 401" in said or "gh auth login" in said:
        return InboxError(NOT_LOGGED_IN)
    if "http 422" in said:
        # An author or a repository that does not exist, or that this login cannot see.
        why = printable(_refusal(completed) or reason).rstrip(".")
        return InboxError(f"GitHub refused the search ({why}); check --author and --repo")
    if _cut_off(completed):
        return InboxError(f"`gh api {what}` failed: {reason}; run it again in a moment")
    return InboxError(f"`gh api {what}` failed: {reason}")


def _json(completed: CompletedProcess[str], what: str) -> Any:
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        if completed.returncode != 0:
            raise _failure(completed, what) from exc
        raise InboxError(f"`gh api {what}` answered something that is not JSON") from exc


_API_REPOS: Final = "https://api.github.com/repos/"
_STATES: Final[dict[StateFilter, tuple[str, ...]]] = {
    "open": ("is:open",),
    "merged": ("is:merged",),
    "closed": ("is:closed", "is:unmerged"),
    "all": (),
}


def search_query(filters: InboxFilters) -> str:
    """What to ask the issue search: the pull requests by the author whose description
    spells the start of Tiltmeter's footer, narrowed to a state and to repositories.
    Several ``repo:`` are any of them."""
    terms = ["is:pr", f"author:{filters.author or '@me'}", "in:body", f'"{GENERATED_BY}"']
    terms += _STATES[filters.state]
    terms += [f"repo:{name}" for name in filters.repositories]
    return " ".join(terms)


def parse_search(data: Any) -> FoundPage:
    """A page of ``search/issues`` as GitHub answers it. A result that is not a pull
    request, or whose repository is not named as GitHub names them, is left out."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InboxError("GitHub answered the search with something unexpected")
    return FoundPage(
        total=_count(data.get("total_count")),
        pulls=[pull for item in items if (pull := _found(item)) is not None],
        results=len(items),
        incomplete=data.get("incomplete_results") is True,
    )


def _found(item: Any) -> Found | None:
    if not isinstance(item, dict) or not isinstance(link := item.get("pull_request"), dict):
        return None
    api, number = item.get("repository_url"), _count(item.get("number"))
    name = api.removeprefix(_API_REPOS) if isinstance(api, str) else ""
    if not FULL_NAME.fullmatch(name) or not number:
        return None
    user = item.get("user")
    merged = _moment(link.get("merged_at"))
    state: PullState = "open" if item.get("state") == "open" else "merged" if merged else "closed"
    return Found(
        repository=name,
        number=number,
        title=_text(item.get("title")),
        author=_text(user.get("login") if isinstance(user, dict) else None) or "ghost",
        state=state,
        footer=footer_of(_text(item.get("body"))),
        draft=item.get("draft") is True,
        created_at=_moment(item.get("created_at")),
        updated_at=_moment(item.get("updated_at")),
        closed_at=merged or _moment(item.get("closed_at")),
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _nodes(value: Any) -> list[dict[str, Any]]:
    """The nodes of a GraphQL connection that are objects."""
    nodes = value.get("nodes") if isinstance(value, dict) else None
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


_SAID: Final = "author { login __typename } authorAssociation"
_FACTS: Final = (
    "fragment facts on PullRequest { state isDraft baseRefName reviewDecision mergeable "
    "mergeStateStatus latestOpinionatedReviews(first: 20, writersOnly: true) "
    "{ nodes { state } } "
    f"comments(last: {MAX_VOICES}) {{ nodes {{ {_SAID} createdAt isMinimized bodyText }} }} "
    f"reviews(last: {MAX_VOICES}) {{ nodes {{ {_SAID} submittedAt state bodyText "
    "comments(first: 1) { nodes { bodyText } } } } "
    "commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state "
    f"contexts(first: {MAX_CHECKS}) {{ nodes {{ __typename ... on CheckRun "
    "{ name conclusion } ... on StatusContext { context state } } } } } } } }"
)


def facts_query(pulls: Sequence[Found]) -> str:
    """One GraphQL query for the facts of many pull requests, each under its own alias."""
    asked = []
    for position, pull in enumerate(pulls):
        owner, _, name = pull.repository.partition("/")
        asked.append(
            f"p{position}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            f"{{ pullRequest(number: {pull.number}) {{ ...facts }} }}"
        )
    return f"{_FACTS}\nquery {{\n" + "\n".join(asked) + "\n}"


def parse_facts(data: Any, count: int) -> list[PullFacts | None]:
    """The pull requests in a :func:`facts_query` answer for ``count`` of them, in the
    order asked. One that is gone, or that the login cannot see, comes back empty and is
    None."""
    found = data.get("data") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        raise InboxError("GitHub answered the pull requests' facts with something unexpected")
    return [_facts(found.get(f"p{position}")) for position in range(count)]


def _facts(node: Any) -> PullFacts | None:
    pull = node.get("pullRequest") if isinstance(node, dict) else None
    if not isinstance(pull, dict):
        return None
    said = [_voice(comment, "comment") for comment in _nodes(pull.get("comments"))]
    said += [_voice(review, "review") for review in _nodes(pull.get("reviews"))]
    head = next(iter(_nodes(pull.get("commits"))), {}).get("commit")
    commit = head if isinstance(head, dict) else {}
    checks, failed = _checks(commit.get("statusCheckRollup"))
    state = _text(pull.get("state")).lower()
    mergeable, merge_state = pull.get("mergeable"), pull.get("mergeStateStatus")
    base = pull.get("baseRefName")
    return PullFacts(
        state="merged" if state == "merged" else "closed" if state == "closed" else "open",
        draft=pull.get("isDraft") is True,
        base=printable(base, 100) if isinstance(base, str) and base else None,
        decision=_decision(pull),
        voices=tuple(
            sorted((voice for voice in said if voice is not None), key=lambda voice: voice.at)
        ),
        committed_at=_moment(commit.get("committedDate")),
        checks=checks,
        failed_checks=tuple(failed),
        # DIRTY is the merge box saying the same as CONFLICTING.
        mergeable=(
            False
            if mergeable == "CONFLICTING" or merge_state == "DIRTY"
            else True
            if mergeable == "MERGEABLE"
            else None
        ),
        behind=merge_state == "BEHIND",
    )


_STANCES: Final[dict[str, Stance]] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}
# Accounts GitHub lists as users that only ever post for a service.
_MACHINES: Final = frozenset({"claassistant", "codecov-commenter", "coveralls", "googlebot"})
_BOT_NAME: Final = re.compile(r"(?:\[bot\]|[-_](?:ro)?bot)$", re.IGNORECASE)


def is_bot(login: str, kind: str | None = None) -> bool:
    """Whether an account speaks for a program: GitHub says so, its name ends in "bot",
    or it is one of a few services that post as a user."""
    return kind == "Bot" or bool(_BOT_NAME.search(login)) or login.lower() in _MACHINES


def _voice(item: dict[str, Any], kind: VoiceKind) -> Voice | None:
    """One comment or review, unless it is hidden, still a draft or nobody's."""
    author = item.get("author")
    login = _text(author.get("login") if isinstance(author, dict) else None)
    at = _moment(item.get("submittedAt" if kind == "review" else "createdAt"))
    stance = _STANCES.get(_text(item.get("state"))) if kind == "review" else None
    if not login or at is None or item.get("isMinimized") or (kind == "review" and not stance):
        return None
    text = _text(item.get("bodyText")).strip()
    if not text and kind == "review":
        # A review made of notes on lines: the first one says what it is about.
        text = _text(next(iter(_nodes(item.get("comments"))), {}).get("bodyText"))
    return Voice(
        author=printable(login, 40),
        at=at,
        kind=kind,
        bot=is_bot(login, _text(author.get("__typename")) if isinstance(author, dict) else None),
        role=printable(_text(item.get("authorAssociation")).lower().replace("_", " "), 40)
        or "none",
        stance=stance,
        text=printable(text, MAX_EXCERPT),
    )


def _decision(pull: dict[str, Any]) -> Decision | None:
    """Where the reviews stand. GitHub decides only for a branch that requires reviews;
    for the others, the latest review of each person who can merge says as much."""
    decided = pull.get("reviewDecision")
    stands = {node.get("state") for node in _nodes(pull.get("latestOpinionatedReviews"))}
    if decided == "CHANGES_REQUESTED" or (decided is None and "CHANGES_REQUESTED" in stands):
        return "changes_requested"
    if decided == "APPROVED" or (decided is None and "APPROVED" in stands):
        return "approved"
    return "review_required" if decided == "REVIEW_REQUIRED" else None


def _checks(rollup: Any) -> tuple[Checks | None, list[str]]:
    """How the checks of a commit went, and the names of those that failed: the ones
    ``tiltmeter fix --follow-up`` would hand to the agent. None when no check ran."""
    if not isinstance(rollup, dict):
        return None, []
    failed: dict[str, None] = {}
    for check in _nodes(rollup.get("contexts")):
        verdict = _text(check.get("conclusion") or check.get("state")).upper()
        name = printable(_text(check.get("name") or check.get("context")), 60)
        if name and verdict in _FAILED:
            failed.setdefault(name)
    state = rollup.get("state")
    if failed or state in ("FAILURE", "ERROR"):
        return "failed", list(failed)[:MAX_FAILED_CHECKS]
    if state == "SUCCESS":
        return "passed", []
    return ("pending" if state in ("PENDING", "EXPECTED") else None), []


# -- what a pull request waits for ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Waits:
    """What an open pull request waits for, and the last person to speak on it."""

    waits_for: Waiting
    detail: str | None = None
    last_word: Voice | None = None


def classify(author: str, facts: PullFacts) -> Waits:
    """What an open pull request waits for, the surest sign first.

    1. changes requested: the reviews stand at "changes requested" and no commit followed
       the latest review that asked. A commit is the answer to such a review, so one that
       came after it hands the pull request back to the reviewer.
    2. an answer: a person other than ``author``, and not a bot, commented or reviewed
       after the author's last comment and the last commit. An approval asks nothing.
    3. checks failed on the latest commit.
    4. behind its base branch, where the base requires it up to date, or conflicting.
    5. ready: approved, with no check failed or still running.
    6. the maintainers, otherwise.

    The first four are the author's move, and a branch to update is said with the first
    three too, since answering them does not update it. Only what GitHub gave is read: a
    pure function of ``facts``.
    """
    own = author.lower()
    people = [voice for voice in facts.voices if not voice.bot and voice.author.lower() != own]
    last = people[-1] if people else None
    base = facts.base or "its base branch"
    branch = None
    if facts.mergeable is False:
        branch = f"conflicts with {base}"
    elif facts.behind:
        branch = f"behind {base}"
    pushed = False
    if facts.decision == "changes_requested":
        asked = [voice.at for voice in facts.voices if voice.stance == "changes_requested"]
        pushed = bool(asked) and facts.committed_at is not None and facts.committed_at > max(asked)
        if not pushed:
            return Waits("changes_requested", branch, last)
    answers = [voice.at for voice in facts.voices if voice.author.lower() == own]
    answers += [facts.committed_at] if facts.committed_at is not None else []
    answered = max(answers, default=None)
    unanswered = [
        voice
        for voice in people
        if voice.stance != "approved" and (answered is None or voice.at > answered)
    ]
    if unanswered:
        return Waits("answer", branch, unanswered[-1])
    if facts.checks == "failed":
        return Waits("checks_failed", branch, last)
    if branch is not None:
        return Waits("behind_or_conflicting", branch, last)
    running = facts.checks == "pending"
    if facts.decision == "approved":
        if running:
            return Waits("maintainers", "approved; checks are running", last)
        return Waits(
            "ready", "approved, " + ("checks passed" if facts.checks else "no checks"), last
        )
    if pushed:
        return Waits("maintainers", "changes were requested; a commit followed", last)
    if running:
        return Waits("maintainers", "checks are running", last)
    return Waits("maintainers", "review required" if facts.decision else None, last)


# -- search, read, sort -----------------------------------------------------------


def _quiet(_: str) -> None:
    return None


def inbox(
    source: PullSource,
    *,
    filters: InboxFilters | None = None,
    progress: Progress = _quiet,
    sleep: Sleep = time.sleep,
) -> InboxReport:
    """Find the pull requests Tiltmeter opened and say which ones need their author.

    Two stages: ask the search for the pull requests that carry the footer, then read the
    facts of the open ones and tell what each waits for. ``progress`` hears what is going
    on, for whoever prints, and ``sleep`` waits before the ones GitHub could not say
    whether they merge are asked about once more.
    """
    chosen = filters or InboxFilters()
    report = InboxReport(filters=chosen, query=search_query(chosen))
    try:
        found = _search(source, report, progress)
        waiting = [pull for pull in found if pull.state == "open"]
        if waiting:
            progress(f"reading {_pulls(len(waiting), 'open')}, {BATCH} per request")
        facts = source.describe(waiting) if waiting else []
    except InboxError as exc:
        report.error = printable(str(exc))
        return report
    _ask_again(source, waiting, facts, progress, sleep)
    described = dict(zip(((pull.repository, pull.number) for pull in waiting), facts, strict=True))
    pulls = [_pull(pull, described.get((pull.repository, pull.number))) for pull in found]
    report.pulls = sorted(pulls, key=_order)
    counts = report.counts
    counts.opened = len(pulls)
    counts.merged = sum(1 for pull in pulls if pull.state == "merged")
    counts.closed = sum(1 for pull in pulls if pull.state == "closed")
    counts.open = counts.opened - counts.merged - counts.closed
    counts.need_you = sum(1 for pull in pulls if pull.needs_you)
    counts.unread = sum(1 for pull in pulls if pull.error is not None)
    return report


def _ask_again(
    source: PullSource,
    pulls: Sequence[Found],
    facts: list[PullFacts | None],
    progress: Progress,
    sleep: Sleep,
) -> None:
    """Read once more the open pull requests GitHub could not yet say merge or not, and
    put what it says now in ``facts``. Asking is what makes GitHub work it out, so the
    first answer is often "unknown", and a conflict would go unseen. When the second read
    fails, the first stands."""
    unsure = [
        position
        for position, known in enumerate(facts)
        if known is not None and known.state == "open" and known.mergeable is None
    ]
    if not unsure:
        return
    they = "it merges" if len(unsure) == 1 else "they merge"
    progress(f"reading {_pulls(len(unsure))} again: GitHub had not worked out whether {they}")
    sleep(MERGE_PAUSE)
    with suppress(InboxError):
        again = source.describe([pulls[position] for position in unsure])
        for position, known in zip(unsure, again, strict=True):
            if known is not None:
                facts[position] = known


def _pulls(count: int, what: str = "") -> str:
    return f"{count:,} {what + ' ' if what else ''}pull request{'' if count == 1 else 's'}"


def _search(source: PullSource, report: InboxReport, progress: Progress) -> list[Found]:
    """The pull requests the search names that carry Tiltmeter's footer, the newest
    first and each once, up to the limit. What was left out is written to the report."""
    limit = report.filters.limit
    found: dict[tuple[str, int], Found] = {}
    read = page = 0
    progress("searching GitHub for the pull requests Tiltmeter opened")
    while True:
        page += 1
        answer = source.search(report.query, page=page)
        report.found = max(report.found, answer.total)
        report.incomplete = report.incomplete or answer.incomplete
        taken = 0
        for pull in answer.pulls:
            if len(found) >= limit:
                break
            taken += 1
            if pull.footer is None:
                report.unrelated += 1
            else:
                found.setdefault((pull.repository.lower(), pull.number), pull)
        whole = taken == len(answer.pulls)
        read += answer.results if whole else taken
        more = answer.results >= PER_PAGE and page * PER_PAGE < min(answer.total, MAX_RESULTS)
        if not whole or not more or len(found) >= limit:
            break
    report.not_read = max(report.found - read, 0)
    return list(found.values())


def _pull(found: Found, facts: PullFacts | None) -> InboxPull:
    """One pull request of the report: what the search said, and for an open one what
    its facts add."""
    change = change_of(found.footer or "")
    pull = InboxPull(
        repository=found.repository,
        number=found.number,
        title=printable(found.title, MAX_TITLE),
        url=found.url,
        author=printable(found.author, 40),
        state=found.state,
        draft=found.draft,
        created_at=found.created_at,
        updated_at=found.updated_at,
        closed_at=found.closed_at,
        change=change.name if change is not None else None,
    )
    if found.state != "open":
        return pull
    if facts is None:
        pull.error = "GitHub did not describe it: it may be gone, or hidden from this login"
        return pull
    pull.state, pull.draft = facts.state, facts.draft
    if facts.state != "open":
        return pull  # merged or closed since the search last indexed it
    waits = classify(found.author, facts)
    # Whoever the source got them from, other people's words are made printable here.
    pull.waits_for = waits.waits_for
    pull.detail = printable(waits.detail) if waits.detail is not None else None
    pull.needs_you = waits.waits_for in NEEDS_YOU
    pull.failed_checks = [printable(name, 60) for name in facts.failed_checks]
    pull.mergeable = facts.mergeable
    if (said := waits.last_word) is not None:
        pull.last_word = LastWord(
            author=printable(said.author, 40),
            role=printable(said.role, 40),
            kind=said.kind,
            stance=said.stance,
            at=said.at,
            excerpt=printable(said.text, MAX_EXCERPT),
        )
    if waits.waits_for in ANSWERABLE:
        pull.follow_up = follow_up_command(found.url, change)
    return pull


def _order(pull: InboxPull) -> tuple[int, float]:
    """Where a pull request goes in the report: by what it waits for, then by state, and
    the latest update first."""
    if pull.waits_for is not None:
        rank = WAITING.index(pull.waits_for)
    else:
        rank = len(WAITING) + ("open", "merged", "closed").index(pull.state)
    return rank, -(pull.updated_at.timestamp() if pull.updated_at is not None else 0.0)
