"""``tiltmeter hunt``: which public repositories still use what a notice changes?

A provider that retires a model or removes an endpoint wants to know who still calls it,
and so does anyone about to propose the fix upstream. The hunt asks GitHub's code search
for what the notice changes, ranks the repositories it names, clones the most starred ones
and runs the search behind ``tiltmeter scan`` (:mod:`tiltmeter.notices.scan`) on each
clone. The code search only nominates: its hits sit in docs, tests and comments as often
as in code, so the clone decides.

It only reads. Nothing is forked, pushed or opened on GitHub, and nothing is written
outside the folder the clones go to. What GitHub answers is third-party text: repository
names are checked against the shape GitHub gives them, the rest is made printable, and the
terminal shows all of it as plain text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tiltmeter.catalog import from_catalog
from tiltmeter.core.models import ChangeKind
from tiltmeter.errors import FixError, HuntError, TiltmeterError
from tiltmeter.fix._proc import failure_reason, run_tool
from tiltmeter.fix.git import Git
from tiltmeter.fix.request import field_name, is_target, reads_response
from tiltmeter.notices.impact import _DISTINCTIVE_SEGMENTS, _VERSION, import_pattern
from tiltmeter.notices.models import (
    ApiChangeNotice,
    ApiVersionPin,
    NoticeEndpoint,
    SdkPin,
    https_host,
    printable,
)
from tiltmeter.notices.reader import LoadedNotice
from tiltmeter.notices.scan import Catalog, ScanMatch, days_until, headline, read_folder, scan

GITHUB: Final = "github.com"
PER_PAGE: Final = 100
# GitHub allows about 10 code searches a minute; one every 7 seconds never makes 10.
SEARCH_PAUSE: Final = 7.0
# Three and a half minutes of searching at that pace, for all the queries together.
MAX_REQUESTS: Final = 30
MAX_PAGES: Final = 5
# GitHub refuses longer queries.
MAX_QUERY_CHARS: Final = 256
BATCH: Final = 50
# GitHub counts a repository's size in kilobytes, history included.
MAX_SIZE_KB: Final = 500_000
MAX_SKIPPED: Final = 20
GH_TIMEOUT: Final = 60.0
CLONE_TIMEOUT: Final = 300.0

# An owner and a repository as GitHub names them. The name becomes a folder and part of a
# URL, so nothing else gets through: no "..", no slash, no space.
FULL_NAME: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}/(?!\.{1,2}$)[A-Za-z0-9_.\-]{1,100}"
)

RATE_LIMITED: Final = (
    "GitHub's rate limit was reached (code search allows about 10 requests a minute); "
    "wait a minute and run it again"
)
NOT_LOGGED_IN: Final = (
    "the GitHub CLI is not logged in, or GitHub rejected its token; run `gh auth login` "
    "and try again"
)
NO_CLI: Final = (
    "the GitHub CLI (gh) is not installed or not on PATH; install it from "
    "https://cli.github.com and run `gh auth login`"
)

type QueryKind = Literal["value", "endpoint", "field"]
type DropReason = Literal["private", "archived", "fork", "stars", "stale", "large", "gone", "limit"]
type Sleep = Callable[[float], None]
type Progress = Callable[[str], None]


class QueryRefused(HuntError):
    """GitHub will not run one query, such as one made of punctuation only. The others
    still can."""


# -- the report -------------------------------------------------------------------


class HuntFilters(BaseModel):
    """Which of the repositories found are worth a clone."""

    model_config = ConfigDict(frozen=True)

    limit: int = Field(default=20, ge=1, description="Clone at most this many, most stars first.")
    min_stars: int = Field(default=0, ge=0)
    pushed_within: int = Field(default=365, ge=1, description="Days since the last push.")
    include_forks: bool = False


class HuntQuery(BaseModel):
    """A code-search query built from the notice, and what GitHub answered."""

    kind: QueryKind
    term: str = Field(description="What it looks for: the value, the endpoint's path, the field.")
    q: str = Field(description="The query as GitHub's code search received it.")
    total: int = Field(default=0, description="Results GitHub counted; few of them are read.")
    pages: int = Field(default=0, description=f"Pages of {PER_PAGE} results read.")
    repositories: int = Field(default=0, description="Distinct repositories in those pages.")
    incomplete: bool = Field(
        default=False, description="GitHub's search timed out, so results are missing."
    )
    error: str | None = Field(default=None, description="Why GitHub refused the query.")


class HuntCounts(BaseModel):
    """How many repositories each stage kept, and why it left the others out."""

    found: int = Field(default=0, description="Distinct repositories the code search named.")
    dropped: dict[DropReason, int] = Field(
        default_factory=dict,
        description=(
            "Repositories left out, by reason. private: not public; archived; fork: without "
            "--include-forks; stars: below --min-stars; stale: no push within --pushed-within "
            f"days; large: over {MAX_SIZE_KB // 1000} MB; gone: GitHub no longer shows it; "
            "limit: passed every filter, but has fewer stars than the --limit that were checked."
        ),
    )
    eligible: int = Field(default=0, description="Repositories that passed every filter.")
    checked: int = Field(default=0, description="Repositories cloned, or tried.")
    affected: int = 0
    unaffected: int = 0
    failed: int = Field(default=0, description="Repositories that could not be checked.")


class HuntSkip(BaseModel):
    """A repository that was not checked because a clone of it would be too large."""

    full_name: str
    url: str
    stars: int
    size_mb: int


class HuntRepository(BaseModel):
    """A repository that was cloned and searched, and what the search found."""

    full_name: str
    url: str
    stars: int
    pushed_at: AwareDatetime | None = None
    default_branch: str | None = None
    fork: bool = False
    affected: bool = Field(
        default=False, description="Its code, tests and docs aside, uses what the notice changes."
    )
    reason: str | None = Field(default=None, description="Why it is affected, or is not.")
    error: str | None = Field(default=None, description="Why it could not be checked.")
    matches: list[ScanMatch] = Field(default_factory=list, description="The surest first.")
    count: int = Field(default=0, description="Every matching line.")
    files: int = Field(default=0, description="Files with a matching line.")
    tests: list[ScanMatch] = Field(
        default_factory=list, description="Places in tests, which never count on their own."
    )
    docs: list[ScanMatch] = Field(
        default_factory=list, description="Mentions in docs of a repository that is not affected."
    )
    sdk_pins: list[SdkPin] = Field(
        default_factory=list,
        description=(
            "The release of each of the notice's SDKs it asks for, next to the first one "
            "that follows the change."
        ),
    )
    api_versions: list[ApiVersionPin] = Field(
        default_factory=list, description="Dated API versions its code pins next to the API."
    )
    truncated: bool = Field(
        default=False, description="It has more text files than a search reads."
    )
    clone: str | None = Field(default=None, description="Where the clone is, with --keep.")


class HuntReport(BaseModel):
    """What ``tiltmeter hunt`` found for one notice."""

    notice_id: str
    provider: str
    headline: str = Field(description="The first sentence of the notice's summary.")
    published_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    days_left: int | None = Field(
        default=None,
        description=(
            "Days until the change takes effect: 0 or less once it has, null when the notice "
            "gives no date."
        ),
    )
    source: str = Field(description="Where the notice was read from.")
    signed: bool = Field(description="Signed by the provider, with the signature checked.")
    drafted: bool = Field(
        description="Drafted by Tiltmeter from the provider's public documentation."
    )
    filters: HuntFilters
    queries: list[HuntQuery] = Field(
        default_factory=list, description="The queries GitHub was asked, the surest sign first."
    )
    queries_not_run: int = Field(
        default=0,
        description=f"Queries that were not run: a hunt makes at most {MAX_REQUESTS} requests.",
    )
    counts: HuntCounts = Field(default_factory=HuntCounts)
    skipped: list[HuntSkip] = Field(
        default_factory=list,
        description=f"Too large to clone, most stars first; at most {MAX_SKIPPED}.",
    )
    repositories: list[HuntRepository] = Field(
        default_factory=list, description="Those checked: the affected first, then by stars."
    )
    kept: str | None = Field(default=None, description="The folder the clones stay in.")
    error: str | None = Field(default=None, description="Why the search could not run.")


# -- what to ask ------------------------------------------------------------------


def build_queries(notice: ApiChangeNotice) -> list[HuntQuery]:
    """What to ask the code search, the surest sign first: every retired value, every
    removed endpoint's path, then every changed field next to a name of the API.

    A field name is anyone's, and so is a short path, so those come with a name the same
    file must spell. The fields of a removed endpoint are not asked for: its path says more.
    """
    values: list[HuntQuery] = []
    endpoints: list[HuntQuery] = []
    fields: list[HuntQuery] = []
    imported = [sdk.name for sdk in notice.sdks if import_pattern(sdk) is not None]
    for endpoint in notice.endpoints:
        api = _api_name(endpoint, notice)
        removed = any(change.kind is ChangeKind.ENDPOINT_REMOVED for change in endpoint.changes)
        if removed:
            path = urlsplit(endpoint.url).path
            endpoints.append(_query("endpoint", path, *_path_terms(endpoint.url, api)))
        for change in endpoint.changes:
            if change.kind is ChangeKind.VALUE_RETIRED and change.before:
                values.append(_query("value", change.before, change.before))
            elif not removed and is_target(change.kind) and reads_response(change.kind):
                name = field_name(change.path)
                if name:
                    fields += [_query("field", name, name, near) for near in [api, *imported]]
    unique: dict[str, HuntQuery] = {}
    for query in [*values, *endpoints, *fields]:
        if len(query.q) <= MAX_QUERY_CHARS:
            unique.setdefault(query.q.lower(), query)
    return list(unique.values())


def _query(kind: QueryKind, term: str, *phrases: str) -> HuntQuery:
    """A query for files that spell every one of ``phrases``, each as written."""
    quoted = [phrase.replace("\\", "\\\\").replace('"', '\\"') for phrase in phrases]
    return HuntQuery(kind=kind, term=term, q=" ".join(f'"{phrase}"' for phrase in quoted))


def _api_name(endpoint: NoticeEndpoint, notice: ApiChangeNotice) -> str:
    """The name of the API a file must spell next to a path or a field: the provider's
    domain, which every customer's own host ends with (``acme.atlassian.net``). For an
    endpoint that moved to another host, the old host is what old code still names."""
    host = https_host(endpoint.url) or notice.provider
    moved = [change.moved_to.url for change in endpoint.changes if change.moved_to is not None]
    return host if any(https_host(url) != host for url in moved) else notice.provider


def _path_terms(url: str, api: str) -> list[str]:
    """The parts of an endpoint's path that code spells: what lies between its parameters,
    without a leading version, which often lives in a base URL. Unless one part is long
    enough to be no other API's, the API's name goes with them."""
    runs: list[list[str]] = [[]]
    for segment in (part for part in urlsplit(url).path.split("/") if part):
        if "{" in segment:
            runs.append([])
        else:
            runs[-1].append(segment)
    if len(runs[0]) > 1 and _VERSION.fullmatch(runs[0][0]):
        runs[0] = runs[0][1:]
    literal = ["/" + "/".join(run) for run in runs if run]
    if any(part.count("/") >= _DISTINCTIVE_SEGMENTS for part in literal):
        return literal
    return [*literal, api]


# -- GitHub -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of code-search results: the repositories it names, in GitHub's order."""

    total: int
    repositories: Sequence[str]
    results: int
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """What ranking and cloning need to know about a repository."""

    full_name: str
    url: str
    clone_url: str
    stars: int
    pushed_at: datetime | None = None
    default_branch: str | None = None
    archived: bool = False
    fork: bool = False
    private: bool = False
    size_kb: int = 0


