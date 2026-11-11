from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rich.console import Console

from tests.fix.kit import rename_request
from tests.fix.test_request import notice_request, spec_request
from tests.fix.test_runner import notice_preview
from tests.notices.test_hunt import FakeGh, printed
from tests.test_cli import flat, invoke
from tiltmeter.catalog import LOCATION
from tiltmeter.cli import prs_exit_code
from tiltmeter.console import render_inbox
from tiltmeter.errors import InboxError
from tiltmeter.fix import inbox as inboxing
from tiltmeter.fix.inbox import (
    BATCH,
    MERGE_PAUSE,
    NEEDS_YOU,
    NOTICE,
    PER_PAGE,
    RATE_LIMITED,
    WAITING,
    Change,
    Checks,
    Decision,
    Found,
    FoundPage,
    GhPulls,
    InboxCounts,
    InboxFilters,
    InboxReport,
    PullFacts,
    PullState,
    Stance,
    Voice,
    Waiting,
    change_of,
    classify,
    facts_query,
    follow_up_command,
    footer_of,
    inbox,
    is_bot,
    parse_facts,
    parse_search,
    search_query,
)
from tiltmeter.fix.request import build_notice_request
from tiltmeter.fix.runner import FOOTER, GENERATED_BY, pull_request_body
from tiltmeter.notices.hunt import NO_CLI, NOT_LOGGED_IN
from tiltmeter.notices.reader import bundled_entry, parse_notice

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
AUTHOR = "octocat"
GROQ = "groq-retired-models-2026-08"
PHRASE = '"This pull request was generated automatically by"'
QUERY = f"is:pr author:@me in:body {PHRASE}"
URL = "https://github.com/acme/app/pull/45"


def at(day: int, hour: int = 12) -> datetime:
    """A moment in November 2026."""
    return datetime(2026, 11, day, hour, tzinfo=UTC)


# When the only commit of a pull request in these tests was made.
OPENED = at(1)


def body(origin: str = f"from `{GROQ}` in its catalog of known API changes") -> str:
    """A description that ends as Tiltmeter's pull requests do."""
    end = "An AI agent (m) wrote these edits; review them like any other change."
    return f"## What changed in the API\n\nA model was retired.\n\n---\n{FOOTER} {origin}. {end}\n"


# -- what GitHub is asked -----------------------------------------------------------


def test_the_search_asks_for_the_footer_tiltmeter_writes() -> None:
    assert search_query(InboxFilters()) == QUERY
    written = pull_request_body(rename_request(), notice_preview(), model_name="m")
    assert f"\n{GENERATED_BY} [Tiltmeter](" in written
    narrowed = InboxFilters(
        author="octo-cat", state="closed", repositories=["acme/app", "acme/.github"]
    )
    assert search_query(narrowed) == (
        f"is:pr author:octo-cat in:body {PHRASE} is:closed is:unmerged "
        "repo:acme/app repo:acme/.github"
    )
    assert search_query(InboxFilters(state="open")).endswith(" is:open")
    assert search_query(InboxFilters(state="merged")).endswith(" is:merged")


