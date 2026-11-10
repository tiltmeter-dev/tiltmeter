from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.fix.kit import (
    PR_BRANCH,
    PR_URL,
    FakeFixAgent,
    FakeGitHub,
    commit_files,
    git,
    make_repo,
    open_pull_request,
    rename_request,
    report,
    review,
)
from tests.fix.test_request import notice_request, spec_request
from tests.fix.test_usage import OLD, SOL, WEATHER_TS
from tests.helpers import explanation
from tests.notices.kit import notice, signed
from tiltmeter.errors import FixError
from tiltmeter.fix.models import FixPreview
from tiltmeter.fix.request import build_notice_request
from tiltmeter.fix.runner import (
    HELD_BACK,
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
    assert "Possibly unused" not in body


def move_to_one_call_3(root: Path) -> None:
    path = root / "src/lib/weather.ts"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("data/2.5/onecall", "data/3.0/onecall"), encoding="utf-8")


async def test_edits_to_old_unused_code_stay_in_a_local_commit(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path, SOL, date=OLD)
    github = FakeGitHub()
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session, FakeFixAgent(edit=move_to_one_call_3))
        preview = session.review(report())
        assert [item.path for item in preview.unused] == ["src/lib/weather.ts"]
        assert preview.likely_dead
        outcome = session.deliver(preview, push=True, github=github, model_name="fake-fixer")
    assert outcome.status == "committed"
    assert outcome.held_back
    assert outcome.notes == [HELD_BACK]
    assert "--allow-unused" in HELD_BACK
    assert outcome.commands == [
        f"git push --set-upstream origin {BRANCH}",
        f"gh pr create --head {BRANCH} --fill --base main",
    ]
    assert github.created == []
    assert git(remote, "branch", "--list", BRANCH) == ""
    assert git(repo, "branch", "--list", BRANCH).strip()  # the commit is kept


async def test_allowed_unused_code_gets_its_pull_request_with_a_note(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, SOL, date=OLD)
    github = FakeGitHub()
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session, FakeFixAgent(edit=move_to_one_call_3))
        outcome = session.deliver(
            session.review(report()),
            push=True,
            github=github,
            model_name="fake-fixer",
            allow_unused=True,
        )
    assert outcome.status == "opened"
    assert not outcome.held_back
    [pr] = github.created
    assert (
        "Possibly unused: nothing else in the repository imports or names "
        "`src/lib/weather.ts` (last changed 2024-01-15)." in pr["body"]
    )


async def test_recent_or_partly_unused_edits_still_open_a_pull_request(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, SOL, date=OLD)
    commit_files(repo, {"src/lib/weather.ts": WEATHER_TS + "// touched\n"})
    github = FakeGitHub()
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session, FakeFixAgent(edit=move_to_one_call_3))
        preview = session.review(report())
        assert [item.path for item in preview.unused] == ["src/lib/weather.ts"]
        assert not preview.likely_dead
        outcome = session.deliver(preview, push=True, github=github, model_name="fake-fixer")
    assert outcome.status == "opened"
    assert "Possibly unused" in github.created[0]["body"]


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


def rendered(body: str) -> str:
    """The part of a pull request body that GitHub renders: no code blocks or code spans."""
    kept, fenced = [], False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            kept.append(re.sub(r"`[^`]*`", "", line))
    assert not fenced
    return "\n".join(kept)


def test_the_agent_s_words_cannot_render_markup() -> None:
    hostile = (
        "Renamed the field. Closes #1, thanks @octocat! ![x](https://evil.test/p?d=SECRET) "
        "<img src=https://evil.test/q>\n\n## Tests\n\n`pytest` passed on these edits.\n```\n# Hi"
    )
    preview = FixPreview(
        branch=BRANCH,
        base="main",
        files=["client.py", "a`![f](https://evil.test/f)`.py"],
        additions=1,
        deletions=1,
        diff="",
        report=report(
            summary=hostile,
            unresolved=["Ask @hubot.\n\n## Approved by the owners", "[Approve](https://evil.test)"],
        ),
    )
    request = rename_request().model_copy(
        update={"explanation": explanation(summary="Renamed, see ![e](https://evil.test/e)")}
    )
    body = pull_request_body(request, preview, model_name="m")
    shown = rendered(body)
    for live in ("evil.test", "@octocat", "@hubot", "Closes #1", "<img", "Approve", "pytest"):
        assert live in body
        assert live not in shown
    assert [line for line in shown.splitlines() if line.startswith("#")] == [
        "## What changed in the API",
        "## What this pull request changes",
        "## Left for a human",
    ]
    assert "- Ask @hubot. ## Approved by the owners\n- [Approve](https://evil.test)\n```" in body
    assert "- `a'![f](https://evil.test/f)'.py`" in body
    silent = preview.model_copy(update={"report": report(summary=" \n")})
    assert pull_request_body(rename_request(), silent, model_name="m").count("(nothing)") == 1


