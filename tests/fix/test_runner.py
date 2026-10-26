from __future__ import annotations

from pathlib import Path

import pytest

from tests.fix.kit import PR_URL, FakeFixAgent, FakeGitHub, git, make_repo, rename_request, report
from tests.fix.test_request import notice_request, spec_request
from tests.notices.kit import notice, signed
from tiltmeter.errors import FixError
from tiltmeter.fix.models import FixPreview
from tiltmeter.fix.request import build_notice_request
from tiltmeter.fix.runner import (
    MAX_CHANGES_IN_BODY,
    FixSession,
    commit_body,
    commit_title,
    default_branch,
    pull_request_body,
)
from tiltmeter.notices.models import ApiChangeNotice

BRANCH = "tiltmeter/fix-billing-customers-12"


async def agent_pass(session: FixSession, agent: FakeFixAgent | None = None) -> None:
    await (agent or FakeFixAgent()).run(session.request, session.root)


def test_default_branch_is_a_valid_ref() -> None:
    assert default_branch(rename_request()) == BRANCH
    assert default_branch(rename_request(3), "bots/api") == "bots/api-billing-customers-3"


async def test_opened_pull_request(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    github = FakeGitHub()
    with FixSession.open(repo, rename_request()) as session:
        assert session.branch == BRANCH
        assert session.base == "main"
        await agent_pass(session)
        preview = session.review(report())
        assert preview.files == ["client.py", "models.py"]
        assert (preview.additions, preview.deletions) == (2, 2)
        assert '-    return customer["phone"]' in preview.diff
        outcome = session.deliver(preview, push=True, github=github, model_name="fake-fixer")
        worktree = session.root

    assert outcome.status == "opened"
    assert outcome.pr_url == PR_URL
    assert outcome.branch == BRANCH
    assert outcome.error is None
    [pr] = github.created
    assert pr["head"] == BRANCH
    assert pr["base"] == "main"
    assert pr["title"] == "Adapt to the billing_customers API change"
    assert (
        "| breaking | field_removed | `$.phone` | string | (absent), probably renamed to"
        in pr["body"]
    )
    assert "Read phone_number instead of phone." in pr["body"]
    assert "fake-fixer" in pr["body"]

    assert git(remote, "rev-parse", "--verify", BRANCH).strip()
    assert 'customer["phone_number"]' in git(remote, "show", f"{BRANCH}:client.py")
    assert not worktree.exists()
    assert git(repo, "status", "--porcelain") == ""  # the checkout is untouched
    assert 'customer["phone"]' in (repo / "client.py").read_text(encoding="utf-8")
    assert git(repo, "branch", "--list", BRANCH).strip()  # the branch stays for the PR


async def test_discarding_leaves_nothing_behind(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session)
        preview = session.review(report())
        outcome = session.outcome("discarded", preview)
        worktree = session.root
    assert outcome.branch is None
    assert not worktree.exists()
    assert git(repo, "branch", "--list", BRANCH) == ""
    assert git(repo, "worktree", "list").count("\n") == 1
    assert git(remote, "branch", "--list", BRANCH) == ""


async def test_committed_only_without_push(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    with FixSession.open(repo, rename_request(), branch="fix/phone") as session:
        await agent_pass(session)
        outcome = session.deliver(
            session.review(report()), push=False, github=None, model_name="fake-fixer"
        )
    assert outcome.status == "committed"
    assert outcome.commands == [
        "git push --set-upstream origin fix/phone",
        "gh pr create --head fix/phone --fill --base main",
    ]
    assert git(repo, "branch", "--list", "fix/phone").strip()
    assert git(remote, "branch", "--list", "fix/phone") == ""


async def test_relative_remote_urls_still_push(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", "../remote.git")
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session)
        outcome = session.deliver(
            session.review(report()), push=True, github=FakeGitHub(), model_name="fake-fixer"
        )
    assert outcome.status == "opened", outcome.error
    assert git(remote, "branch", "--list", BRANCH).strip()


async def test_pushed_when_gh_is_not_available(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    github = FakeGitHub(problem="the GitHub CLI (gh) is not installed")
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session)
        outcome = session.deliver(
            session.review(report()), push=True, github=github, model_name="fake-fixer"
        )
    assert outcome.status == "pushed"
    assert outcome.error is None
    assert outcome.notes == ["the GitHub CLI (gh) is not installed"]
    assert outcome.commands == [f"gh pr create --head {BRANCH} --fill --base main"]
    assert git(remote, "branch", "--list", BRANCH).strip()


async def test_failed_steps_degrade_with_the_reason(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session)
        outcome = session.deliver(
            session.review(report()),
            push=True,
            github=FakeGitHub(error="GraphQL: base branch missing"),
            model_name="fake-fixer",
        )
    assert outcome.status == "pushed"
    assert outcome.error == "GraphQL: base branch missing"

    git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with FixSession.open(repo, rename_request(13)) as session:
        await agent_pass(session)
        outcome = session.deliver(
            session.review(report()), push=True, github=FakeGitHub(), model_name="fake-fixer"
        )
    assert outcome.status == "committed"
    assert outcome.error is not None
    assert "git push" in outcome.error


async def test_ignored_files_are_flagged_and_never_committed(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)

    def leak(root: Path) -> None:
        (root / ".env.production").write_text("KEY=1\n", encoding="utf-8")

    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session, FakeFixAgent(edit=leak))
        preview = session.review(report(), ignore=[".env*"])
        assert preview.ignored == [".env.production"]
        with pytest.raises(FixError, match="refusing to commit ignored files"):
            session.deliver(preview, push=False, github=None, model_name="fake-fixer")
    assert git(repo, "branch", "--list", BRANCH) == ""


async def test_no_edits_means_an_empty_preview(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session, FakeFixAgent(edit=lambda _: None))
        preview = session.review(report(files_changed=[]))
        assert preview.empty
        assert preview.diff == ""
        with pytest.raises(FixError, match="nothing to commit"):
            session.deliver(preview, push=False, github=None, model_name="fake-fixer")


def test_existing_branches_are_never_reused(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    git(repo, "branch", BRANCH)
    with pytest.raises(FixError, match="already exists"):
        FixSession.open(repo, rename_request())


async def test_uncommitted_changes_stay_out_of_the_fix(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / "client.py").write_text("# work in progress\n", encoding="utf-8")
    (repo / ".env").write_text("BILLING_KEY=secret\n", encoding="utf-8")
    with FixSession.open(repo, rename_request()) as session:
        assert session.repo.has_uncommitted_changes()
        assert not (session.root / ".env").exists()  # untracked files never reach the agent
        assert "customer_phone" in (session.root / "client.py").read_text(encoding="utf-8")
    assert (repo / "client.py").read_text(encoding="utf-8") == "# work in progress\n"


def test_pull_request_body_lists_what_is_left() -> None:
    preview = FixPreview(
        branch=BRANCH,
        base="main",
        files=["client.py"],
        additions=1,
        deletions=1,
        diff="",
        report=report(unresolved=["billing/export.py sums balance; check the new format"]),
    )
    body = pull_request_body(rename_request(), preview, model_name="claude-sonnet-5-5")
    assert "## Left for a human" in body
    assert "- billing/export.py sums balance" in body
    assert "| breaking | type_changed | `$.balance` | integer | string |" in body
    assert "The phone field was renamed." in body  # Claude's explanation of the drift
    assert "${BILLING_KEY}" not in body
    assert "api_key" not in body


def test_pull_request_bodies_list_a_bounded_number_of_changes() -> None:
    endpoints = [
        {
            "method": "GET",
            "url": f"https://api.billing.test/v1/r{n}/{{id}}",
            "changes": [{"path": f"$.field_{i}", "kind": "field_removed"} for i in range(150)],
        }
        for n in range(3)
    ]
    request = build_notice_request(
        ApiChangeNotice.model_validate(signed(notice(endpoints=endpoints))),
        location="x",
        verified=True,
    )
    body = pull_request_body(request, notice_preview(), model_name="claude-sonnet-5-5")
    assert body.count("| breaking | field_removed |") == MAX_CHANGES_IN_BODY
    assert body.count("And 116 more changes, not listed.") == 1  # 34, 33 and 33 listed
    assert body.count("And 117 more changes, not listed.") == 2
    assert len(body) < 65_536


def notice_preview() -> FixPreview:
    return FixPreview(
        branch="b",
        base="main",
        files=["client.py"],
        additions=1,
        deletions=1,
        diff="",
        report=report(unresolved=["The format of balance: the provider says decimal strings."]),
    )


def test_notice_branches_titles_and_commits() -> None:
    request = notice_request()
    branch = default_branch(request)
    assert branch.startswith("tiltmeter/fix-api-billing-test-2026-11-03-phone-rename-")
    assert len(branch) == len("tiltmeter/fix-api-billing-test-2026-11-03-phone-rename-") + 8
    look_alikes = [
        notice_request(id="2026-11-03_phone.rename"),
        notice_request(id="2026.11.03.Phone.Rename"),
        notice_request(id="x" * 99 + "a"),
        notice_request(id="x" * 99 + "b"),
    ]
    branches = {default_branch(r) for r in [request, *look_alikes]}
    assert len(branches) == 5  # slugs collide, branches do not
    assert all(len(b) <= len("tiltmeter/fix-") + 80 + 9 for b in branches)
    assert commit_title(request) == "Adapt to the api.billing.test API change effective 2026-12-01"
    assert commit_title(notice_request(effective_at=None)) == (
        "Adapt to the api.billing.test API change"
    )
    assert commit_body(request, report()).endswith(
        "api.billing.test announced the change in notice 2026-11-03-phone-rename."
    )


def test_notice_pull_requests_quote_the_provider_as_plain_text() -> None:
    hostile = "Thanks @octocat! See [docs](https://evil.test) ![x](https://evil.test/p.png) `x`"
    request = notice_request(summary=hostile)
    body = pull_request_body(request, notice_preview(), model_name="claude-sonnet-5-5")
    assert "`api.billing.test` announced this change in notice `2026-11-03-phone-rename`" in body
    assert "published 2026-11-03 and effective 2026-12-01" in body
    assert "verified against `https://api.billing.test/.well-known/api-change-key`" in body
    quoted = body.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert "@octocat" in quoted  # inside a code block: no mention, link or image
    assert "`" not in quoted
    assert (
        "| breaking | field_removed | `$.phone` | — | (absent), renamed to `$.phone_number` |"
        in body
    )
    assert "| breaking | type_changed | `$.balance` | `integer` | `string` |" in body
    assert "**`GET https://api.billing.test/v1/customers/{id}`**" in body
    assert "probably renamed" not in body
    assert "$.balance: Decimal string in the account currency" in body
    assert "SDK releases that already follow the change: pypi `billing-sdk` 3.0.0." in body
    assert "## Left for a human" in body
    assert "from api.billing.test's change notice `2026-11-03-phone-rename`" in body
    assert "This pull request was generated automatically by [Tiltmeter]" in body
    unsigned = pull_request_body(notice_request(verified=False), notice_preview(), model_name="m")
    # Anyone may write an unsigned notice: it is never put in the provider's mouth.
    assert "announced" not in unsigned
    assert "The provider's" not in unsigned
    assert (
        "Notice `2026-11-03-phone-rename` about `api.billing.test`'s API describes this change"
        in unsigned
    )
    assert "It is not signed by `api.billing.test`" in unsigned
    assert "from the unsigned notice `2026-11-03-phone-rename` about api.billing.test's API" in (
        unsigned
    )
    assert "is not signed by the provider" in commit_body(notice_request(verified=False), report())


async def test_notice_outcomes_name_the_notice(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    with FixSession.open(repo, notice_request()) as session:
        outcome = session.outcome("discarded", None)
    assert (outcome.notice_id, outcome.provider, outcome.check_id, outcome.endpoint_id) == (
        "2026-11-03-phone-rename",
        "api.billing.test",
        None,
        None,
    )


def test_notice_values_cannot_render_markup() -> None:
    data = notice()
    data["endpoints"][0]["changes"] = [
        {
            "path": "$.balance",
            "kind": "type_changed",
            "before": "integer @octocat",
            "after": "`string` <img src=x> [Approve](https://evil.test)",
        }
    ]
    body = pull_request_body(
        notice_request(endpoints=data["endpoints"]), notice_preview(), model_name="m"
    )
    row = next(line for line in body.splitlines() if line.startswith("| breaking"))
    assert row == (
        "| breaking | type_changed | `$.balance` | `integer @octocat` "
        "| `'string' <img src=x> [Approve](https://evil.test)` |"
    )


def test_spec_pull_requests_name_the_spec_and_hedge_renames() -> None:
    request = spec_request()
    assert commit_title(request) == "Adapt to the api.billing.test API change in spec 1.1"
    assert "comparing two versions of api.billing.test's OpenAPI spec" in commit_body(
        request, report()
    )
    body = pull_request_body(request, notice_preview(), model_name="m")
    assert (
        "Tiltmeter compared two versions of the OpenAPI spec `api.billing.test` publishes" in body
    )
    assert "`1.0 to 1.1`, fetched 2026-10-01 and 2026-11-03" in body
    assert "probably renamed to `$.phone_number`" in body
    assert "2 other changed endpoints are left out" in body
    assert "from a diff of api.billing.test's OpenAPI spec" in body
    assert "announced this change in notice" not in body


def test_the_left_for_a_human_section_is_always_there() -> None:
    preview = notice_preview().model_copy(update={"report": report(unresolved=[])})
    body = pull_request_body(rename_request(), preview, model_name="m")
    assert "## Left for a human\n\nNothing: the agent flagged nothing it could not decide." in body


def test_pull_requests_show_moves_and_retired_values() -> None:
    from tests.fix.test_request import MOVES_AND_RETIREMENTS

    request = notice_request(endpoints=MOVES_AND_RETIREMENTS)
    body = pull_request_body(request, notice_preview(), model_name="m")
    assert (
        "| breaking | endpoint_removed | `$` | — | (gone), moved to "
        "`GET https://api.billing.test/v2/customers/{id}` |" in body
    )
    assert (
        "| breaking | value_retired | `$.model` | `billing-3-5-20240620` "
        "| (no longer accepted), send `billing-4` instead |" in body
    )