@pytest.mark.parametrize(
    "odd",
    [
        {"author": "octo cat"},
        {"author": "octocat\n"},
        {"repositories": ["acme/app\n"]},
        {"author": "octocat repo:acme/secret"},
        {"author": '"'},
        {"repositories": ["acme"]},
        {"repositories": ["acme/app is:open"]},
        {"repositories": ["acme/.."]},
        {"limit": 0},
        {"limit": 1001},
        {"state": "draft"},
    ],
)
def test_filters_take_only_what_github_names(odd: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        InboxFilters(**odd)


def item(
    repository: str = "acme/app",
    number: int = 45,
    *,
    state: str = "open",
    merged_at: str | None = None,
    text: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A pull request as ``search/issues`` answers it."""
    data: dict[str, Any] = {
        "repository_url": f"https://api.github.com/repos/{repository}",
        "html_url": f"https://github.com/{repository}/pull/{number}",
        "number": number,
        "title": "Replace the models api.groq.com retired",
        "user": {"login": AUTHOR, "type": "User"},
        "state": state,
        "draft": False,
        "created_at": "2026-10-27T05:20:14Z",
        "updated_at": "2026-11-07T16:20:22Z",
        "closed_at": merged_at or ("2026-11-02T09:00:00Z" if state == "closed" else None),
        "pull_request": {"html_url": f"https://github.com/{repository}/pull/{number}"},
        "body": body() if text is None else text,
    }
    data["pull_request"]["merged_at"] = merged_at
    data.update(overrides)
    return data


SEARCH_ANSWER: dict[str, Any] = {
    "total_count": 96,
    "incomplete_results": False,
    "items": [
        item(),
        item("acme/tool", 7, state="closed", merged_at="2026-10-30T10:00:00Z"),
        item("acme/old", 3, state="closed"),
        # Its description quotes the footer; it does not end with it.
        item("acme/meta", 26, text=f'Pull requests end with "{FOOTER} …", so one can tell.'),
        item("acme/ghost", 2, user=None, title=None, body=None),
        {
            "number": 9,
            "repository_url": "https://api.github.com/repos/acme/app",
            "title": "An issue",
        },
        item("acme/a b", 1),
        item("acme/app", 0),
        "nonsense",
    ],
}


def test_a_search_answer_becomes_pull_requests() -> None:
    page = parse_search(SEARCH_ANSWER)
    assert (page.total, page.results, page.incomplete) == (96, 9, False)
    first, merged, closed, quoting, ghost = page.pulls
    assert first == Found(
        repository="acme/app",
        number=45,
        title="Replace the models api.groq.com retired",
        author=AUTHOR,
        state="open",
        footer=f" from `{GROQ}` in its catalog of known API changes. An AI agent (m) wrote "
        "these edits; review them like any other change.",
        created_at=datetime(2026, 10, 27, 5, 20, 14, tzinfo=UTC),
        updated_at=datetime(2026, 11, 7, 16, 20, 22, tzinfo=UTC),
    )
    assert first.url == URL
    assert (merged.state, merged.closed_at) == ("merged", datetime(2026, 10, 30, 10, tzinfo=UTC))
    assert (closed.state, closed.closed_at) == ("closed", datetime(2026, 11, 2, 9, tzinfo=UTC))
    assert quoting.footer is None
    assert (ghost.author, ghost.title, ghost.footer) == ("ghost", "", None)
    assert parse_search({"items": [], "incomplete_results": True}).incomplete
    with pytest.raises(InboxError, match="something unexpected"):
        parse_search({"message": "Not Found"})


def test_only_a_line_that_starts_with_the_footer_counts() -> None:
    assert (footer_of(body("with `tiltmeter fix 12`")) or "").startswith(
        " with `tiltmeter fix 12`. "
    )
    # GitHub's editor saves CRLF, and a maintainer may add a line after the footer.
    edited = body().replace("\n", "\r\n") + "\r\nThanks for reading!\r\n"
    assert footer_of(edited) == footer_of(body())
    assert footer_of(f"> {FOOTER} from `x` in its catalog of known API changes.") is None
    assert footer_of(f"The footer says “{FOOTER} …”.") is None
    assert footer_of("") is None


# -- what a pull request was made from ----------------------------------------------


def catalog_request() -> Any:
    """A fix made from an entry of the bundled catalog, as ``fix --notice ID`` builds it."""
    entry = parse_notice(bundled_entry(GROQ))
    return build_notice_request(entry, location=f"{LOCATION}#{GROQ}", verified=False)


def test_the_footer_tiltmeter_writes_names_the_change() -> None:
    """Every end ``pull_request_body`` gives the footer is read back."""

    def written(request: Any) -> Change | None:
        footer = footer_of(pull_request_body(request, notice_preview(), model_name="m"))
        assert footer is not None
        return change_of(footer)

    assert written(rename_request(check_id=12)) == Change("check", "12")
    assert written(catalog_request()) == Change("catalog", GROQ)
    assert written(notice_request(verified=False)) == Change("unsigned", "2026-11-03-phone-rename")
    assert written(notice_request()) == Change("signed", "2026-11-03-phone-rename")
    assert written(spec_request()) is None  # a spec diff has no notice to name


def test_the_follow_up_command_names_the_change_when_it_can() -> None:
    tail = f"--follow-up {URL}"
    assert follow_up_command(URL, Change("check", "12")) == f"tiltmeter fix 12 {tail}"
    by_id = f"tiltmeter fix --notice {GROQ} --allow-unsigned {tail}"
    assert follow_up_command(URL, Change("catalog", GROQ)) == by_id
    # A notice read from a file whose id the catalog has: the entry describes the change.
    assert follow_up_command(URL, Change("unsigned", GROQ)) == by_id
    elsewhere = f"tiltmeter fix --notice {NOTICE} --allow-unsigned {tail}"
    assert follow_up_command(URL, Change("unsigned", "acme-2026-01")) == elsewhere
    assert follow_up_command(URL, Change("catalog", "an-entry-since-renamed")) == elsewhere
    unnamed = f"tiltmeter fix --notice {NOTICE} {tail}"
    # A signed notice came from a feed or a URL, which the footer does not say.
    assert follow_up_command(URL, Change("signed", GROQ)) == unnamed
    assert follow_up_command(URL, None) == unnamed


@pytest.mark.parametrize(
    "footer",
    [
        " from `x; rm -rf ~` in its catalog of known API changes. ",
        " from `$(reboot)` in its catalog of known API changes. ",
        " from the unsigned notice `a b` about api.acme.test's API. ",
        " with `tiltmeter fix 12; reboot`. ",
        " with `tiltmeter fix -1`. ",
        ", which follows the API specs a codebase depends on. ",
        "",
    ],
)
def test_a_rewritten_footer_names_no_change(footer: str) -> None:
    assert change_of(footer) is None


# -- the facts of an open pull request ----------------------------------------------


def said(
    login: str,
    when: datetime,
    text: str = "",
    *,
    kind: str = "User",
    role: str = "NONE",
    **more: Any,
) -> dict[str, Any]:
    """A comment as GraphQL answers it."""
    return {
        "author": {"login": login, "__typename": kind},
        "authorAssociation": role,
        "createdAt": when.isoformat(),
        "isMinimized": False,
        "bodyText": text,
        **more,
    }


def reviewed(
    login: str,
    when: datetime | None,
    state: str = "COMMENTED",
    text: str = "",
    *,
    kind: str = "User",
    role: str = "MEMBER",
    lines: Sequence[str] = (),
) -> dict[str, Any]:
    """A review as GraphQL answers it; ``lines`` are its notes on lines."""
    return {
        "author": {"login": login, "__typename": kind},
        "authorAssociation": role,
        "submittedAt": when.isoformat() if when is not None else None,
        "state": state,
        "bodyText": text,
        "comments": {"nodes": [{"bodyText": line} for line in lines[:1]]},
    }


def checks(state: str, *contexts: dict[str, Any]) -> dict[str, Any]:
    return {"state": state, "contexts": {"nodes": list(contexts)}}


def run(name: str, conclusion: str | None = "SUCCESS") -> dict[str, Any]:
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion}


def status(context: str, state: str = "SUCCESS") -> dict[str, Any]:
    return {"__typename": "StatusContext", "context": context, "state": state}


def node(
    *,
    comments: Sequence[dict[str, Any]] = (),
    reviews: Sequence[dict[str, Any]] = (),
    opinions: Sequence[str] = (),
    committed: datetime | None = OPENED,
    rollup: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """An open pull request nobody answered, as the facts query gets it."""
    pull: dict[str, Any] = {
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "reviewDecision": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "latestOpinionatedReviews": {"nodes": [{"state": state} for state in opinions]},
        "comments": {"nodes": list(comments)},
        "reviews": {"nodes": list(reviews)},
        "commits": {"nodes": []},
    }
    if committed is not None:
        commit = {"committedDate": committed.isoformat(), "statusCheckRollup": rollup}
        pull["commits"] = {"nodes": [{"commit": commit}]}
    pull.update(overrides)
    return {"pullRequest": pull}


def facts_of(answer: dict[str, Any]) -> PullFacts:
    [facts] = parse_facts({"data": {"p0": answer}}, 1)
    assert facts is not None
    return facts


def test_the_facts_of_a_quiet_pull_request() -> None:
    assert facts_of(node()) == PullFacts(base="main", committed_at=at(1), mergeable=True)
    assert facts_of(node(committed=None, baseRefName=None)) == PullFacts(mergeable=True)


def test_who_said_what_is_read_in_order_and_cut_short() -> None:
    long = "Could you keep the old model as a fallback? " * 10
    facts = facts_of(
        node(
            comments=[
                said("maintainer", at(5), f"  {long}\n\nThanks!\x1b[31m ", role="OWNER"),
                said(AUTHOR, at(6), "Done.", role="FIRST_TIME_CONTRIBUTOR"),
                said("spammer", at(7), "Buy now", isMinimized=True),
                {"author": None, "createdAt": at(8).isoformat(), "bodyText": "from a deleted user"},
                said("maintainer", at(9), "no date", createdAt="soon"),
            ],
            reviews=[
                reviewed("maintainer", at(4), "CHANGES_REQUESTED", "Not like this."),
                reviewed("maintainer", at(7), lines=["This line too.", "And this one."]),
                reviewed("maintainer", None, "PENDING", "A draft only its author sees."),
                reviewed("maintainer", at(3), "ODD"),
            ],
        )
    )
    heard = [(voice.author, voice.at, voice.kind, voice.stance) for voice in facts.voices]
    assert heard == [
        ("maintainer", at(4), "review", "changes_requested"),
        ("maintainer", at(5), "comment", None),
        (AUTHOR, at(6), "comment", None),
        ("maintainer", at(7), "review", "commented"),
    ]
    requested, asked, answer, on_lines = facts.voices
    assert (requested.text, requested.role) == ("Not like this.", "member")
    assert asked.role == "owner"
    assert asked.text.startswith("Could you keep the old model as a fallback? Could you")
    assert len(asked.text) == 140
    assert asked.text.endswith("…")
    assert "\n" not in asked.text
    assert answer.role == "first time contributor"
    assert on_lines.text == "This line too."


@pytest.mark.parametrize(
    ("login", "kind", "bot"),
    [
        ("github-actions", "Bot", True),
        ("coderabbitai", "Bot", True),
        ("dependabot[bot]", "User", True),
        ("CLAassistant", "User", True),
        ("codecov-commenter", "User", True),
        ("houndci-bot", "User", True),
        ("k8s-ci-robot", "User", True),
        ("acme_bot", None, True),
        ("maintainer", "User", False),
        ("abbot", "User", False),
        ("robotnik", "Organization", False),
    ],
)
def test_bots_are_told_from_people(login: str, kind: str | None, bot: bool) -> None:
    assert is_bot(login, kind) is bot
    [voice] = facts_of(node(comments=[said(login, at(2), "hello", kind=kind or "User")])).voices
    assert voice.bot is bot


@pytest.mark.parametrize(
    ("decided", "opinions", "decision"),
    [
        ("CHANGES_REQUESTED", [], "changes_requested"),
        ("APPROVED", ["CHANGES_REQUESTED"], "approved"),
        ("REVIEW_REQUIRED", ["APPROVED"], "review_required"),
        ("REVIEW_REQUIRED", [], "review_required"),
        # A branch that requires no review: GitHub decides nothing, the reviews still stand.
        (None, ["APPROVED", "CHANGES_REQUESTED"], "changes_requested"),
        (None, ["APPROVED"], "approved"),
        (None, ["DISMISSED"], None),
        (None, [], None),
    ],
)
def test_where_the_reviews_stand(
    decided: str | None, opinions: list[str], decision: Decision | None
) -> None:
    assert facts_of(node(reviewDecision=decided, opinions=opinions)).decision == decision


def test_how_the_checks_went() -> None:
    assert facts_of(node()).checks is None  # no check ran
    green = checks("SUCCESS", run("test"), run("docs", "SKIPPED"), status("ci/deploy"))
    assert facts_of(node(rollup=green)).checks == "passed"
    assert facts_of(node(rollup=green)).failed_checks == ()
    assert facts_of(node(rollup=checks("PENDING", run("test", None)))).checks == "pending"
    assert facts_of(node(rollup=checks("EXPECTED"))).checks == "pending"
    red = checks(
        "FAILURE",
        run("test (3.12)", "FAILURE"),
        run("test (3.12)", "FAILURE"),
        run("lint"),
        run("build", "TIMED_OUT"),
        run("cla", "ACTION_REQUIRED"),
        status("Vercel", "FAILURE"),
        status("ci/legacy", "ERROR"),
        run("  [red]odd[/red]\nname ", "FAILURE"),
    )
    failed = facts_of(node(rollup=red))
    assert failed.checks == "failed"
    assert failed.failed_checks == (
        "test (3.12)",
        "build",
        "cla",
        "Vercel",
        "ci/legacy",
        "[red]odd[/red] name",
    )
    # More checks than were read: GitHub's own verdict still counts.
    assert facts_of(node(rollup=checks("FAILURE", run("test")))).checks == "failed"
    assert facts_of(node(rollup=checks("ERROR"))).failed_checks == ()
    many = checks("FAILURE", *(run(f"job {n}", "FAILURE") for n in range(30)))
    assert len(facts_of(node(rollup=many)).failed_checks) == 10


@pytest.mark.parametrize(
    ("mergeable", "merge_state", "expected"),
    [
        ("MERGEABLE", "CLEAN", (True, False)),
        ("MERGEABLE", "BLOCKED", (True, False)),
        ("MERGEABLE", "UNSTABLE", (True, False)),
        ("MERGEABLE", "BEHIND", (True, True)),
        ("CONFLICTING", "DIRTY", (False, False)),
        ("UNKNOWN", "DIRTY", (False, False)),
        ("UNKNOWN", "UNKNOWN", (None, False)),
        (None, None, (None, False)),
    ],
)
def test_whether_the_branch_merges(
    mergeable: str | None, merge_state: str | None, expected: tuple[bool | None, bool]
) -> None:
    facts = facts_of(node(mergeable=mergeable, mergeStateStatus=merge_state))
    assert (facts.mergeable, facts.behind) == expected


def test_a_pull_request_that_moved_on_or_is_gone() -> None:
    assert facts_of(node(state="MERGED")).state == "merged"
    assert facts_of(node(state="CLOSED", isDraft=True)) == PullFacts(
        state="closed", draft=True, base="main", committed_at=at(1), mergeable=True
    )
    answer = {"data": {"p0": None, "p1": node(), "p2": {"pullRequest": None}}, "errors": [{}]}
    gone, there, hidden, never_answered = parse_facts(answer, 4)
    assert (gone, hidden, never_answered) == (None, None, None)
    assert there is not None
    with pytest.raises(InboxError, match="something unexpected"):
        parse_facts({"data": None}, 1)


def found(
    repository: str = "acme/app",
    number: int = 45,
    state: PullState = "open",
    **overrides: Any,
) -> Found:
    """A pull request Tiltmeter opened from the Groq entry of its catalog."""
    data: dict[str, Any] = {
        "title": "Replace the models api.groq.com retired",
        "author": AUTHOR,
        "footer": footer_of(body()),
        "created_at": at(1),
        "updated_at": at(7),
        "closed_at": None if state == "open" else at(8),
    }
    data.update(overrides)
    return Found(repository=repository, number=number, state=state, **data)


def test_one_query_asks_for_many_pull_requests() -> None:
    query = facts_query([found(), found("acme/.github", 7)])
    assert query.startswith("fragment facts on PullRequest { state isDraft baseRefName ")
    assert (
        'p0: repository(owner: "acme", name: "app") { pullRequest(number: 45) { ...facts } }'
        in (query)
    )
    assert 'p1: repository(owner: "acme", name: ".github") { pullRequest(number: 7) {' in query
    for field in (
        "reviewDecision mergeable mergeStateStatus",
        "latestOpinionatedReviews(first: 20, writersOnly: true) { nodes { state } }",
        "comments(last: 30) { nodes { author { login __typename } authorAssociation createdAt",
        "reviews(last: 30) { nodes { author { login __typename } authorAssociation submittedAt",
        "commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state",
        "... on CheckRun { name conclusion } ... on StatusContext { context state }",
    ):
        assert field in query


# -- what a pull request waits for --------------------------------------------------


def person(when: datetime, text: str = "Could you rename it?", **more: Any) -> Voice:
    return Voice(author="maintainer", at=when, role="owner", text=text, **more)


def own(when: datetime) -> Voice:
    return Voice(author=AUTHOR, at=when, text="Done.")


def review(when: datetime, stance: Stance = "commented", **more: Any) -> Voice:
    return Voice(author="maintainer", at=when, kind="review", role="owner", stance=stance, **more)


def facts(
    *voices: Voice,
    committed: datetime | None = OPENED,
    decision: Decision | None = None,
    ran: Checks | None = None,
    **more: Any,
) -> PullFacts:
    """A pull request whose only commit is from November 1, and what was said on it."""
    return PullFacts(
        base="main",
        voices=sorted(voices, key=lambda voice: voice.at),
        committed_at=committed,
        decision=decision,
        checks=ran,
        mergeable=more.pop("mergeable", True),
        **more,
    )


REQUESTED = review(at(3), "changes_requested", text="Keep the old model as a fallback.")
BOT = Voice(author="coderabbitai", at=at(9), bot=True, kind="review", stance="commented")


@pytest.mark.parametrize(
    ("given", "waits_for", "detail", "last_word"),
    [
        # 1. changes requested, until a commit follows the review that asked.
        (facts(REQUESTED, decision="changes_requested"), "changes_requested", None, at(3)),
        (
            facts(REQUESTED, own(at(4)), decision="changes_requested"),
            "changes_requested",
            None,
            at(3),
        ),
        (
            facts(REQUESTED, decision="changes_requested", committed=at(2)),
            "changes_requested",
            None,
            at(3),
        ),
        (
            facts(REQUESTED, decision="changes_requested", committed=at(4)),
            "maintainers",
            "changes were requested; a commit followed",
            at(3),
        ),
        (
            facts(REQUESTED, person(at(5)), decision="changes_requested", committed=at(4)),
            "answer",
            None,
            at(5),
        ),
        # The review that asked is older than the ones read: nothing says it was answered.
        (
            facts(decision="changes_requested", committed=at(4)),
            "changes_requested",
            None,
            None,
        ),
        (
            facts(REQUESTED, person(at(5)), decision="changes_requested", ran="failed"),
            "changes_requested",
            None,
            at(5),
        ),
        # 2. a person spoke after the author's last comment and the last commit.
        (facts(person(at(5))), "answer", None, at(5)),
        (facts(review(at(5))), "answer", None, at(5)),
        (facts(review(at(5), "dismissed")), "answer", None, at(5)),
        (facts(person(at(5)), own(at(4))), "answer", None, at(5)),
        (facts(person(at(5)), person(at(6), "And the docs?"), own(at(4))), "answer", None, at(6)),
        (facts(person(at(5)), committed=None), "answer", None, at(5)),
        (
            facts(person(at(5)), BOT, ran="failed", mergeable=False),
            "answer",
            "conflicts with main",
            at(5),
        ),
        # The author's own later comment answers it, and so does a later commit.
        (facts(person(at(5)), own(at(6))), "maintainers", None, at(5)),
        (facts(person(at(5)), committed=at(6)), "maintainers", None, at(5)),
        (facts(review(at(5)), committed=at(6)), "maintainers", None, at(5)),
        (facts(person(at(5)), Voice(author="OctoCat", at=at(6))), "maintainers", None, at(5)),
        # A bot is nobody to answer, and an approval asks nothing.
        (facts(BOT), "maintainers", None, None),
        (facts(Voice(author="CLAassistant", at=at(5), bot=True)), "maintainers", None, None),
        (facts(review(at(5), "approved")), "maintainers", None, at(5)),
        # 3. checks failed on the latest commit.
        (facts(ran="failed", failed_checks=["test"]), "checks_failed", None, None),
        (facts(person(at(5)), own(at(6)), ran="failed"), "checks_failed", None, at(5)),
        (facts(ran="failed", behind=True), "checks_failed", "behind main", None),
        (facts(decision="approved", ran="failed"), "checks_failed", None, None),
        # 4. behind or conflicting with its base.
        (facts(mergeable=False), "behind_or_conflicting", "conflicts with main", None),
        (facts(behind=True), "behind_or_conflicting", "behind main", None),
        (facts(mergeable=False, behind=True), "behind_or_conflicting", "conflicts with main", None),
        (
            facts(decision="approved", ran="passed", mergeable=False),
            "behind_or_conflicting",
            "conflicts with main",
            None,
        ),
        # 5. approved and green.
        (
            facts(review(at(5), "approved"), decision="approved", ran="passed"),
            "ready",
            "approved, checks passed",
            at(5),
        ),
        (facts(decision="approved"), "ready", "approved, no checks", None),
        (facts(decision="approved", mergeable=None), "ready", "approved, no checks", None),
        # 6. the maintainers, otherwise.
        (facts(), "maintainers", None, None),
        (facts(mergeable=None), "maintainers", None, None),
        (facts(ran="passed"), "maintainers", None, None),
        (facts(ran="pending"), "maintainers", "checks are running", None),
        (facts(decision="review_required", ran="passed"), "maintainers", "review required", None),
        (
            facts(decision="approved", ran="pending"),
            "maintainers",
            "approved; checks are running",
            None,
        ),
    ],
)
def test_what_an_open_pull_request_waits_for(
    given: PullFacts, waits_for: Waiting, detail: str | None, last_word: datetime | None
) -> None:
    waits = classify(AUTHOR, given)
    assert (waits.waits_for, waits.detail) == (waits_for, detail)
    assert (waits.last_word.at if waits.last_word else None) == last_word
    if waits.last_word is not None:
        assert waits.last_word.author == "maintainer"


def test_the_first_four_need_the_author() -> None:
    assert WAITING == (
        "changes_requested",
        "answer",
        "checks_failed",
        "behind_or_conflicting",
        "ready",
        "maintainers",
    )
    assert frozenset(WAITING[:4]) == NEEDS_YOU
    assert classify(AUTHOR, PullFacts(mergeable=False)).detail == "conflicts with its base branch"


# -- gh -----------------------------------------------------------------------------


def gh(monkeypatch: pytest.MonkeyPatch, *results: subprocess.CompletedProcess[str]) -> FakeGh:
    fake = FakeGh(*results)
    monkeypatch.setattr(inboxing, "run_tool", fake)
    return fake


def test_the_search_runs_gh_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = gh(monkeypatch, printed(SEARCH_ANSWER))
    page = GhPulls().search(QUERY, page=2)
    assert [pull.repository for pull in page.pulls][:2] == ["acme/app", "acme/tool"]
    start = ["gh", "api", "--hostname", "github.com", "-X", "GET", "search/issues"]
    fields = ["-f", f"q={QUERY}", "-f", "sort=created", "-f", "order=desc"]
    fields += ["-f", "per_page=100", "-f", "page=2"]
    assert fake.commands == [start + fields]
    assert fake.environments[0]["GH_PROMPT_DISABLED"] == "1"


NO_SUCH_USER = {
    "message": "Validation Failed",
    "errors": [
        {
            "message": "The listed users cannot be searched either because the users do not "
            "exist or you do not have permission to view the users.",
            "resource": "Search",
            "field": "q",
            "code": "invalid",
        }
    ],
    "status": "422",
}


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
        (
            printed(NO_SUCH_USER, stderr="gh: Validation Failed (HTTP 422)", code=1),
            r"GitHub refused the search \(The listed users cannot be searched .* view the "
            r"users\); check --author and --repo",
        ),
        (
            printed(stderr="gh: Validation Failed (HTTP 422)", code=1),
            r"GitHub refused the search \(gh: Validation Failed \(HTTP 422\)\); check --author",
        ),
        (
            printed("<html>", stderr="gh: HTTP 502", code=1),
            "`gh api search/issues` failed: gh: HTTP 502; run it again in a moment",
        ),
        (printed(stderr="gh: Not Found (HTTP 404)", code=1), "`gh api search/issues` failed"),
        (printed("<html>"), "not JSON"),
        (printed({"message": "odd"}), "something unexpected"),
    ],
)
def test_a_search_that_fails_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, result: subprocess.CompletedProcess[str], error: str
) -> None:
    gh(monkeypatch, result)
    with pytest.raises(InboxError, match=error):
        GhPulls().search(QUERY, page=1)


def test_github_is_only_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = gh(monkeypatch, printed(SEARCH_ANSWER), printed(answer(2)))
    source = GhPulls()
    source.describe(list(source.search(QUERY, page=1).pulls)[:2])
    search, read = fake.commands
    assert search[:7] == ["gh", "api", "--hostname", "github.com", "-X", "GET", "search/issues"]
    assert read[:5] == ["gh", "api", "--hostname", "github.com", "graphql"]
    assert len(read) == 7  # the query and nothing else: no variables, no input
    assert "\nquery {\n" in read[6]
    assert "mutation" not in read[6]


def test_the_inbox_needs_no_agent_and_no_fix_extra() -> None:
    script = (
        "import sys, tiltmeter.fix.inbox; "
        "print(sorted({name.split('.')[0] for name in sys.modules} "
        "& {'langchain', 'langgraph', 'deepagents', 'mcp', 'anthropic'}))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert loaded.stdout.strip() == "[]"


def test_a_missing_gh_says_how_to_get_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(InboxError) as caught:
        GhPulls().search(QUERY, page=1)
    assert str(caught.value) == NO_CLI
    assert "gh auth login" in NO_CLI


def answer(count: int, **more: Any) -> dict[str, Any]:
    """A facts answer for ``count`` quiet pull requests."""
    return {"data": {f"p{position}": node() for position in range(count)}, **more}


def test_facts_are_asked_a_few_pull_requests_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = gh(monkeypatch, printed(answer(BATCH)), printed(answer(2)))
    pulls = [found("acme/app", number) for number in range(1, BATCH + 3)]
    assert GhPulls().describe(pulls) == [facts_of(node())] * (BATCH + 2)
    first, second = fake.commands
    assert first[:6] == ["gh", "api", "--hostname", "github.com", "graphql", "-f"]
    assert first[6].startswith("query=fragment facts on PullRequest")
    assert f"p{BATCH - 1}: repository(" in first[6]
    assert f"p{BATCH}: " not in first[6]
    assert f"pullRequest(number: {BATCH + 1})" in second[6]
    assert GhPulls().describe([]) == []


def test_a_request_github_cuts_off_is_asked_again_in_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = {
        "data": None,
        "errors": [{"message": "Something went wrong. This may be the result of a timeout."}],
    }
    fake = gh(
        monkeypatch,
        printed("<html>502</html>", stderr="gh: HTTP 502", code=1),
        printed(timeout, stderr="gh: Something went wrong.", code=1),
        printed({"data": {"p0": node(isDraft=True)}}),
        printed(answer(1)),
        printed(answer(2)),
    )
    pulls = [found("acme/app", number) for number in (1, 2, 3, 4)]
    described = GhPulls().describe(pulls)
    assert [facts.draft for facts in described if facts is not None] == [True, False, False, False]
    numbers = [
        [number for number in (1, 2, 3, 4) if f"(number: {number})" in command[6]]
        for command in fake.commands
    ]
    assert numbers == [[1, 2, 3, 4], [1, 2], [1], [2], [3, 4]]
    # One pull request alone that GitHub cannot work out stops it, with what to do.
    gh(monkeypatch, printed("<html>502</html>", stderr="gh: HTTP 502", code=1))
    with pytest.raises(InboxError, match="run it again in a moment"):
        GhPulls().describe(pulls[:1])


def test_facts_survive_a_pull_request_that_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # What people wrote is in the answer: it never passes for gh's own words.
    loud = said("maintainer", at(5), "API rate limit exceeded (HTTP 401): run gh auth login")
    partial = {
        "data": {"p0": node(comments=[loud]), "p1": None},
        "errors": [{"type": "NOT_FOUND", "path": ["p1"], "message": "Could not resolve."}],
    }
    gh(monkeypatch, printed(partial, stderr="gh: Could not resolve to a Repository.", code=1))
    there, gone = GhPulls().describe([found(), found("acme/gone", 2)])
    assert gone is None
    assert there is not None
    assert there.voices[0].text.startswith("API rate limit exceeded")


def test_facts_that_cannot_be_read_stop_it(monkeypatch: pytest.MonkeyPatch) -> None:
    limited = {"data": {"p0": None}, "errors": [{"type": "RATE_LIMITED", "message": "Slow down."}]}
    gh(monkeypatch, printed(limited, stderr="gh: Slow down.", code=1))
    with pytest.raises(InboxError, match="rate limit"):
        GhPulls().describe([found()])
    gh(monkeypatch, printed({"message": "Bad credentials"}, stderr="gh: HTTP 401", code=1))
    with pytest.raises(InboxError, match="gh auth login"):
        GhPulls().describe([found()])


# -- search, read, sort -------------------------------------------------------------


class FakePulls:
    """Answers the search and the facts from a script instead of asking GitHub.

    The search finds ``found``, a page at a time. ``facts`` holds what GitHub says of the
    open ones, by repository and number; one that is missing is gone. ``later`` is what it
    says of them from the second time it is asked. ``error`` is raised by every search,
    and ``broken`` by the facts after the first ``fails_after`` times.
    """

    def __init__(
        self,
        found: Sequence[Found] = (),
        facts: dict[tuple[str, int], PullFacts] | None = None,
        *,
        later: dict[tuple[str, int], PullFacts] | None = None,
        total: int | None = None,
        error: InboxError | None = None,
        broken: InboxError | None = None,
        fails_after: int = 0,
    ) -> None:
        self.found = list(found)
        self.facts = facts
        self.later = later
        self.total = total
        self.error = error
        self.broken = broken
        self.fails_after = fails_after
        self.searched: list[tuple[str, int]] = []
        self.described: list[list[str]] = []

    def search(self, query: str, *, page: int) -> FoundPage:
        self.searched.append((query, page))
        if self.error is not None:
            raise self.error
        shown = self.found[(page - 1) * PER_PAGE : page * PER_PAGE]
        total = len(self.found) if self.total is None else self.total
        return FoundPage(total=total, pulls=shown, results=len(shown))

    def describe(self, pulls: Sequence[Found]) -> list[PullFacts | None]:
        self.described.append([f"{pull.repository}#{pull.number}" for pull in pulls])
        if self.broken is not None and len(self.described) > self.fails_after:
            raise self.broken
        if self.facts is None:
            return [PullFacts(mergeable=True) for _ in pulls]
        known = {**self.facts, **(self.later or {})} if len(self.described) > 1 else self.facts
        return [known.get((pull.repository, pull.number)) for pull in pulls]


ASKED = person(at(9, 8), "Thanks! Could you also update the README?")
INBOX = [
    found("acme/quiet", 1, updated_at=at(2)),
    found("acme/merged", 2, "merged"),
    found("acme/red", 3, footer=" with `tiltmeter fix 12`. An AI agent (m) wrote these edits."),
    found("acme/app", 45, updated_at=at(9)),
    found("acme/closed", 5, "closed"),
    found("acme/approved", 6, updated_at=at(8)),
    found("acme/stale", 7, footer=", which follows the API specs a codebase depends on."),
    found("acme/review", 8, footer=footer_of(body("from api.acme.test's change notice `n-1`"))),
    found("acme/later", 9, updated_at=at(9)),
]
INBOX_FACTS = {
    ("acme/quiet", 1): facts(),
    ("acme/red", 3): facts(ran="failed", failed_checks=["lint", "test (3.12)", "docs", "e2e"]),
    ("acme/app", 45): facts(ASKED, own(at(8))),
    ("acme/approved", 6): facts(review(at(8), "approved"), decision="approved", ran="passed"),
    ("acme/stale", 7): facts(mergeable=False),
    ("acme/review", 8): facts(REQUESTED, decision="changes_requested", draft=True),
    ("acme/later", 9): facts(mergeable=None),
}


def test_the_ones_that_need_you_come_first() -> None:
    source = FakePulls(INBOX, INBOX_FACTS)
    heard: list[str] = []
    report = inbox(source, progress=heard.append)
    assert report.error is None
    assert report.counts == InboxCounts(opened=9, merged=1, closed=1, open=7, need_you=4)
    assert [(pull.repository, pull.state, pull.waits_for) for pull in report.pulls] == [
        ("acme/review", "open", "changes_requested"),
        ("acme/app", "open", "answer"),
        ("acme/red", "open", "checks_failed"),
        ("acme/stale", "open", "behind_or_conflicting"),
        ("acme/approved", "open", "ready"),
        ("acme/later", "open", "maintainers"),
        ("acme/quiet", "open", "maintainers"),
        ("acme/merged", "merged", None),
        ("acme/closed", "closed", None),
    ]
    assert [pull.needs_you for pull in report.pulls] == [True] * 4 + [False] * 5
    # Only the open ones are asked about: the search said enough of the others.
    asked = ["acme/quiet#1", "acme/red#3", "acme/app#45", "acme/approved#6", "acme/stale#7"]
    # And once more about the one GitHub could not yet say merges or not.
    assert source.described == [[*asked, "acme/review#8", "acme/later#9"], ["acme/later#9"]]
    assert source.searched == [(QUERY, 1)]
    assert heard == [
        "searching GitHub for the pull requests Tiltmeter opened",
        "reading 7 open pull requests, 10 per request",
        "reading 1 pull request again: GitHub had not worked out whether it merges",
    ]
    review_, app, red, stale, approved, *_, merged, closed = report.pulls
    assert (app.url, app.title, app.author) == (
        URL,
        "Replace the models api.groq.com retired",
        AUTHOR,
    )
    assert app.last_word is not None
    assert app.last_word.model_dump() == {
        "author": "maintainer",
        "role": "owner",
        "kind": "comment",
        "stance": None,
        "at": at(9, 8),
        "excerpt": "Thanks! Could you also update the README?",
    }
    assert (app.change, app.mergeable, app.draft) == (GROQ, True, False)
    assert app.follow_up == f"tiltmeter fix --notice {GROQ} --allow-unsigned --follow-up {URL}"
    assert red.follow_up == "tiltmeter fix 12 --follow-up https://github.com/acme/red/pull/3"
    assert (red.change, red.failed_checks) == ("12", ["lint", "test (3.12)", "docs", "e2e"])
    assert review_.follow_up == (
        f"tiltmeter fix --notice {NOTICE} --follow-up https://github.com/acme/review/pull/8"
    )
    assert (review_.change, review_.draft) == ("n-1", True)
    assert review_.last_word is not None
    assert review_.last_word.stance == "changes_requested"
    # A branch to update is not what `fix --follow-up` does, and the rest needs no answer.
    assert (stale.follow_up, stale.detail, stale.change) == (None, "conflicts with main", None)
    assert (approved.follow_up, approved.detail) == (None, "approved, checks passed")
    assert (merged.closed_at, merged.waits_for, merged.follow_up) == (at(8), None, None)
    assert closed.change == GROQ


def test_a_branch_github_had_not_worked_out_is_asked_about_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inboxing, "MERGE_PAUSE", 2.5)
    pulls = [found("acme/app", 45), found("acme/tool", 7), found("acme/done", 2)]
    unsure = {
        ("acme/app", 45): facts(mergeable=None),
        ("acme/tool", 7): facts(mergeable=None, decision="approved"),
        ("acme/done", 2): facts(),
    }
    worked_out = {("acme/app", 45): facts(mergeable=False), ("acme/tool", 7): facts(behind=True)}
    source = FakePulls(pulls, unsure, later=worked_out)
    pauses: list[float] = []
    heard: list[str] = []
    report = inbox(source, progress=heard.append, sleep=pauses.append)
    assert pauses == [2.5]  # time for GitHub to work it out
    assert MERGE_PAUSE > 0
    assert source.described[1] == ["acme/app#45", "acme/tool#7"]
    assert (
        heard[-1] == "reading 2 pull requests again: GitHub had not worked out whether they merge"
    )
    assert [(pull.repository, pull.waits_for, pull.detail) for pull in report.pulls] == [
        ("acme/app", "behind_or_conflicting", "conflicts with main"),
        ("acme/tool", "behind_or_conflicting", "behind main"),
        ("acme/done", "maintainers", None),
    ]

    # Still unknown, or GitHub cannot be asked again: what it said first stands.
    for source in (
        FakePulls(pulls, unsure),
        FakePulls(pulls, unsure, broken=InboxError(RATE_LIMITED), fails_after=1),
        FakePulls(pulls, unsure, later={("acme/app", 45): PullFacts(state="merged")}),
    ):
        report = inbox(source, sleep=pauses.append)
        assert report.error is None
        assert len(source.described) == 2
        tool = next(pull for pull in report.pulls if pull.repository == "acme/tool")
        assert (tool.waits_for, tool.mergeable) == ("ready", None)
    assert report.counts.merged == 1  # the second read is the fresher one

    # Nothing is asked twice when GitHub knew, or when the pull request is not open.
    source = FakePulls(
        pulls, {("acme/app", 45): facts(), ("acme/tool", 7): PullFacts(state="closed")}
    )
    inbox(source, sleep=pauses.append)
    assert len(source.described) == 1
    assert len(pauses) == 4


def test_the_search_is_read_a_page_at_a_time_up_to_the_limit() -> None:
    quoting = found("acme/meta", 26, footer=None)
    everything = [found("acme/app", number, "merged") for number in range(1, 251)]
    everything.insert(3, quoting)
    source = FakePulls(everything)
    report = inbox(source)
    assert source.searched == [(QUERY, 1), (QUERY, 2), (QUERY, 3)]
    assert (report.found, report.not_read, report.unrelated) == (251, 50, 1)
    assert report.counts.opened == 200
    assert source.described == []  # none is open

    source = FakePulls(everything)
    report = inbox(source, filters=InboxFilters(limit=1000))
    assert source.searched == [(QUERY, 1), (QUERY, 2), (QUERY, 3)]
    assert (report.counts.opened, report.not_read, report.unrelated) == (250, 0, 1)

    source = FakePulls(everything)
    report = inbox(source, filters=InboxFilters(limit=3))
    assert source.searched == [(QUERY, 1)]
    assert (report.counts.opened, report.not_read, report.unrelated) == (3, 248, 0)

    # A full last page, and GitHub's count says there is no other.
    source = FakePulls(everything[:100])
    assert inbox(source).counts.opened == 99
    assert source.searched == [(QUERY, 1)]
    # GitHub serves the first 1,000 results and counts the rest.
    source = FakePulls([found("acme/app", n, "closed") for n in range(1, 1001)], total=1500)
    report = inbox(source, filters=InboxFilters(limit=1000))
    assert len(source.searched) == 10
    assert (report.counts.opened, report.found, report.not_read) == (1000, 1500, 500)


def test_a_pull_request_found_twice_counts_once() -> None:
    twice = [found("acme/app", 45), found("Acme/App", 45), found("acme/app", 46)]
    report = inbox(FakePulls(twice))
    assert [(pull.repository, pull.number) for pull in report.pulls] == [
        ("acme/app", 45),
        ("acme/app", 46),
    ]


def test_the_filters_reach_the_search() -> None:
    source = FakePulls()
    chosen = InboxFilters(author="octo-cat", state="merged", repositories=["acme/app"], limit=5)
    report = inbox(source, filters=chosen)
    assert source.searched == [
        (f"is:pr author:octo-cat in:body {PHRASE} is:merged repo:acme/app", 1)
    ]
    assert (report.query, report.filters) == (source.searched[0][0], chosen)
    assert report.counts == InboxCounts()


def test_a_pull_request_that_moved_on_since_the_search() -> None:
    moved = {
        ("acme/app", 45): PullFacts(state="merged"),
        ("acme/tool", 7): PullFacts(state="closed"),
    }
    report = inbox(FakePulls([found(), found("acme/tool", 7), found("acme/gone", 2)], moved))
    gone, merged, closed = report.pulls
    assert (merged.state, merged.waits_for, merged.needs_you) == ("merged", None, False)
    assert (closed.state, closed.waits_for) == ("closed", None)
    assert (gone.state, gone.waits_for, gone.needs_you) == ("open", None, False)
    assert gone.error == "GitHub did not describe it: it may be gone, or hidden from this login"
    assert report.counts == InboxCounts(opened=3, merged=1, closed=1, open=1, unread=1)


def test_github_that_cannot_be_asked_is_the_report_s_error() -> None:
    report = inbox(FakePulls(error=InboxError(NOT_LOGGED_IN)))
    assert report.error == NOT_LOGGED_IN
    assert (report.pulls, report.counts) == ([], InboxCounts())
    source = FakePulls([found()], broken=InboxError("`gh api graphql` failed:\n[red]odd[/red]"))
    report = inbox(source)
    assert report.error == "`gh api graphql` failed: [red]odd[/red]"
    assert report.pulls == []


def test_exit_codes() -> None:
    def report(**counts: int) -> InboxReport:
        return InboxReport(
            filters=InboxFilters(), query=QUERY, counts=InboxCounts(**counts), error=None
        )

    assert prs_exit_code(report()) == 0
    assert prs_exit_code(report(opened=9, merged=5, closed=1, open=3)) == 0
    assert prs_exit_code(report(opened=3, open=3, need_you=1)) == 1
    assert prs_exit_code(report(opened=3, open=3, need_you=1, unread=1)) == 1
    assert prs_exit_code(report(opened=3, open=3, unread=1)) == 0
    assert prs_exit_code(report(opened=4, merged=1, open=3, unread=3)) == 3
    failed = report(opened=3, open=3, need_you=1)
    failed.error = RATE_LIMITED
    assert prs_exit_code(failed) == 3


# -- rendering ----------------------------------------------------------------------


def shown(report: InboxReport, *, show_all: bool = False) -> str:
    """What ``tiltmeter prs`` prints for a report on November 10 at noon, on one line."""
    console = Console(file=io.StringIO(), width=200, highlight=False)
    render_inbox(console, report, show_all=show_all, now=NOW)
    assert isinstance(console.file, io.StringIO)
    return flat(console.file.getvalue())


def test_each_pull_request_that_needs_you_says_why_who_and_what_to_run() -> None:
    text = shown(inbox(FakePulls(INBOX, INBOX_FACTS)))
    assert (
        "! acme/app#45 Replace the models api.groq.com retired waits for an answer "
        "last word: maintainer (owner) commented 1 day ago: Thanks! Could you also update the "
        f"README? tiltmeter fix --notice {GROQ} --allow-unsigned --follow-up {URL}"
    ) in text
    assert (
        "! acme/review#8 Replace the models api.groq.com retired changes requested (draft) "
        "last word: maintainer (owner) requested changes 7 days ago: Keep the old model as a "
        f"fallback. tiltmeter fix --notice {NOTICE} --follow-up "
        "https://github.com/acme/review/pull/8"
    ) in text
    assert (
        "! acme/red#3 Replace the models api.groq.com retired checks failed: lint, test (3.12), "
        "docs and 1 more tiltmeter fix 12 --follow-up https://github.com/acme/red/pull/3"
    ) in text
    assert (
        "! acme/stale#7 Replace the models api.groq.com retired conflicts with main update its "
        "branch by hand: `tiltmeter fix --follow-up` does not rebase or merge "
        "https://github.com/acme/stale/pull/7"
    ) in text
    assert (
        "✓ acme/approved#6 Replace the models api.groq.com retired ready: approved, checks "
        "passed · updated 2 days ago"
    ) in text
    waiting = "Replace the models api.groq.com retired waiting for the maintainers · updated"
    assert f"- acme/later#9 {waiting} 1 day ago" in text
    assert f"- acme/quiet#1 {waiting} 8 days ago" in text
    assert "9 opened: 1 merged, 1 closed, 7 open (4 need you)." in text
    order = ["acme/review#8", "acme/app#45", "acme/red#3", "acme/stale#7", "acme/approved#6"]
    order += ["acme/later#9", "acme/quiet#1", "9 opened"]
    assert [text.index(name) for name in order] == sorted(text.index(name) for name in order)
    assert "Run a command in a checkout of that repository that has the pull request's branch" in (
        text
    )
    assert (
        f"{NOTICE} is the notice the fix was made from, as `tiltmeter fix --notice` takes it"
        in (text)
    )
    assert (
        "GitHub has not worked out yet whether 1 of the open ones merges without conflicts; a "
        "later run will know."
    ) in text
    # Merged and closed ones are counted, and listed only when asked.
    assert "acme/merged" not in text
    assert "acme/closed" not in text
    everything = shown(inbox(FakePulls(INBOX, INBOX_FACTS)), show_all=True)
    assert "✓ acme/merged#2 Replace the models api.groq.com retired merged 2026-11-08" in everything
    assert "× acme/closed#5 Replace the models api.groq.com retired closed 2026-11-08" in everything
    assert everything.index("acme/quiet#1") < everything.index("acme/merged#2")


def test_a_branch_to_update_is_said_next_to_a_surer_reason() -> None:
    behind = {("acme/app", 45): facts(ASKED, behind=True)}
    text = shown(inbox(FakePulls([found()], behind)))
    assert "waits for an answer; behind main last word: maintainer (owner) commented" in text
    assert f"--follow-up {URL}" in text
    assert "1 opened: 0 merged, 0 closed, 1 open (1 needs you)." in text
    assert NOTICE not in text


def test_the_summary_follows_the_state_asked_for() -> None:
    def summary(state: Any, pulls: Sequence[Found]) -> str:
        return shown(inbox(FakePulls(pulls), filters=InboxFilters(state=state)), show_all=True)

    waiting = [found("acme/app", 45), found("acme/tool", 7)]
    assert summary("open", waiting).endswith("2 open (none needs you).")
    assert summary("all", waiting).endswith(
        "2 opened: 0 merged, 0 closed, 2 open (none needs you)."
    )
    assert summary("merged", [found("acme/app", 45, "merged")]).endswith("1 merged.")
    assert summary("closed", [found("acme/app", 45, "closed", closed_at=None)]).endswith(
        "× acme/app#45 Replace the models api.groq.com retired closed 1 closed."
    )
    assert shown(inbox(FakePulls())) == (
        "No pull request that ends with Tiltmeter's footer was found for the login the GitHub "
        "CLI holds."
    )
    narrowed = InboxFilters(author="octo-cat", state="merged", repositories=["acme/app"])
    assert shown(inbox(FakePulls(), filters=narrowed)) == (
        "No merged pull request that ends with Tiltmeter's footer was found for octo-cat in "
        "the repositories named."
    )


def test_what_was_not_read_is_said() -> None:
    many = [found("acme/app", number, "merged") for number in range(1, 13)]
    text = shown(inbox(FakePulls(many), filters=InboxFilters(limit=10)))
    assert "10 opened: 10 merged, 0 closed, 0 open (none needs you)." in text
    assert "note: 2 more results were not read: raise --limit to read them." in text
    capped = inbox(FakePulls(many[:1], total=1001), filters=InboxFilters(limit=1000))
    assert capped.not_read == 1000
    assert "note: 1,000 more results were not read: GitHub serves" in shown(capped)
    assert "note: 1 more result was not read" in shown(
        inbox(FakePulls(many[:2]), filters=InboxFilters(limit=1))
    )
    assert (
        "GitHub serves the first 1,000 results of a search, so narrow it with --state or --repo"
        in (shown(capped))
    )
    timed_out = inbox(FakePulls(many[:1]))
    timed_out.incomplete = True
    assert "note: GitHub's search timed out, so results are missing; run it again." in (
        shown(timed_out)
    )
    unread = shown(inbox(FakePulls([found()], {})))
    assert (
        "! acme/app#45 Replace the models api.groq.com retired could not be read: GitHub did not "
        "describe it"
    ) in unread


# -- tiltmeter prs ------------------------------------------------------------------


def github(monkeypatch: pytest.MonkeyPatch, source: FakePulls) -> FakePulls:
    """Make ``tiltmeter prs`` ask ``source`` instead of GitHub."""
    monkeypatch.setattr(inboxing, "GhPulls", lambda: source)
    return source


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never wait for GitHub to work out whether a branch merges."""
    monkeypatch.setattr(inboxing, "MERGE_PAUSE", 0.0)


def test_prs_lists_what_needs_you_first(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = datetime.now(UTC) - timedelta(days=3, hours=1)
    pulls = [
        found("acme/quiet", 1, updated_at=recent),
        found("acme/app", 45, updated_at=recent),
        found("acme/merged", 2, "merged"),
    ]
    spoken = person(recent, "Thanks! Could you also update the README?")
    asked = facts(spoken, committed=recent - timedelta(days=9))
    github(monkeypatch, FakePulls(pulls, {("acme/quiet", 1): facts(), ("acme/app", 45): asked}))
    outcome = invoke("prs")
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert "searching GitHub for the pull requests Tiltmeter opened" in text  # progress, on stderr
    assert "reading 2 open pull requests" in text
    assert "! acme/app#45 Replace the models api.groq.com retired waits for an answer" in text
    assert (
        "last word: maintainer (owner) commented 3 days ago: Thanks! Could you also update the "
        "README?"
    ) in text
    assert f"tiltmeter fix --notice {GROQ} --allow-unsigned --follow-up {URL}" in text
    assert (
        "- acme/quiet#1 Replace the models api.groq.com retired waiting for the maintainers · "
        "updated 3 days ago"
    ) in text
    assert "3 opened: 1 merged, 0 closed, 2 open (1 needs you)." in text
    assert "acme/merged" not in text
    assert text.index("acme/app#45") < text.index("acme/quiet#1") < text.index("3 opened")


def test_prs_exits_0_when_nothing_needs_you(monkeypatch: pytest.MonkeyPatch) -> None:
    source = github(monkeypatch, FakePulls([found(), found("acme/merged", 2, "merged")]))
    outcome = invoke("prs")
    assert outcome.exit_code == 0, outcome.output
    text = flat(outcome.output)
    assert "2 opened: 1 merged, 0 closed, 1 open (none needs you)." in text
    assert "Run a command" not in text
    assert source.searched == [(QUERY, 1)]


def test_prs_passes_its_options_and_lists_the_state_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = github(monkeypatch, FakePulls([found("acme/app", 45, "merged")]))
    args = ["--author", "octo-cat", "--repo", "acme/app", "--repo", "acme/tool", "-n", "5"]
    outcome = invoke("prs", "--state", "merged", *args)
    assert outcome.exit_code == 0, outcome.output
    assert source.searched == [
        (f"is:pr author:octo-cat in:body {PHRASE} is:merged repo:acme/app repo:acme/tool", 1)
    ]
    text = flat(outcome.output)
    assert "✓ acme/app#45 Replace the models api.groq.com retired merged 2026-11-08" in text
    assert text.endswith("1 merged.")
    listed = flat(invoke("prs", "--state", "all").output)
    assert "✓ acme/app#45" in listed
    assert "1 opened: 1 merged, 0 closed, 0 open (none needs you)." in listed
    assert "acme/app#45" not in flat(invoke("prs", "--state", "open").output)
    assert source.searched[-1] == (f"{QUERY} is:open", 1)


def test_prs_prints_json(monkeypatch: pytest.MonkeyPatch) -> None:
    github(monkeypatch, FakePulls(INBOX, INBOX_FACTS))
    outcome = invoke("prs", "--json", "--limit", "50")
    assert outcome.exit_code == 1, outcome.output
    report = json.loads(outcome.stdout)
    assert list(report) == [
        "filters",
        "query",
        "found",
        "not_read",
        "unrelated",
        "incomplete",
        "counts",
        "pulls",
        "error",
    ]
    assert report["filters"] == {"author": None, "state": "all", "repositories": [], "limit": 50}
    assert (report["query"], report["found"], report["error"]) == (QUERY, 9, None)
    assert report["counts"] == {
        "opened": 9,
        "merged": 1,
        "closed": 1,
        "open": 7,
        "need_you": 4,
        "unread": 0,
    }
    # Everything found is there, whatever the terminal would list.
    assert [pull["state"] for pull in report["pulls"]] == ["open"] * 7 + ["merged", "closed"]
    app = report["pulls"][1]
    assert app == {
        "repository": "acme/app",
        "number": 45,
        "title": "Replace the models api.groq.com retired",
        "url": URL,
        "author": AUTHOR,
        "state": "open",
        "draft": False,
        "created_at": "2026-11-01T12:00:00Z",
        "updated_at": "2026-11-09T12:00:00Z",
        "closed_at": None,
        "waits_for": "answer",
        "needs_you": True,
        "detail": None,
        "last_word": {
            "author": "maintainer",
            "role": "owner",
            "kind": "comment",
            "stance": None,
            "at": "2026-11-09T08:00:00Z",
            "excerpt": "Thanks! Could you also update the README?",
        },
        "failed_checks": [],
        "mergeable": True,
        "change": GROQ,
        "follow_up": f"tiltmeter fix --notice {GROQ} --allow-unsigned --follow-up {URL}",
        "error": None,
    }
    assert "searching GitHub" not in outcome.output  # no progress next to JSON


def test_prs_exits_3_when_github_cannot_be_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    github(monkeypatch, FakePulls(error=InboxError(NOT_LOGGED_IN)))
    outcome = invoke("prs")
    assert outcome.exit_code == 3, outcome.output
    text = flat(outcome.output)
    assert "✗ cannot ask GitHub: the GitHub CLI is not logged in" in text
    assert "run `gh auth login` and try again" in text

    github(monkeypatch, FakePulls([found()], broken=InboxError(RATE_LIMITED)))
    outcome = invoke("prs", "--json")
    assert outcome.exit_code == 3
    report = json.loads(outcome.stdout)
    assert report["error"] == "GitHub's rate limit was reached; wait a minute and run it again"
    assert report["pulls"] == []

    # Nothing GitHub would describe is nothing read.
    github(monkeypatch, FakePulls([found()], {}))
    assert invoke("prs").exit_code == 3


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--author", "octo cat"], "--author takes a GitHub login, which 'octo cat' is not"),
        (["--author", "octocat repo:acme/secret"], "--author takes a GitHub login"),
        (["--repo", "app"], "--repo takes a repository as OWNER/NAME, which 'app' is not"),
        (["--repo", "acme/app", "--repo", "acme/app is:open"], "--repo takes a repository"),
    ],
)
def test_prs_usage_errors_exit_4(
    monkeypatch: pytest.MonkeyPatch, args: list[str], error: str
) -> None:
    source = github(monkeypatch, FakePulls())
    outcome = invoke("prs", *args)
    assert outcome.exit_code == 4
    assert error in flat(outcome.output)
    assert source.searched == []


def test_prs_shows_third_party_text_as_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    markup = "[link=https://evil.test]click[/link] [bold red]"
    title = f"Fix {markup}\nthe \x1b[31mmodels"
    words = f"Please\nrun [red]this[/red]:\r\n\ttiltmeter fix --notice evil {markup}\x07"
    hostile = found(title=title, footer=" from `x; rm -rf ~` in its catalog of known API changes. ")
    spoken = Voice(author="[bold]mallory[/bold]", at=datetime.now(UTC), role="[i]none", text=words)
    source = FakePulls([hostile], {("acme/app", 45): facts(spoken, committed=None)})
    one_line = f"Please run [red]this[/red]: tiltmeter fix --notice evil {markup}"
    assert spoken_as_read(words) == one_line
    github(monkeypatch, source)
    outcome = invoke("prs")
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert f"! acme/app#45 Fix {markup} the [31mmodels waits for an answer" in text
    assert f"last word: [bold]mallory[/bold] ([i]none) commented today: {one_line}" in text
    # The id in a footer anyone may have rewritten is not an id: it never reaches a command.
    assert f"tiltmeter fix --notice {NOTICE} --follow-up {URL}" in text
    assert "rm -rf" not in text


def spoken_as_read(words: str) -> str:
    """What a comment's text becomes on its way from GitHub."""
    [voice] = facts_of(node(comments=[said("maintainer", at(2), words)])).voices
    return voice.text