class CodeSearch(Protocol):
    """Where code is searched and repositories are described."""

    def search(self, query: str, *, page: int) -> SearchPage:
        """One page of the repositories whose code matches ``query``. Raises
        :class:`QueryRefused` for a query that cannot run and :class:`HuntError` when no
        query can."""
        ...

    def describe(self, names: Sequence[str]) -> list[RepoFacts]:
        """The facts of each of these repositories that still exists."""
        ...


class GhSearch:
    """GitHub through its CLI, which already holds the user's login. Only ``gh api``
    requests that read: a code search, and a GraphQL query for the repositories' facts."""

    def search(self, query: str, *, page: int) -> SearchPage:
        fields = (f"q={query}", f"per_page={PER_PAGE}", f"page={page}")
        completed = _gh("-X", "GET", "search/code", *(arg for f in fields for arg in ("-f", f)))
        if completed.returncode != 0:
            raise _failure(completed, "search/code")
        return parse_search(_json(completed, "search/code"))

    def describe(self, names: Sequence[str]) -> list[RepoFacts]:
        facts: list[RepoFacts] = []
        for start in range(0, len(names), BATCH):
            completed = _gh("graphql", "-f", f"query={facts_query(names[start : start + BATCH])}")
            answer = _json(completed, "graphql")
            # A repository that is gone makes gh fail, with the others in its answer.
            partial = isinstance(answer, dict) and isinstance(answer.get("data"), dict)
            if completed.returncode != 0 and (_rate_limited(completed) or not partial):
                raise _failure(completed, "graphql")
            facts += parse_facts(answer)
        return facts


