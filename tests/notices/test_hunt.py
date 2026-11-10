from __future__ import annotations

import json
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.fix.kit import CLIENT, make_repo
from tests.notices.kit import (
    KEY_FILE,
    KEY_URL,
    NOTICE_ID,
    PROVIDER,
    FakeCodeSearch,
    notice,
    repo_facts,
    signed,
)
from tests.test_cli import flat, invoke
from tiltmeter.cli import hunt_exit_code
from tiltmeter.errors import FixError, HuntError
from tiltmeter.notices import hunt as hunting
from tiltmeter.notices.hunt import (
    BATCH,
    CLONE_TIMEOUT,
    MAX_QUERY_CHARS,
    NO_CLI,
    NOT_LOGGED_IN,
    RATE_LIMITED,
    SEARCH_PAUSE,
    GhSearch,
    HuntFilters,
    HuntReport,
    QueryRefused,
    RepoFacts,
    SearchPage,
    build_queries,
    drop_reason,
    facts_query,
    hunt,
    parse_facts,
    parse_search,
    verify,
)
from tiltmeter.notices.reader import LoadedNotice, bundled_feed, parse_notice

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
GROQ = "groq-retired-models-2026-08"

# A repository whose code never calls the billing API: its docs and tests only name it.
ONLY_MENTIONS = {
    "README.md": "The billing API at api.billing.test returns phone.\n",
    "tests/test_client.py": 'URL = "https://api.billing.test/v1/customers/1"\nPHONE = "phone"\n',
    "app.py": "print('hello')\n",
}


def retired(value: str) -> dict[str, Any]:
    return {"path": "$.model", "kind": "value_retired", "before": value}


def removed(url: str, moved_to: str | None = None) -> dict[str, Any]:
    change: dict[str, Any] = {"path": "$", "kind": "endpoint_removed"}
    if moved_to is not None:
        change["moved_to"] = {"method": "GET", "url": moved_to}
    return {"method": "GET", "url": url, "changes": [change]}


def retirement(*values: str) -> dict[str, Any]:
    """A notice that retires model ids, each of which is one query."""
    chat = {
        "method": "POST",
        "url": f"https://{PROVIDER}/v1/chat",
        "changes": [retired(value) for value in values],
    }
    return notice(endpoints=[chat], sdks=[])


def loaded(raw: dict[str, Any] | None = None) -> LoadedNotice:
    raw = raw if raw is not None else notice()
    return LoadedNotice(parse_notice(raw), raw, "notice.json", verified=False)


def asked(raw: dict[str, Any]) -> list[str]:
    return [query.q for query in build_queries(parse_notice(raw))]


def source(tmp_path: Path, name: str, files: dict[str, str] | None = None) -> Path:
    """A repository to clone from, as a hosting service would serve it."""
    folder = tmp_path / "sources" / name
    folder.mkdir(parents=True)
    return make_repo(folder, files)[1]


def hunted(
    searcher: FakeCodeSearch,
    raw: dict[str, Any] | None = None,
    *,
    keep: Path | None = None,
    **filters: Any,
) -> tuple[HuntReport, list[float]]:
    """A hunt that never waits, and the pauses it asked for."""
    pauses: list[float] = []
    report = hunt(
        loaded(raw),
        searcher,
        filters=HuntFilters(**filters),
        keep=keep,
        now=NOW,
        sleep=pauses.append,
    )
    return report, pauses


# -- what GitHub is asked -----------------------------------------------------------


def test_a_retired_value_is_searched_whole_and_once() -> None:
    chat = {
        "method": "POST",
        "url": f"https://{PROVIDER}/v1/chat",
        "changes": [retired("tilt-1"), retired('say "hi"')],
    }
    text = {"method": "POST", "url": f"https://{PROVIDER}/v1/text", "changes": [retired("tilt-1")]}
    built = build_queries(parse_notice(notice(endpoints=[chat, text], sdks=[])))
    assert [(query.kind, query.term, query.q) for query in built] == [
        ("value", "tilt-1", '"tilt-1"'),
        ("value", 'say "hi"', '"say \\"hi\\""'),
    ]


def test_a_removed_endpoint_is_searched_by_the_path_code_spells() -> None:
    raw = notice(
        provider="acme.test",
        sdks=[],
        endpoints=[
            # Long enough to be no other API's: the path alone.
            removed("https://your-domain.acme.test/rest/api/3/search"),
            # What lies between the parameters, without the version, next to the provider.
            removed("https://api.acme.test/v1/playlists/{id}/tracks"),
            # It moved to another host, so the old host is what old code names.
            removed("https://old.acme.test/v1/chat", moved_to="https://new.acme.test/v1/chat"),
            removed("https://api.acme.test/{id}"),
        ],
    )
    assert asked(raw) == [
        '"/rest/api/3/search"',
        '"/playlists" "/tracks" "acme.test"',
        '"/chat" "old.acme.test"',
        '"acme.test"',
    ]
    [first, *_] = build_queries(parse_notice(raw))
    assert (first.kind, first.term) == ("endpoint", "/rest/api/3/search")