def test_commit_messages_keep_only_a_summary_that_is_plain_prose() -> None:
    request = rename_request()
    found = "Tiltmeter check #12 found the change in GET https://api.billing.test/v1/customers."
    assert commit_body(request, report()) == f"Read phone_number instead of phone.\n\n{found}"
    spaced = report(summary="Read  phone_number\n\nCo-authored-by: someone")
    assert commit_body(request, spaced) == f"Read phone_number Co-authored-by: someone\n\n{found}"
    for live in [
        "Renamed the field. Fixes #12.",
        "Renamed the field, closes GH-3",
        "Thanks @octocat for the hint",
        "See https://evil.test/issues/1",
        "See www.evil.test",
        "Renamed ![x](x.png)",
        "Renamed <img src=x>",
        " ",
    ]:
        assert commit_body(request, report(summary=live)) == found


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


def test_removed_endpoints_come_first_and_shared_notes_show_once() -> None:
    from tests.notices.kit import notice as make_notice

    note = "Pages are fetched with nextPageToken."
    endpoints = [
        {
            "method": method,
            "url": "https://api.billing.test/v1/search",
            "changes": [
                {"path": "$.startAt", "kind": "field_removed", "note": note},
                {
                    "path": "$",
                    "kind": "endpoint_removed",
                    "moved_to": {"method": method, "url": "https://api.billing.test/v1/search/jql"},
                    "note": f"{method} takes the same parameters.",
                },
            ],
        }
        for method in ("GET", "POST")
    ]
    model = ApiChangeNotice.model_validate(signed(make_notice(endpoints=endpoints)))
    request = build_notice_request(model, location="x", verified=True, read=["startAt"])
    assert [t.kind.value for t in request.apis[0].targets] == ["endpoint_removed", "field_removed"]
    body = pull_request_body(request, notice_preview(), model_name="m")
    assert body.count("$.startAt: Pages are fetched with nextPageToken.") == 1
    assert "GET /v1/search: GET takes the same parameters." in body
    assert "POST /v1/search: POST takes the same parameters." in body


def test_retired_values_name_what_they_replace() -> None:
    from tests.fix.test_request import MOVES_AND_RETIREMENTS

    retired = [MOVES_AND_RETIREMENTS[1]]
    assert commit_title(notice_request(endpoints=retired)) == (
        "Replace the models api.billing.test retired"
    )
    # Anything else in the notice keeps the general title.
    assert commit_title(notice_request(endpoints=MOVES_AND_RETIREMENTS)).startswith(
        "Adapt to the api.billing.test API change"
    )


def test_notes_on_retired_values_name_the_value() -> None:
    from tests.notices.kit import notice as make_notice

    retired = {
        "method": "POST",
        "url": "https://api.billing.test/v1/completions",
        "changes": [
            {
                "path": "$.model",
                "kind": "value_retired",
                "before": "b-1",
                "replaced_by": "b-4",
                "note": "Retired on May 1.",
            }
        ],
    }
    model = ApiChangeNotice.model_validate(signed(make_notice(endpoints=[retired])))
    request = build_notice_request(model, location="x", verified=True)
    assert "b-1: Retired on May 1." in pull_request_body(request, notice_preview(), model_name="m")


def test_titles_follow_the_repository_convention_when_the_agent_gives_one() -> None:
    request = notice_request()
    default = "Adapt to the api.billing.test API change effective 2026-12-01"
    assert commit_title(request, report(commit_title="fix(billing):  read  phone_number")) == (
        "fix(billing): read phone_number"
    )
    for unusable in [
        None,
        "fix",
        "x" * 101,
        "fix: thanks @octocat",
        "fix: see https://evil.test",
        "fix: [docs](x)",
        "fix: `code`",
        "fix: <b>bold</b>",
        "fix: read phone_number (#12)",
        "fix: read phone_number, closes GH-12",
        "fix: see www.evil.test",
    ]:
        assert commit_title(request, report(commit_title=unusable)) == default