def _gh(*args: str) -> CompletedProcess[str]:
    """``gh api`` on github.com, whatever host the user's ``gh`` defaults to."""
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    command = ["gh", "api", "--hostname", GITHUB, *args]
    try:
        return run_tool(command, cwd=Path.cwd(), env=env, timeout=GH_TIMEOUT)
    except FixError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            raise HuntError(NO_CLI) from exc
        raise HuntError(f"{exc}; check the connection and run it again") from exc


def _rate_limited(completed: CompletedProcess[str]) -> bool:
    said = f"{completed.stderr}\n{completed.stdout}".lower()
    return "rate limit" in said or "http 429" in said


def _failure(completed: CompletedProcess[str], what: str) -> HuntError:
    """Why ``gh api`` failed, and what to do about it."""
    said = f"{completed.stderr}\n{completed.stdout}".lower()
    reason = printable(failure_reason(completed))
    if _rate_limited(completed):
        return HuntError(RATE_LIMITED)
    # gh exits with 4 when it has no login at all.
    if completed.returncode == 4 or "http 401" in said or "gh auth login" in said:
        return HuntError(NOT_LOGGED_IN)
    if "http 422" in said:
        return QueryRefused(reason)
    return HuntError(f"`gh api {what}` failed: {reason}")


def _json(completed: CompletedProcess[str], what: str) -> Any:
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        if completed.returncode != 0:
            raise _failure(completed, what) from exc
        raise HuntError(f"`gh api {what}` answered something that is not JSON") from exc