def test_a_changed_field_is_searched_next_to_a_name_of_the_api() -> None:
    sdks = [
        {"ecosystem": "pypi", "name": "billing-sdk", "fixed_in": "3.0.0"},
        # Code never spells a Maven coordinate, so it is no use next to a field.
        {"ecosystem": "maven", "name": "test.billing:billing-java", "fixed_in": "2.0.0"},
    ]
    assert asked(notice(sdks=sdks)) == [
        '"phone" "api.billing.test"',
        '"phone" "billing-sdk"',
        '"balance" "api.billing.test"',
        '"balance" "billing-sdk"',
    ]


def test_values_come_first_and_a_removed_endpoint_s_fields_are_not_searched() -> None:
    gone = removed(f"https://{PROVIDER}/v1/customers/{{id}}")
    gone["changes"] += [{"path": "$.phone", "kind": "field_removed"}, retired("tilt-1")]
    assert asked(notice(endpoints=[gone], sdks=[])) == [
        '"tilt-1"',
        '"/customers" "api.billing.test"',
    ]


def test_a_notice_code_need_not_follow_builds_no_query() -> None:
    added = {
        "method": "GET",
        "url": f"https://{PROVIDER}/v1/customers",
        "changes": [{"path": "$.nickname", "kind": "field_added"}],
    }
    too_long = removed(f"https://{PROVIDER}/{'a' * MAX_QUERY_CHARS}")
    assert asked(notice(endpoints=[added, too_long], sdks=[])) == []


def test_every_catalog_entry_builds_queries_github_accepts() -> None:
    for item in bundled_feed():
        built = build_queries(parse_notice(item))
        assert built, item["id"]
        assert all(len(query.q) <= MAX_QUERY_CHARS for query in built), item["id"]
    jira = next(item for item in bundled_feed() if item["id"].startswith("jira-cloud"))
    assert asked(jira) == ['"/rest/api/3/search"', '"/rest/api/2/search"']


# -- what GitHub answers ------------------------------------------------------------

# Trimmed from real answers of `gh api search/code` and `gh api graphql`.
SEARCH_ANSWER: dict[str, Any] = {
    "total_count": 49280,
    "incomplete_results": False,
    "items": [
        {
            "name": "README.md",
            "path": "README.md",
            "repository": {"id": 609542180, "full_name": "appleboy/CodeGPT", "private": False},
            "score": 1.0,
        },
        {"name": "llm.go", "path": "llm.go", "repository": {"full_name": "appleboy/CodeGPT"}},
        {"name": "x.py", "path": "x.py", "repository": {"full_name": "acme/.github"}},
        {"name": "x.py", "path": "x.py", "repository": {"full_name": "acme/../../etc"}},
        {"name": "x.py", "path": "x.py", "repository": {"full_name": "acme/.."}},
        {"name": "x.py", "path": "x.py", "repository": None},
        "not a result",
    ],
}
FACTS_ANSWER: dict[str, Any] = {
    "data": {
        "r0": {
            "nameWithOwner": "appleboy/CodeGPT",
            "stargazerCount": 1505,
            "pushedAt": "2026-09-29T14:01:56Z",
            "isArchived": False,
            "isFork": False,
            "isPrivate": False,
            "diskUsage": 1779,
            "defaultBranchRef": {"name": "main"},
        },
        "r1": None,
    },
    "errors": [
        {
            "type": "NOT_FOUND",
            "path": ["r1"],
            "message": "Could not resolve to a Repository with the name 'acme/gone'.",
        }
    ],
}
CODEGPT = RepoFacts(
    full_name="appleboy/CodeGPT",
    url="https://github.com/appleboy/CodeGPT",
    clone_url="https://github.com/appleboy/CodeGPT.git",
    stars=1505,
    pushed_at=datetime(2026, 9, 29, 14, 1, 56, tzinfo=UTC),
    default_branch="main",
    size_kb=1779,
)


def test_a_search_answer_names_each_repository_once() -> None:
    assert parse_search(SEARCH_ANSWER) == SearchPage(
        total=49280, repositories=["appleboy/CodeGPT", "acme/.github"], results=7
    )
    assert parse_search({"items": [], "incomplete_results": True}).incomplete
    with pytest.raises(HuntError, match="something unexpected"):
        parse_search({"message": "Not Found"})


def test_the_facts_of_repositories_that_still_exist() -> None:
    assert parse_facts(FACTS_ANSWER) == [CODEGPT]
    odd = {"nameWithOwner": "acme/app", "stargazerCount": "many", "pushedAt": "yesterday"}
    [repo] = parse_facts({"data": {"r0": odd, "r1": {"nameWithOwner": "acme/a b"}}})
    assert (repo.stars, repo.pushed_at, repo.default_branch, repo.size_kb) == (0, None, None, 0)
    assert repo.private  # not said to be public, so never cloned
    with pytest.raises(HuntError, match="something unexpected"):
        parse_facts({"data": None})


