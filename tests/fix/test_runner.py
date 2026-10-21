from __future__ import annotations

from pathlib import Path

import pytest

from tests.fix.kit import PR_URL, FakeFixAgent, FakeGitHub, git, make_repo, rename_request, report
from tiltmeter.errors import FixError
from tiltmeter.fix.runner import FixSession, default_branch, pull_request_body

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
    from tiltmeter.fix.models import FixPreview

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