async def test_the_commit_and_the_pull_request_use_the_agent_s_title(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    github = FakeGitHub()
    with FixSession.open(repo, rename_request()) as session:
        await agent_pass(session)
        preview = session.review(report(commit_title="fix(billing): read phone_number"))
        session.deliver(preview, push=True, github=github, model_name="fake-fixer")
    [pr] = github.created
    assert pr["title"] == "fix(billing): read phone_number"
    assert git(repo, "log", "-1", "--format=%s", BRANCH).strip() == pr["title"]


def test_the_body_says_first_who_a_scoped_change_affects() -> None:
    scope = "Accounts on API version 2026-11 or later."
    body = pull_request_body(notice_request(applies_to=scope), notice_preview(), model_name="m")
    section = body.split("## What changed in the API\n\n", 1)[1]
    assert section.startswith(
        "**This change affects only some callers, and Tiltmeter can't tell which ones this "
        "code serves.** According to the provider's notice, it affects:"
    )
    assert f"```text\n{scope}\n```\n\nCallers outside that keep the old behaviour." in body
    plain = pull_request_body(notice_request(), notice_preview(), model_name="m")
    assert "only some callers" not in plain


def test_a_partly_scoped_notice_names_what_each_scope_covers() -> None:
    retired = {
        "method": "POST",
        "url": "https://api.billing.test/v1/completions",
        "changes": [
            {
                "path": "$.model",
                "kind": "value_retired",
                "before": "b-1",
                "replaced_by": "b-4",
                "applies_to": "Free keys. Enterprise contracts keep b-1.",
            },
            {"path": "$.model", "kind": "value_retired", "before": "b-2", "replaced_by": "b-4"},
        ],
    }
    model = ApiChangeNotice.model_validate(signed(notice(endpoints=[retired])))
    request = build_notice_request(model, location="x", verified=True)
    body = pull_request_body(request, notice_preview(), model_name="m")
    assert "**Some of these changes affect only some callers" in body
    assert "b-1: Free keys. Enterprise contracts keep b-1." in body
    assert "keep the old behaviour; the other changes affect every caller." in body


# -- answering a review ----------------------------------------------------------------


def keep_the_old_name(root: Path) -> None:
    client = root / "client.py"
    client.write_text(
        client.read_text(encoding="utf-8").replace(
            'customer["phone_number"]', 'customer.get("phone_number") or customer["phone"]'
        ),
        encoding="utf-8",
    )


def answering(tmp_path: Path) -> tuple[Path, Path, FakeFixAgent]:
    repo, remote = make_repo(tmp_path)
    open_pull_request(repo, remote)
    return repo, remote, FakeFixAgent(edit=keep_the_old_name)


async def test_a_review_is_answered_on_the_pull_request_s_own_branch(tmp_path: Path) -> None:
    repo, remote, agent = answering(tmp_path)
    feedback, github = review(), FakeGitHub()
    request = rename_request().model_copy(update={"feedback": feedback})
    answer = report(summary="Kept phone as a fallback, as asked.", unresolved=["Sign the CLA."])
    with FixSession.follow(repo, request, feedback) as session:
        # The agent starts from the pull request as it stands, not from main.
        assert 'customer["phone_number"]' in (session.root / "client.py").read_text()
        assert session.base == "main"
        await agent_pass(session, agent)
        preview = session.review(answer)
        assert preview.files == ["client.py"]
        outcome = session.deliver(preview, push=True, github=github, model_name="fake-fixer")
    assert (outcome.status, outcome.pr_url, outcome.branch) == ("updated", PR_URL, None)
    assert github.created == []  # no second pull request
    titles = git(remote, "log", "-2", "--format=%s", PR_BRANCH).splitlines()
    assert titles == ["Address the review", "Adapt to the billing_customers API change"]
    message = git(remote, "log", "-1", "--format=%b", PR_BRANCH)
    assert message.strip() == (
        "Kept phone as a fallback, as asked.\n\n"
        "Follows the review of the pull request for this change."
    )
    assert not git(repo, "branch", "--list", "tiltmeter/fix-follow-up-7").strip()
    assert git(repo, "status", "--porcelain").strip() == ""
    [comment] = github.comments
    short = git(remote, "rev-parse", "--short", PR_BRANCH).strip()
    assert comment["pull"] == PR_URL
    assert comment["body"].startswith(f"Pushed `{short}` in answer to the review.")
    assert "```text\nKept phone as a fallback, as asked.\n```" in comment["body"]
    assert "Not done, left for a person:\n\n```text\n- Sign the CLA.\n```" in comment["body"]
    assert "written automatically by [Tiltmeter]" in comment["body"]


async def test_a_branch_that_moved_keeps_the_answer_for_a_push_by_hand(tmp_path: Path) -> None:
    repo, remote, agent = answering(tmp_path)
    feedback, github = review(), FakeGitHub()
    request = rename_request().model_copy(update={"feedback": feedback})
    with FixSession.follow(repo, request, feedback) as session:
        await agent_pass(session, agent)
        preview = session.review(report())
        # The maintainer pushes to the branch while the agent works.
        theirs = session.repo.add_worktree(tmp_path / "theirs", "theirs", "FETCH_HEAD")
        (theirs.root / "models.py").write_text("THEIRS = True\n", encoding="utf-8")
        theirs.stage_all()
        theirs.commit("A commit of the maintainer's", "Body.")
        session.repo.push_to("origin", "theirs", PR_BRANCH)
        outcome = session.deliver(preview, push=True, github=github, model_name="fake-fixer")
    assert outcome.status == "committed"
    assert outcome.error is not None
    assert outcome.branch == "tiltmeter/fix-follow-up-7"
    assert outcome.commands == [f"git push origin tiltmeter/fix-follow-up-7:{PR_BRANCH}"]
    assert github.comments == []  # nothing was pushed, so there is nothing to announce
    assert git(repo, "branch", "--list", "tiltmeter/fix-follow-up-7").strip()
    assert git(remote, "log", "-1", "--format=%s", PR_BRANCH).strip() == (
        "A commit of the maintainer's"
    )


async def test_an_answer_can_stay_local_or_go_without_a_comment(tmp_path: Path) -> None:
    repo, remote, agent = answering(tmp_path)
    feedback = review()
    request = rename_request().model_copy(update={"feedback": feedback})
    with FixSession.follow(repo, request, feedback) as session:
        await agent_pass(session, agent)
        local = session.deliver(
            session.review(report()), push=False, github=None, model_name="fake-fixer"
        )
    assert (local.status, local.branch) == ("committed", "tiltmeter/fix-follow-up-7")
    assert git(remote, "log", "-1", "--format=%s", PR_BRANCH).strip() != "Address the review"
    with pytest.raises(FixError, match="already exists from an earlier run"):
        FixSession.follow(repo, request, feedback)
    git(repo, "branch", "--quiet", "-D", "tiltmeter/fix-follow-up-7")

    failing = FakeGitHub(error="HTTP 403")
    with FixSession.follow(repo, request, feedback) as session:
        await agent_pass(session, agent)
        outcome = session.deliver(
            session.review(report()), push=True, github=failing, model_name="fake-fixer"
        )
    assert outcome.status == "updated"  # the push is what counts
    assert outcome.notes == ["pushed, but could not comment on the pull request: HTTP 403"]


def test_a_review_needs_the_remote_the_branch_is_in(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    request = rename_request()
    with pytest.raises(FixError, match="no remote of this checkout is acme/app"):
        FixSession.follow(repo, request, review())
    open_pull_request(repo, remote)
    with pytest.raises(FixError, match="`git fetch --quiet` failed"):
        FixSession.follow(repo, request, review(branch="no-such-branch"))
    assert not list(Path(tmp_path).glob("tiltmeter-fix-*"))


def test_an_agent_s_answer_is_titled_like_its_commits() -> None:
    request = rename_request().model_copy(update={"feedback": review()})
    assert commit_title(request, report()) == "Address the review"
    titled = report(commit_title="fix(billing): keep phone as a fallback")
    assert commit_title(request, titled) == "fix(billing): keep phone as a fallback"
    assert commit_body(request, report(summary="See https://evil.test")) == (
        "Follows the review of the pull request for this change."
    )