def test_one_query_asks_for_many_repositories() -> None:
    query = facts_query(["appleboy/CodeGPT", "acme/.github"])
    assert 'r0: repository(owner: "appleboy", name: "CodeGPT") { ...facts }' in query
    assert 'r1: repository(owner: "acme", name: ".github") { ...facts }' in query
    assert "stargazerCount pushedAt isArchived isFork isPrivate diskUsage" in query


class FakeGh:
    """Stands in for ``run_tool``: records each command and plays back what gh printed."""

    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        self.results = list(results)
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        self.environments.append(dict(options["env"]))
        return self.results.pop(0)


def printed(stdout: Any = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess(["gh"], code, text, stderr)


def gh(monkeypatch: pytest.MonkeyPatch, *results: subprocess.CompletedProcess[str]) -> FakeGh:
    fake = FakeGh(*results)
    monkeypatch.setattr(hunting, "run_tool", fake)
    return fake


def test_the_code_search_runs_gh_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = gh(monkeypatch, printed(SEARCH_ANSWER))
    page = GhSearch().search('"tilt-1" "acme.test"', page=2)
    assert page.repositories == ["appleboy/CodeGPT", "acme/.github"]
    start = ["gh", "api", "--hostname", "github.com", "-X", "GET", "search/code"]
    fields = ["-f", 'q="tilt-1" "acme.test"', "-f", "per_page=100", "-f", "page=2"]
    assert fake.commands == [start + fields]
    assert fake.environments[0]["GH_PROMPT_DISABLED"] == "1"


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (printed(stderr="gh: API rate limit exceeded for user ID 1. (HTTP 403)", code=1), "wait"),
        (
            printed(
                {"message": "You have exceeded a secondary rate limit."},
                stderr="gh: You have exceeded a secondary rate limit. (HTTP 429)",
                code=1,
            ),
            "wait a minute and run it again",
        ),
        (
            printed(
                stderr=(
                    "To get started with GitHub CLI, please run:  gh auth login\n"
                    "Alternatively, populate the GH_TOKEN environment variable with a GitHub "
                    "API authentication token.\n"
                ),
                code=4,
            ),
            "run `gh auth login`",
        ),
        (printed(stderr="gh: Bad credentials (HTTP 401)", code=1), "run `gh auth login`"),
        (printed(stderr="gh: Server Error (HTTP 502)", code=1), r"`gh api search/code` failed"),
        (printed("<html>"), "not JSON"),
    ],
)
def test_a_search_that_fails_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, result: subprocess.CompletedProcess[str], error: str
) -> None:
    gh(monkeypatch, result)
    with pytest.raises(HuntError, match=error) as caught:
        GhSearch().search('"tilt-1"', page=1)
    assert not isinstance(caught.value, QueryRefused)


def test_a_query_github_refuses_is_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    gh(monkeypatch, printed(stderr="gh: Validation Failed (HTTP 422)", code=1))
    with pytest.raises(QueryRefused, match="Validation Failed"):
        GhSearch().search('"!"', page=1)


def test_a_missing_gh_says_how_to_get_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HuntError) as caught:
        GhSearch().search('"tilt-1"', page=1)
    assert str(caught.value) == NO_CLI
    assert "gh auth login" in NO_CLI


def test_facts_are_asked_in_batches_and_survive_a_missing_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gone = "gh: Could not resolve to a Repository with the name 'acme/gone'."
    fake = gh(monkeypatch, printed(FACTS_ANSWER, stderr=gone, code=1), printed({"data": {}}))
    names = ["appleboy/CodeGPT", "acme/gone", *(f"acme/r{i}" for i in range(BATCH - 1))]
    assert GhSearch().describe(names) == [CODEGPT]
    first, second = fake.commands
    assert first[:5] == ["gh", "api", "--hostname", "github.com", "graphql"]
    assert first[5] == "-f"
    assert first[6].startswith("query=fragment facts on Repository")
    assert f"r{BATCH - 1}: repository(" in first[6]
    assert f"r{BATCH}: " not in first[6]
    assert f'r0: repository(owner: "acme", name: "r{BATCH - 2}")' in second[6]


def test_facts_that_cannot_be_read_stop_the_hunt(monkeypatch: pytest.MonkeyPatch) -> None:
    limited = {"data": {"r0": None}, "errors": [{"type": "RATE_LIMITED"}]}
    gh(monkeypatch, printed(limited, stderr="gh: API rate limit exceeded", code=1))
    with pytest.raises(HuntError, match="rate limit"):
        GhSearch().describe(["acme/app"])
    gh(monkeypatch, printed({"message": "Bad credentials"}, stderr="gh: HTTP 401", code=1))
    with pytest.raises(HuntError, match="gh auth login"):
        GhSearch().describe(["acme/app"])


# -- search -------------------------------------------------------------------------