def parse_search(data: Any) -> SearchPage:
    """A page of ``search/code`` as GitHub answers it. A result whose repository is not
    named as GitHub names them is left out."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise HuntError("GitHub answered a code search with something unexpected")
    names: list[str] = []
    for item in items:
        repository = item.get("repository") if isinstance(item, dict) else None
        name = repository.get("full_name") if isinstance(repository, dict) else None
        if isinstance(name, str) and FULL_NAME.fullmatch(name):
            names.append(name)
    return SearchPage(
        total=_count(data.get("total_count")),
        repositories=list(dict.fromkeys(names)),
        results=len(items),
        incomplete=data.get("incomplete_results") is True,
    )


_FACTS: Final = (
    "fragment facts on Repository { nameWithOwner stargazerCount pushedAt isArchived isFork "
    "isPrivate diskUsage defaultBranchRef { name } }"
)


def facts_query(names: Sequence[str]) -> str:
    """One GraphQL query for the facts of many repositories, each under its own alias."""
    asked = []
    for position, full_name in enumerate(names):
        owner, _, name = full_name.partition("/")
        asked.append(
            f"r{position}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            "{ ...facts }"
        )
    return f"{_FACTS}\nquery {{\n" + "\n".join(asked) + "\n}"


def parse_facts(data: Any) -> list[RepoFacts]:
    """The repositories in a :func:`facts_query` answer. One that is gone, or that the
    login cannot see, comes back empty and is left out."""
    found = data.get("data") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        raise HuntError("GitHub answered the repositories' facts with something unexpected")
    return [facts for node in found.values() if (facts := _facts(node)) is not None]


def _facts(node: Any) -> RepoFacts | None:
    name = node.get("nameWithOwner") if isinstance(node, dict) else None
    if not isinstance(name, str) or not FULL_NAME.fullmatch(name):
        return None
    branch = node.get("defaultBranchRef")
    branch_name = branch.get("name") if isinstance(branch, dict) else None
    return RepoFacts(
        full_name=name,
        url=f"https://{GITHUB}/{name}",
        clone_url=f"https://{GITHUB}/{name}.git",
        stars=_count(node.get("stargazerCount")),
        pushed_at=_moment(node.get("pushedAt")),
        default_branch=printable(branch_name, 100) if isinstance(branch_name, str) else None,
        archived=node.get("isArchived") is True,
        fork=node.get("isFork") is True,
        # Anything but a plain "no" is treated as private, and so never cloned.
        private=node.get("isPrivate") is not False,
        size_kb=_count(node.get("diskUsage")),
    )


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


# -- search, rank, verify ---------------------------------------------------------


def _quiet(_: str) -> None:
    return None


def hunt(
    loaded: LoadedNotice,
    searcher: CodeSearch,
    *,
    filters: HuntFilters | None = None,
    keep: Path | None = None,
    now: datetime | None = None,
    sleep: Sleep = time.sleep,
    progress: Progress = _quiet,
) -> HuntReport:
    """Find the public repositories that still use what the notice changes.

    Three stages: ask the code search for candidates, keep the most starred that are
    alive and small enough, then clone each one and search the clone. Clones go to a
    temporary folder that is removed, or stay under ``keep``. ``sleep`` paces the code
    search, and ``progress`` hears what is going on, for whoever prints.
    """
    notice = loaded.notice
    moment = now or datetime.now(UTC)
    report = HuntReport(
        notice_id=notice.id,
        provider=notice.provider,
        headline=headline(notice.summary),
        published_at=notice.published_at,
        effective_at=notice.effective_at,
        days_left=days_until(notice.effective_at, moment),
        source=loaded.location,
        signed=loaded.verified,
        drafted=from_catalog(loaded.location),
        filters=filters or HuntFilters(),
        kept=str(keep) if keep is not None else None,
    )
    queries = build_queries(notice)
    if not queries:
        return report
    try:
        names = _search(searcher, queries, report, sleep, progress)
        progress(f"reading the facts of {_repositories(len(names))}")
        facts = {repo.full_name.lower(): repo for repo in searcher.describe(names)}
    except HuntError as exc:
        report.error = printable(str(exc))
        return report
    counts = report.counts
    counts.found = len(names)
    chosen = _rank(list(facts.values()), report, moment)
    if counts.found > len(facts):
        counts.dropped["gone"] = counts.found - len(facts)
    with _workspace(keep) as root:
        for position, repo in enumerate(chosen, start=1):
            progress(f"checking {repo.full_name} ({position} of {len(chosen)})")
            report.repositories.append(verify(repo, loaded, root, keep=keep is not None))
    report.repositories.sort(key=lambda checked: not checked.affected)
    counts.checked = len(report.repositories)
    counts.affected = sum(1 for checked in report.repositories if checked.affected)
    counts.failed = sum(1 for checked in report.repositories if checked.error is not None)
    counts.unaffected = counts.checked - counts.affected - counts.failed
    return report


def _repositories(count: int) -> str:
    return f"{count} repositor{'y' if count == 1 else 'ies'}"


def _search(
    searcher: CodeSearch,
    asked: Sequence[HuntQuery],
    report: HuntReport,
    sleep: Sleep,
    progress: Progress,
) -> list[str]:
    """The repositories the code search names, with what each query brought written to it
    and to the report. Every query gets its first page before any gets a second, for as
    long as requests are left, so a notice with many values is searched wide before deep."""
    named: list[set[str]] = [set() for _ in asked]
    spelled: dict[str, str] = {}
    turns = deque((position, 1) for position in range(len(asked)))
    requests = 0
    progress(
        f"searching GitHub code: {len(asked)} quer{'y' if len(asked) == 1 else 'ies'}, at "
        f"most {MAX_REQUESTS} requests, one every {SEARCH_PAUSE:g} seconds"
    )
    try:
        while turns and requests < MAX_REQUESTS:
            position, page = turns.popleft()
            query = asked[position]
            if requests:
                sleep(SEARCH_PAUSE)
            requests += 1
            try:
                found = searcher.search(query.q, page=page)
            except QueryRefused as exc:
                query.error = printable(str(exc))
                continue
            for name in found.repositories:
                spelled.setdefault(name.lower(), name)
                named[position].add(name.lower())
            query.total, query.pages, query.repositories = found.total, page, len(named[position])
            query.incomplete = query.incomplete or found.incomplete
            if found.results >= PER_PAGE and page * PER_PAGE < found.total and page < MAX_PAGES:
                turns.append((position, page + 1))
    finally:
        # Also when a request fails, so the report says how far the search got.
        report.queries = [query for query in asked if query.pages or query.error]
        report.queries_not_run = len(asked) - len(report.queries)
    refused = [query.error for query in report.queries if query.error and not query.pages]
    if len(refused) == len(report.queries):
        raise HuntError(f"GitHub refused every query built from the notice: {refused[0]}")
    return list(spelled.values())


def _rank(facts: Sequence[RepoFacts], report: HuntReport, now: datetime) -> list[RepoFacts]:
    """The repositories worth a clone, most stars first, with the count of those left out
    and why written to the report."""
    filters, counts = report.filters, report.counts
    oldest = now - timedelta(days=filters.pushed_within)
    eligible: list[RepoFacts] = []
    large: list[RepoFacts] = []
    for repo in sorted(facts, key=lambda repo: (-repo.stars, repo.full_name.lower())):
        reason = drop_reason(repo, filters, oldest)
        if reason is None:
            eligible.append(repo)
            continue
        counts.dropped[reason] = counts.dropped.get(reason, 0) + 1
        if reason == "large":
            large.append(repo)
    counts.eligible = len(eligible)
    if len(eligible) > filters.limit:
        counts.dropped["limit"] = len(eligible) - filters.limit
    report.skipped = [
        HuntSkip(
            full_name=repo.full_name, url=repo.url, stars=repo.stars, size_mb=repo.size_kb // 1000
        )
        for repo in large[:MAX_SKIPPED]
    ]
    return eligible[: filters.limit]


def drop_reason(repo: RepoFacts, filters: HuntFilters, oldest: datetime) -> DropReason | None:
    """Why a repository is not worth a clone, or None when it is."""
    if repo.private:
        return "private"
    if repo.archived:
        return "archived"
    if repo.fork and not filters.include_forks:
        return "fork"
    if repo.stars < filters.min_stars:
        return "stars"
    if repo.pushed_at is None or repo.pushed_at < oldest:
        return "stale"
    if repo.size_kb > MAX_SIZE_KB:
        return "large"
    return None


@contextmanager
def _workspace(keep: Path | None) -> Iterator[Path]:
    """Where the clones go: under ``keep``, to stay, or in a temporary folder."""
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        yield keep
        return
    with tempfile.TemporaryDirectory(prefix="tiltmeter-hunt-", ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


def verify(repo: RepoFacts, loaded: LoadedNotice, root: Path, *, keep: bool) -> HuntRepository:
    """Clone one repository under ``root`` and search the clone for the notice. A clone
    that fails is that repository's error, not the hunt's. Without ``keep`` the clone is
    removed right away, so a hunt never holds more than one."""
    checked = HuntRepository(
        full_name=printable(repo.full_name),
        url=repo.url,
        stars=repo.stars,
        pushed_at=repo.pushed_at,
        default_branch=repo.default_branch,
        fork=repo.fork,
    )
    # The name becomes a folder here, whoever the searcher got it from.
    if not FULL_NAME.fullmatch(repo.full_name):
        checked.error = "its name is not one GitHub gives a repository"
        return checked
    owner, _, name = repo.full_name.partition("/")
    into = root / owner / name
    if into.exists():
        checked.error = f"{into} already exists; remove it or keep the clones elsewhere"
        return checked
    try:
        into.parent.mkdir(parents=True, exist_ok=True)
        Git.clone(repo.clone_url, into, timeout=CLONE_TIMEOUT)
        _read(checked, into, loaded)
    except (TiltmeterError, OSError) as exc:
        checked.error = printable(str(exc))
    if keep and checked.error is None:
        checked.clone = str(into)
    else:
        _remove(into)
    return checked


def _remove(clone: Path) -> None:
    """Delete a clone, or what a failed one left. Git marks its objects read-only, and
    Windows deletes no such file until it is made writable."""

    def unlock(retry: Callable[[str], object], path: str, _: BaseException) -> None:
        os.chmod(path, stat.S_IWRITE)
        retry(path)

    with suppress(OSError):
        shutil.rmtree(clone, onexc=unlock)


def _read(checked: HuntRepository, clone: Path, loaded: LoadedNotice) -> None:
    """Search a clone as ``tiltmeter scan`` would, for this one notice: code decides, and
    tests and docs are listed apart."""
    location = loaded.location
    only = Catalog(source=location, bundled=from_catalog(location), entries=[loaded], rejected=[])
    found = scan(read_folder(clone), only, label=checked.full_name)
    checked.truncated = found.truncated
    for finding in found.affected:
        checked.affected = True
        checked.reason = finding.reason
        checked.matches = finding.matches
        checked.count, checked.files = finding.count, finding.files
        checked.tests = finding.tests
        checked.sdk_pins, checked.api_versions = finding.sdk_pins, finding.api_versions
    for miss in found.unaffected:
        checked.reason = miss.reason
        checked.tests, checked.docs = miss.tests, miss.docs