def test_the_search_is_paced_and_reads_wide_before_deep() -> None:
    many = [f"acme/r{i}" for i in range(250)]
    hits = {'"tilt-1"': many, '"tilt-2"': ["acme/r1", "Acme/R1", "other/app"], '"tilt-3"': []}
    searcher = FakeCodeSearch(hits=hits)
    report, pauses = hunted(searcher, retirement("tilt-1", "tilt-2", "tilt-3"))
    assert searcher.searched == [
        ('"tilt-1"', 1),
        ('"tilt-2"', 1),
        ('"tilt-3"', 1),
        ('"tilt-1"', 2),
        ('"tilt-1"', 3),
    ]
    assert pauses == [SEARCH_PAUSE] * 4  # between requests, so none before the first
    assert SEARCH_PAUSE * 10 > 60  # GitHub allows about 10 code searches a minute
    first, second, third = report.queries
    assert (first.total, first.pages, first.repositories) == (250, 3, 250)
    assert (second.total, second.pages, second.repositories) == (3, 1, 2)
    assert (third.total, third.pages, third.repositories) == (0, 1, 0)
    assert report.counts.found == 251
    assert len(searcher.described[0]) == 251
    assert report.counts.dropped == {"gone": 251}  # the fake knows none of them
    assert report.repositories == []


def test_the_search_stops_at_its_request_and_page_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hunting, "MAX_REQUESTS", 2)
    searcher = FakeCodeSearch(hits={})
    report, pauses = hunted(searcher, retirement("tilt-1", "tilt-2", "tilt-3"))
    assert [query.term for query in report.queries] == ["tilt-1", "tilt-2"]
    assert report.queries_not_run == 1
    assert len(pauses) == 1

    monkeypatch.setattr(hunting, "MAX_REQUESTS", 30)
    monkeypatch.setattr(hunting, "MAX_PAGES", 2)
    deep = FakeCodeSearch(hits={'"tilt-1"': [f"acme/r{i}" for i in range(250)]})
    report, _ = hunted(deep, retirement("tilt-1"))
    assert deep.searched == [('"tilt-1"', 1), ('"tilt-1"', 2)]
    assert report.queries[0].repositories == 200


def test_a_rate_limit_ends_the_hunt_with_what_to_do() -> None:
    searcher = FakeCodeSearch(error=HuntError(RATE_LIMITED))
    report, _ = hunted(searcher, retirement("tilt-1", "tilt-2"))
    assert report.error == RATE_LIMITED
    assert "wait a minute" in report.error
    assert len(searcher.searched) == 1
    assert searcher.described == []
    assert report.repositories == []
    assert hunt_exit_code(report) == 3

    later = FakeCodeSearch(error=HuntError(RATE_LIMITED), fails_after=1)
    report, _ = hunted(later, retirement("tilt-1", "tilt-2", "tilt-3"))
    assert report.error == RATE_LIMITED
    assert [query.term for query in report.queries] == ["tilt-1"]  # how far it got
    assert report.queries_not_run == 2
    assert later.described == []


def test_a_refused_query_does_not_stop_the_others() -> None:
    searcher = FakeCodeSearch(refused=['"tilt-1"'])
    report, _ = hunted(searcher, retirement("tilt-1", "tilt-2"))
    assert report.error is None
    refused, ran = report.queries
    assert refused.error == "gh: Validation Failed (HTTP 422)"
    assert (refused.pages, ran.pages) == (0, 1)

    every = FakeCodeSearch(refused=['"tilt-1"', '"tilt-2"'])
    report, _ = hunted(every, retirement("tilt-1", "tilt-2"))
    assert report.error is not None
    assert "GitHub refused every query" in report.error


def test_nothing_to_search_for_asks_github_nothing() -> None:
    added = {
        "method": "GET",
        "url": f"https://{PROVIDER}/v1/customers",
        "changes": [{"path": "$.nickname", "kind": "field_added"}],
    }
    searcher = FakeCodeSearch()
    report, _ = hunted(searcher, notice(endpoints=[added], sdks=[]))
    assert report.queries == []
    assert searcher.searched == []
    assert hunt_exit_code(report) == 0


# -- rank ---------------------------------------------------------------------------


def candidates(tmp_path: Path) -> list[RepoFacts]:
    """Repositories that cannot be cloned, which is enough to see which ones a hunt picks."""
    nowhere = tmp_path / "nowhere.git"
    return [
        repo_facts("acme/second", nowhere, stars=50, pushed_at=NOW),
        repo_facts("acme/top", nowhere, stars=900, pushed_at=NOW),
        repo_facts("acme/third", nowhere, stars=5, pushed_at=NOW - timedelta(days=364)),
        repo_facts("acme/archived", nowhere, stars=9000, pushed_at=NOW, archived=True),
        repo_facts("acme/fork", nowhere, stars=7000, pushed_at=NOW, fork=True),
        repo_facts("acme/quiet", nowhere, stars=6000, pushed_at=NOW - timedelta(days=366)),
        repo_facts("acme/never", nowhere, stars=5000, pushed_at=None),
        repo_facts("acme/small", nowhere, stars=2, pushed_at=NOW),
        repo_facts("acme/huge", nowhere, stars=4000, pushed_at=NOW, size_kb=600_000),
        repo_facts("acme/private", nowhere, stars=3000, pushed_at=NOW, private=True),
    ]


def test_the_most_starred_live_repositories_are_checked(tmp_path: Path) -> None:
    facts = candidates(tmp_path)
    names = [repo.full_name for repo in facts]
    searcher = FakeCodeSearch(facts, hits={'"tilt-1"': [*names, "acme/gone"]})
    report, _ = hunted(searcher, retirement("tilt-1"), limit=2, min_stars=3)
    assert [repo.full_name for repo in report.repositories] == ["acme/top", "acme/second"]
    assert report.counts.found == 11
    assert report.counts.dropped == {
        "archived": 1,
        "fork": 1,
        "stale": 2,
        "large": 1,
        "private": 1,
        "stars": 1,
        "gone": 1,
        "limit": 1,
    }
    assert report.counts.eligible == 3
    assert report.counts.checked == 2
    [skipped] = report.skipped
    assert (skipped.full_name, skipped.stars, skipped.size_mb) == ("acme/huge", 4000, 600)
    assert skipped.url == "https://github.com/acme/huge"


def test_filters_can_be_loosened(tmp_path: Path) -> None:
    searcher = FakeCodeSearch(candidates(tmp_path))
    report, _ = hunted(
        searcher, retirement("tilt-1"), limit=3, pushed_within=400, include_forks=True
    )
    assert [repo.full_name for repo in report.repositories] == [
        "acme/fork",
        "acme/quiet",
        "acme/top",
    ]
    assert report.repositories[0].fork
    assert "fork" not in report.counts.dropped
    assert report.counts.dropped["stale"] == 1  # never pushed
    assert report.counts.dropped["limit"] == 3


def test_why_a_repository_is_not_worth_a_clone() -> None:
    filters, oldest = HuntFilters(min_stars=5), NOW - timedelta(days=365)
    fine = repo_facts("acme/app", "x", pushed_at=NOW)
    assert drop_reason(fine, filters, oldest) is None
    assert drop_reason(repo_facts("acme/app", "x", stars=4), filters, oldest) == "stars"
    # Private beats everything: such a repository is not even described further.
    private = repo_facts("acme/app", "x", private=True, archived=True, fork=True)
    assert drop_reason(private, filters, oldest) == "private"
    edge = repo_facts("acme/app", "x", pushed_at=NOW, size_kb=500_000)
    assert drop_reason(edge, filters, oldest) is None


# -- verify -------------------------------------------------------------------------


def test_each_clone_is_searched_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    facts = [
        repo_facts("acme/mentions", source(tmp_path, "mentions", ONLY_MENTIONS), stars=900),
        repo_facts("acme/broken", tmp_path / "missing.git", stars=300),
        repo_facts("acme/app", source(tmp_path, "app"), stars=20),
    ]
    steps: list[str] = []
    report = hunt(
        loaded(), FakeCodeSearch(facts), now=NOW, sleep=lambda _: None, progress=steps.append
    )
    assert report.error is None
    affected, unaffected, failed = report.repositories  # the affected first, then by stars

    assert affected.full_name == "acme/app"
    assert affected.affected
    assert affected.reason is not None
    assert "code near them reads phone" in affected.reason
    [match] = affected.matches
    assert (match.file, match.line, match.kind, match.term) == ("client.py", 6, "field", "phone")
    assert match.text == 'return customer["phone"]'
    assert match.detail == "renamed to $.phone_number"
    assert (affected.count, affected.files, affected.clone) == (1, 1, None)
    assert (affected.stars, affected.default_branch) == (20, "main")
    assert affected.url == "https://github.com/acme/app"

    assert unaffected.full_name == "acme/mentions"
    assert not unaffected.affected
    assert unaffected.error is None
    assert unaffected.reason is not None
    assert unaffected.reason.startswith("no tracked file mentions")
    assert [(place.file, place.line) for place in unaffected.tests] == [("tests/test_client.py", 2)]
    assert [(place.file, place.line) for place in unaffected.docs] == [("README.md", 1)]
    assert unaffected.matches == []

    assert failed.full_name == "acme/broken"
    assert not failed.affected
    assert failed.error is not None
    assert failed.error.startswith("`git clone` failed: ")
    assert failed.reason is None

    counts = report.counts
    assert (counts.checked, counts.affected, counts.unaffected, counts.failed) == (3, 1, 1, 1)
    assert hunt_exit_code(report) == 1
    assert list(scratch.iterdir()) == []  # nothing stays behind
    assert steps[-3:] == [
        "checking acme/mentions (1 of 3)",
        "checking acme/broken (2 of 3)",
        "checking acme/app (3 of 3)",
    ]


def test_kept_clones_stay_and_are_never_overwritten(tmp_path: Path) -> None:
    keep = tmp_path / "clones"
    facts = [
        repo_facts("acme/app", source(tmp_path, "app")),
        repo_facts("acme/broken", tmp_path / "missing.git"),
    ]
    report, _ = hunted(FakeCodeSearch(facts), keep=keep)
    assert report.kept == str(keep)
    [kept] = [repo for repo in report.repositories if repo.clone]
    assert kept.clone == str(keep / "acme" / "app")
    assert (keep / "acme" / "app" / "client.py").read_text(encoding="utf-8") == CLIENT
    assert not (keep / "acme" / "broken").exists()

    (keep / "acme" / "app" / "client.py").write_text("mine\n", encoding="utf-8")
    again, _ = hunted(FakeCodeSearch(facts[:1]), keep=keep)
    [existing] = again.repositories
    assert existing.error is not None
    assert "already exists" in existing.error
    assert (keep / "acme" / "app" / "client.py").read_text(encoding="utf-8") == "mine\n"
    assert hunt_exit_code(again) == 3  # nothing could be checked


def test_a_clone_that_takes_too_long_is_that_repository_s_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits: list[float | None] = []

    def too_slow(command: list[str], **options: Any) -> Any:
        limits.append(options["timeout"])
        raise FixError("git did not finish within 300 seconds")  # as run_tool says it

    monkeypatch.setattr("tiltmeter.fix.git.run_tool", too_slow)
    root = tmp_path / "clones"
    root.mkdir()
    checked = verify(repo_facts("acme/app", tmp_path / "slow.git"), loaded(), root, keep=True)
    assert limits == [CLONE_TIMEOUT]
    assert checked.error == "git did not finish within 300 seconds"
    assert not checked.affected
    assert not (root / "acme" / "app").exists()


def test_a_clone_is_removed_with_its_read_only_files(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / "objects").mkdir(parents=True)
    packed = clone / "objects" / "pack.idx"
    packed.write_text("x", encoding="utf-8")
    packed.chmod(stat.S_IREAD)  # as git leaves its objects, which Windows then keeps
    hunting._remove(clone)
    assert not clone.exists()
    hunting._remove(clone)  # nothing left to remove is no error


def test_a_private_repository_is_never_cloned(tmp_path: Path) -> None:
    keep = tmp_path / "clones"
    private = repo_facts("acme/app", source(tmp_path, "app"), private=True)
    report, _ = hunted(FakeCodeSearch([private]), keep=keep)
    assert report.repositories == []
    assert report.counts.dropped == {"private": 1}
    assert list(keep.iterdir()) == []


@pytest.mark.parametrize("name", ["../outside", "acme/..", "acme/a/b", "acme/a b", "-x/app"])
def test_a_name_github_would_not_give_is_not_made_a_folder(tmp_path: Path, name: str) -> None:
    root = tmp_path / "clones"
    root.mkdir()
    odd = repo_facts(name, source(tmp_path, "app"))
    checked = verify(odd, loaded(), root, keep=True)
    assert checked.error == "its name is not one GitHub gives a repository"
    assert list(root.iterdir()) == []
    assert not (tmp_path / "outside").exists()


def test_every_failed_clone_means_the_hunt_could_not_run() -> None:
    def report(**counts: int) -> HuntReport:
        raw = {"notice_id": "x", "provider": "acme.test", "headline": "", "source": "x"}
        flags = {"signed": False, "drafted": False, "filters": HuntFilters(), "counts": counts}
        return HuntReport.model_validate({**raw, **flags, "published_at": NOW})

    assert hunt_exit_code(report()) == 0
    assert hunt_exit_code(report(checked=3, unaffected=2, failed=1)) == 0
    assert hunt_exit_code(report(checked=3, affected=1, failed=2)) == 1
    assert hunt_exit_code(report(checked=3, failed=3)) == 3


# -- tiltmeter hunt -----------------------------------------------------------------


def github(monkeypatch: pytest.MonkeyPatch, searcher: FakeCodeSearch) -> FakeCodeSearch:
    """Make ``tiltmeter hunt`` ask ``searcher`` instead of GitHub, without the pauses."""
    monkeypatch.setattr(hunting, "GhSearch", lambda: searcher)
    monkeypatch.setattr(hunting, "SEARCH_PAUSE", 0.0)
    return searcher


def notice_file(tmp_path: Path, raw: dict[str, Any] | None = None) -> str:
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(raw if raw is not None else notice()), encoding="utf-8")
    return str(path)


def test_hunt_shows_who_still_uses_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = [
        repo_facts("acme/app", source(tmp_path, "app"), stars=1505),
        repo_facts("acme/mentions", source(tmp_path, "mentions", ONLY_MENTIONS), stars=900),
        repo_facts("acme/broken", tmp_path / "missing.git", stars=300),
        repo_facts("acme/monorepo", tmp_path / "missing.git", stars=90_000, size_kb=2_400_000),
        repo_facts("acme/old", tmp_path / "missing.git", archived=True),
    ]
    github(monkeypatch, FakeCodeSearch(facts))
    outcome = invoke("hunt", notice_file(tmp_path))
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert "api.billing.test Customers now return phone_number instead of phone" in text
    assert f"{NOTICE_ID} · unsigned" in text
    assert "✗ acme/app 1,505 stars · pushed " in text
    assert "https://github.com/acme/app" in text
    assert "client.py:6 phone: renamed to $.phone_number" in text
    assert 'return customer["phone"]' in text
    assert "- acme/mentions 900 stars not affected: no tracked file mentions" in text
    assert "used only in tests, which do not count: tests/test_client.py:2" in text
    assert "mentioned in docs, which do not count: README.md:1" in text
    assert "! acme/broken 300 stars could not be checked: `git clone` failed" in text
    assert (
        f"1 of 2 repositories checked still uses what {NOTICE_ID} changes; 1 more could not "
        "be checked." in text
    )
    assert "GitHub's code search named 5 repositories for 4 queries" in text
    assert "left out: 1 archived, 1 too large to clone." in text
    assert "skipped as too large to clone: acme/monorepo (2,400 MB)" in text
    assert "checking acme/app (1 of 3)" in text  # progress, on stderr
    assert text.index("✗ acme/app") < text.index("- acme/mentions") < text.index("1 of 2")


def test_hunt_says_when_the_change_takes_effect_and_which_release_each_one_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    behind = {
        "client.py": CLIENT,
        "requirements.txt": "billing-sdk==2.9.0\n",
        "uv.lock": '[[package]]\nname = "billing-sdk"\nversion = "2.9.0"\n',
    }
    facts = [repo_facts("acme/behind", source(tmp_path, "behind", behind), stars=40)]
    github(monkeypatch, FakeCodeSearch(facts))
    soon = (datetime.now(UTC) + timedelta(days=24, hours=1)).isoformat()
    raw = notice(effective_at=soon, published_at="2026-01-01T00:00:00Z")
    outcome = invoke("hunt", notice_file(tmp_path, raw))
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert f"takes effect {soon[:10]}, in 25 days" in text
    assert (
        "installs billing-sdk 2.9.0 (uv.lock), which is older than 3.0.0, the first release "
        "that follows the change"
    ) in text
    report = json.loads(invoke("hunt", notice_file(tmp_path, raw), "--json").stdout)
    assert report["days_left"] == 25
    assert report["effective_at"] is not None
    [repo] = report["repositories"]
    assert [(pin["file"], pin["version"], pin["follows"]) for pin in repo["sdk_pins"]] == [
        ("uv.lock", "2.9.0", False)
    ]
    assert repo["api_versions"] == []
    undated = notice_file(tmp_path, notice(effective_at=None))
    assert "published 2026-11-03" in flat(invoke("hunt", undated).output)


def test_hunt_prints_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facts = [
        repo_facts("acme/app", source(tmp_path, "app"), stars=1505),
        repo_facts("acme/broken", tmp_path / "missing.git", stars=300),
    ]
    github(monkeypatch, FakeCodeSearch(facts))
    path = notice_file(tmp_path)
    outcome = invoke("hunt", path, "--limit", "5", "--min-stars", "1", "--json")
    assert outcome.exit_code == 1, outcome.output
    report = json.loads(outcome.stdout)
    assert list(report) == [
        "notice_id",
        "provider",
        "headline",
        "published_at",
        "effective_at",
        "days_left",
        "source",
        "signed",
        "drafted",
        "filters",
        "queries",
        "queries_not_run",
        "counts",
        "skipped",
        "repositories",
        "kept",
        "error",
    ]
    assert (report["notice_id"], report["provider"]) == (NOTICE_ID, PROVIDER)
    assert (report["source"], report["signed"], report["drafted"]) == (path, False, False)
    assert report["filters"] == {
        "limit": 5,
        "min_stars": 1,
        "pushed_within": 365,
        "include_forks": False,
    }
    assert report["queries"][0] == {
        "kind": "field",
        "term": "phone",
        "q": '"phone" "api.billing.test"',
        "total": 2,
        "pages": 1,
        "repositories": 2,
        "incomplete": False,
        "error": None,
    }
    assert report["counts"] == {
        "found": 2,
        "dropped": {},
        "eligible": 2,
        "checked": 2,
        "affected": 1,
        "unaffected": 0,
        "failed": 1,
    }
    app, broken = report["repositories"]
    assert list(app) == [
        "full_name",
        "url",
        "stars",
        "pushed_at",
        "default_branch",
        "fork",
        "affected",
        "reason",
        "error",
        "matches",
        "count",
        "files",
        "tests",
        "docs",
        "sdk_pins",
        "api_versions",
        "truncated",
        "clone",
    ]
    assert (app["full_name"], app["url"]) == ("acme/app", "https://github.com/acme/app")
    assert (app["stars"], app["default_branch"], app["affected"]) == (1505, "main", True)
    assert app["pushed_at"].endswith("Z")
    assert app["matches"] == [
        {
            "file": "client.py",
            "line": 6,
            "text": 'return customer["phone"]',
            "kind": "field",
            "term": "phone",
            "detail": "renamed to $.phone_number",
            "possibly_unused": True,
            "applies_to": None,
        }
    ]
    assert (broken["affected"], broken["reason"]) == (False, None)
    assert broken["error"].startswith("`git clone` failed")
    assert (report["kept"], report["error"]) == (None, None)


def test_hunt_takes_a_catalog_entry_and_says_who_drafted_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = {"llm.py": 'MODEL = "openai/gpt-oss-20b"\n'}
    searcher = github(
        monkeypatch, FakeCodeSearch([repo_facts("acme/app", source(tmp_path, "app", current))])
    )
    outcome = invoke("hunt", GROQ)
    assert outcome.exit_code == 0, outcome.output
    text = flat(outcome.output)
    origin = "unsigned: drafted by Tiltmeter from the provider's public documentation"
    assert f"{GROQ} · {origin}" in text
    assert "- acme/app 10 stars not affected:" in text
    assert f"None of the 1 repository checked still uses what {GROQ} changes." in text
    assert ('"llama3-8b-8192"', 1) in searcher.searched


def test_hunt_says_when_the_provider_signed_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github(monkeypatch, FakeCodeSearch([repo_facts("acme/app", source(tmp_path, "app"))]))
    path = notice_file(tmp_path, signed())
    with respx.mock:
        respx.get(KEY_URL).respond(200, text=KEY_FILE)
        as_json = invoke("hunt", path, "--json")
        as_text = invoke("hunt", path)
    assert as_json.exit_code == 1, as_json.output
    assert json.loads(as_json.stdout)["signed"] is True
    assert f"{NOTICE_ID} · signed by the provider" in flat(as_text.output)


def test_hunt_keeps_clones_and_passes_its_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = tmp_path / "kept"
    a_month_ago = datetime.now(UTC) - timedelta(days=30)
    facts = [
        repo_facts("acme/app", source(tmp_path, "app"), stars=5),
        repo_facts("acme/fork", source(tmp_path, "fork"), stars=50, fork=True),
        repo_facts("acme/quiet", tmp_path / "missing.git", stars=500, pushed_at=a_month_ago),
    ]
    github(monkeypatch, FakeCodeSearch(facts))
    args = ["--include-forks", "--pushed-within", "7", "-n", "1", "--keep", str(keep)]
    outcome = invoke("hunt", notice_file(tmp_path), *args)
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert "✗ acme/fork 50 stars" in text
    assert "left out: 1 not pushed in 7 days, 1 past --limit 1." in text
    assert f"clone: {keep / 'acme' / 'fork'}" in text
    assert f"The clones are in {keep}." in text
    assert (keep / "acme" / "fork" / "client.py").is_file()
    assert not (keep / "acme" / "app").exists()


def test_hunt_exits_3_when_github_cannot_be_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github(monkeypatch, FakeCodeSearch(error=HuntError(RATE_LIMITED)))
    outcome = invoke("hunt", notice_file(tmp_path))
    assert outcome.exit_code == 3, outcome.output
    text = flat(outcome.output)
    assert "✗ cannot search GitHub: GitHub's rate limit was reached" in text
    assert "wait a minute and run it again" in text

    github(monkeypatch, FakeCodeSearch(error=HuntError(NOT_LOGGED_IN)))
    outcome = invoke("hunt", notice_file(tmp_path), "--json")
    assert outcome.exit_code == 3
    report = json.loads(outcome.stdout)
    assert "run `gh auth login`" in report["error"]
    assert report["repositories"] == []


def test_hunt_usage_errors_exit_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = github(monkeypatch, FakeCodeSearch())
    unknown = invoke("hunt", "no-such-entry")
    assert unknown.exit_code == 4
    assert "no entry of the catalog bundled with Tiltmeter has that id" in flat(unknown.output)

    in_the_way = tmp_path / "file.txt"
    in_the_way.write_text("x", encoding="utf-8")
    blocked = invoke("hunt", notice_file(tmp_path), "--keep", str(in_the_way))
    assert blocked.exit_code == 4
    assert "cannot keep the clones in" in flat(blocked.output)
    assert searcher.searched == []


def test_hunt_shows_third_party_text_as_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markup = "[link=https://evil.test]click[/link] [bold red]"
    files = {"client.py": CLIENT.replace('["phone"]', f'["phone"]  # {markup}')}
    summary = "Customers [red]lose[/red] phone. Their balance is now a string."
    facts = [repo_facts("acme/app", source(tmp_path, "app", files))]
    github(monkeypatch, FakeCodeSearch(facts))
    outcome = invoke("hunt", notice_file(tmp_path, notice(summary=summary)))
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert "Customers [red]lose[/red] phone." in text
    assert f'return customer["phone"] # {markup}' in text
