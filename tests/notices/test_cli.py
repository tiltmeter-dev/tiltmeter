from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.fix.kit import PR_URL, FakeFixAgent, FakeGitHub, git, make_repo
from tests.notices.kit import (
    FEED_URL,
    KEY,
    KEY_FILE,
    KEY_URL,
    NOTICE_ID,
    NOTICE_URL,
    PROVIDER,
    SPEC_URL,
    billing_spec,
    feed,
    notice,
    serve,
    signed,
)
from tests.test_cli import URL, flat, invoke, runner, write_config
from tiltmeter.cli import app
from tiltmeter.core.store import Store
from tiltmeter.fix.agent import DeepFixAgent
from tiltmeter.fix.prompts import render_fix_request
from tiltmeter.fix.request import build_notice_request
from tiltmeter.fix.runner import default_branch
from tiltmeter.notices.models import ApiChangeNotice, notice_json_schema
from tiltmeter.notices.signing import private_key_pem, write_private_key

SPECS = Path(__file__).parent.parent / "fixtures" / "specs"
BRANCH = default_branch(
    build_notice_request(ApiChangeNotice.model_validate(signed()), location="", verified=True)
)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeFixAgent, FakeGitHub]:
    agent, github = FakeFixAgent(), FakeGitHub()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(DeepFixAgent, "from_config", classmethod(lambda cls, _: agent))
    monkeypatch.setattr("tiltmeter.fix.git.GhCli", lambda: github)
    return agent, github


def subscribed(tmp_path: Path) -> Path:
    path = tmp_path / "tiltmeter.yaml"
    path.write_text(f"subscriptions:\n  - provider: {PROVIDER}\n", encoding="utf-8")
    return path


def records(config: Path) -> list[Any]:
    with Store(config.parent / ".tiltmeter" / "tiltmeter.db") as store:
        return store.notice_records()


# -- provider side ----------------------------------------------------------------


def test_schema_command_prints_the_published_schema() -> None:
    outcome = invoke("notice", "schema")
    assert outcome.exit_code == 0
    assert json.loads(outcome.stdout) == notice_json_schema()
    assert "api-change-feed" in invoke("notice", "schema", "--feed").stdout


def test_keygen_create_sign_publish_and_verify(tmp_path: Path) -> None:
    site = tmp_path / "site" / ".well-known"
    key = tmp_path / "notice-key.pem"
    made = invoke("notice", "keygen", str(key), "--public", str(site / "api-change-key"))
    assert made.exit_code == 0, made.output
    assert "v=ACN1; k=ed25519; p=" in (site / "api-change-key").read_text(encoding="utf-8")
    assert invoke("notice", "keygen", str(key)).exit_code == 4  # never overwritten silently

    draft = tmp_path / "notice.json"
    created = invoke(
        "notice",
        "create",
        "--from-openapi",
        str(SPECS / "rename_and_retype" / "old.yaml"),
        str(SPECS / "rename_and_retype" / "new.yaml"),
        "--id",
        "partner-phone-rename",
        "--effective",
        "2026-12-01",
        "--sdk",
        "pypi:partner-sdk@3.0.0",
        "-o",
        str(draft),
    )
    assert created.exit_code == 0, created.output
    assert "drafted notice partner-phone-rename with 9 changes" in flat(created.output)
    raw = json.loads(draft.read_text(encoding="utf-8"))
    legacy = [e for e in raw["endpoints"] if e["url"].endswith("/v1/legacy")]
    assert legacy[0]["changes"] == [{"path": "$", "kind": "endpoint_removed"}]
    assert raw["effective_at"] == "2026-12-01T00:00:00Z"
    assert "signature" not in raw

    unsigned = invoke("notice", "publish", str(draft), "--feed", str(site / "api-changes.json"))
    assert unsigned.exit_code == 4
    assert "not signed" in flat(unsigned.output)

    assert invoke("notice", "sign", str(draft), "--key", str(key)).exit_code == 0
    signed_raw = json.loads(draft.read_text(encoding="utf-8"))
    assert signed_raw["signature"]["key"] == (
        "https://api.partner.example/.well-known/api-change-key"
    )

    published = invoke("notice", "publish", str(draft), "--feed", str(site / "api-changes.json"))
    assert published.exit_code == 0, published.output
    assert "signatures checked against" in flat(published.output)
    feed = json.loads((site / "api-changes.json").read_text(encoding="utf-8"))
    assert [n["id"] for n in feed["notices"]] == ["partner-phone-rename"]

    verified = invoke("notice", "verify", str(draft), "--key-file", str(site / "api-change-key"))
    assert verified.exit_code == 0, verified.output
    assert "signature verified against" in flat(verified.output)


def test_publish_refuses_a_signature_the_served_key_does_not_match(tmp_path: Path) -> None:
    site = tmp_path / ".well-known"
    site.mkdir()
    (site / "api-change-key").write_text(KEY_FILE, encoding="utf-8")
    other = tmp_path / "other.pem"
    assert (
        invoke("notice", "keygen", str(other), "--public", str(tmp_path / "unused")).exit_code == 0
    )
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    assert invoke("notice", "sign", str(path), "--key", str(other)).exit_code == 0
    refused = invoke("notice", "publish", str(path), "--feed", str(site / "api-changes.json"))
    assert refused.exit_code == 4
    assert "does not match" in flat(refused.output)
    assert not (site / "api-changes.json").exists()


def test_create_from_a_check(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    with respx.mock:
        route = respx.get(URL).respond(200, json={"id": 1, "phone": "555"})
        invoke("check", "-c", str(config))
        route.respond(200, json={"id": 1, "phone_number": "555"})
        invoke("check", "-c", str(config), "--no-llm")
    outcome = invoke(
        "notice",
        "create",
        "--from-check",
        "2",
        "-c",
        str(config),
        "--url",
        "https://api.test/v1/items",
        "--summary",
        "Items return phone_number instead of phone.",
    )
    assert outcome.exit_code == 0, outcome.output
    raw = json.loads(outcome.stdout)
    assert raw["provider"] == "api.test"
    assert raw["summary"] == "Items return phone_number instead of phone."
    assert raw["endpoints"][0]["changes"] == [
        {
            "path": "$.phone",
            "kind": "field_removed",
            "before": "string",
            "renamed_to": "$.phone_number",
        }
    ]
    assert invoke("notice", "create", "--from-check", "9", "-c", str(config)).exit_code == 4


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--from-check", "1", "--from-openapi", "a", "b"],
        ["--from-openapi", "a", "b", "--url", "https://x.test"],
        ["--from-check", "1", "--base-url", "https://x.test"],
        ["--from-openapi", "a", "b", "--sdk", "nope"],
        ["--from-openapi", "missing.yaml", "missing.yaml"],
    ],
)
def test_create_usage_errors(args: list[str]) -> None:
    assert invoke("notice", "create", *args).exit_code == 4


@respx.mock
def test_verify_fetches_the_provider_key(tmp_path: Path) -> None:
    respx.get(NOTICE_URL).respond(200, json=signed())
    respx.get(KEY_URL).respond(200, text=KEY_FILE)
    ok = invoke("notice", "verify", NOTICE_URL, "--json")
    assert ok.exit_code == 0
    assert json.loads(ok.stdout)["verified"] is True
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    assert invoke("notice", "verify", str(path)).exit_code == 1
    path.write_text(json.dumps({**signed(), "summary": "Edited."}), encoding="utf-8")
    bad = invoke("notice", "verify", str(path), "--json")
    assert bad.exit_code == 3
    assert "signature does not match" in json.loads(bad.stdout)["error"]


# -- subscriber side --------------------------------------------------------------


@respx.mock
def test_notices_check_lists_new_notices(tmp_path: Path) -> None:
    config = subscribed(tmp_path)
    serve(signed(), notice(id="unsigned"))
    listed = invoke("notices", "check", "-c", str(config))
    assert listed.exit_code == 1, listed.output
    text = flat(listed.output)
    assert f"{PROVIDER} {FEED_URL}" in text
    assert f"● {PROVIDER} {NOTICE_ID} signed · published 2026-11-03 · effective 2026-12-01" in text
    assert "✗ unsigned rejected: not signed" in text
    assert "1 new · 1 rejected" in text
    assert records(config) == []  # checking records nothing

    repo, _ = make_repo(tmp_path)
    affected = invoke("notices", "check", "-c", str(config), "--repo", str(repo), "--json")
    assert affected.exit_code == 1
    [report] = json.loads(affected.stdout)
    assert report["entries"][0]["impact"]["files"] == ["client.py", "models.py"]

    (tmp_path / "other").mkdir()
    other, _ = make_repo(tmp_path / "other", {"app.py": "print('hello')\n"})
    clean = invoke("notices", "check", "-c", str(config), "--repo", str(other))
    assert clean.exit_code == 0
    assert "not affected: no tracked file mentions" in flat(clean.output)


@respx.mock
def test_notices_check_failures(tmp_path: Path) -> None:
    respx.get(FEED_URL).respond(404)
    failed = invoke("notices", "check", "-c", str(subscribed(tmp_path)))
    assert failed.exit_code == 3
    assert f"✗ {FEED_URL} answered HTTP 404" in flat(failed.output)
    no_subscriptions = invoke("notices", "check", "-c", str(write_config(tmp_path)))
    assert no_subscriptions.exit_code == 4
    assert "no subscriptions" in flat(no_subscriptions.output)


@respx.mock
def test_notices_apply_opens_one_pull_request_and_remembers_it(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    config = subscribed(tmp_path)
    repo, remote = make_repo(tmp_path)
    serve(signed())
    first = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert first.exit_code == 0, first.output
    text = flat(first.output)
    assert f"Working on a new branch, {BRANCH}, from main at" in text
    assert f"✓ opened {PR_URL}" in text
    [request] = agent.requests
    assert request.notice is not None
    assert request.notice.key == KEY_URL
    assert request.candidate_files == ["client.py", "models.py"]
    [pr] = github.created
    assert pr["title"] == "Adapt to the api.billing.test API change effective 2026-12-01"
    assert "announced this change in notice `2026-11-03-phone-rename`" in pr["body"]
    assert git(remote, "branch", "--list", BRANCH).strip()
    [record] = records(config)
    assert (record.status, record.detail) == ("opened", PR_URL)

    again = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert again.exit_code == 0
    assert "No new notices." in again.output
    assert len(agent.requests) == 1


@respx.mock
def test_notices_apply_skips_the_agent_when_nothing_is_affected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_agent(*_: Any, **__: Any) -> Any:
        raise AssertionError("no agent should run for an unaffected repository")

    monkeypatch.setattr("tiltmeter.fix.agent.build_fix_agent", no_agent)
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path, {"app.py": "print('hello')\n"})
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--json")
    assert outcome.exit_code == 0, outcome.output
    [applied] = json.loads(outcome.stdout)["applied"]
    assert applied["status"] == "unaffected"
    assert applied["impact"]["affected"] is False
    assert [r.status for r in records(config)] == ["unaffected"]


@respx.mock
def test_notices_apply_records_failures_and_retries_them(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, _ = fakes
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    serve(signed())
    agent.error = RuntimeError("overloaded")
    failed = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert failed.exit_code == 3
    assert "failed: fix failed: overloaded" in flat(failed.output)
    assert git(repo, "branch", "--list", BRANCH) == ""
    assert [(r.status, r.attempts) for r in records(config)] == [("failed", 1)]

    agent.error = None
    retried = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert retried.exit_code == 0, retried.output
    assert [(r.status, r.attempts) for r in records(config)] == [("opened", 2)]


@respx.mock
def test_notices_apply_dry_run_records_nothing(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--dry-run")
    assert outcome.exit_code == 0, outcome.output
    assert "discarded" in flat(outcome.output)
    assert records(config) == []


@respx.mock
def test_a_branch_already_pushed_only_gets_its_pull_request(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    git(repo, "push", "--quiet", "origin", f"main:refs/heads/{BRANCH}")
    serve(signed())
    dry = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--dry-run")
    assert f"branch {BRANCH} is already on origin" in flat(dry.output)
    assert github.created == []

    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert outcome.exit_code == 0, outcome.output
    assert f"opened: {PR_URL}" in flat(outcome.output)
    assert agent.requests == []  # no agent: the branch holds the fix already
    [pr] = github.created
    assert pr["title"] == "Initial commit"  # from the branch's commit, fetched from the remote
    assert "Opened by Tiltmeter for a branch an earlier run pushed." in pr["body"]
    assert [(r.status, r.detail) for r in records(config)] == [("opened", PR_URL)]


@respx.mock
def test_a_pull_request_that_exists_is_found_not_opened_again(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    _, github = fakes
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    git(repo, "push", "--quiet", "origin", f"main:refs/heads/{BRANCH}")
    github.created.append({"head": BRANCH, "base": "main", "title": "earlier", "body": ""})
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert outcome.exit_code == 0, outcome.output
    assert len(github.created) == 1
    assert [r.status for r in records(config)] == ["opened"]


@respx.mock
def test_a_pull_request_that_cannot_be_opened_is_retried(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = FakeGitHub(problem="the GitHub CLI is not logged in (run `gh auth login`)")
    monkeypatch.setattr("tiltmeter.fix.git.GhCli", lambda: broken)
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    git(repo, "push", "--quiet", "origin", f"main:refs/heads/{BRANCH}")
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert outcome.exit_code == 3
    [record] = records(config)
    assert record.status == "failed"
    assert "its pull request could not be opened" in (record.detail or "")
    assert not record.done


def test_fix_from_a_notice_file(tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]) -> None:
    agent, github = fakes
    repo, _ = make_repo(tmp_path)
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    missing = tmp_path / "none.yaml"

    refused = runner.invoke(
        app, ["fix", "--notice", str(path), "--repo", str(repo), "-c", str(missing)]
    )
    assert refused.exit_code == 4
    assert "--allow-unsigned" in flat(refused.output)

    outcome = runner.invoke(
        app,
        [
            "fix",
            "--notice",
            str(path),
            "--allow-unsigned",
            "--repo",
            str(repo),
            "-c",
            str(missing),
            "--yes",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    text = flat(outcome.output)
    assert "the default fix settings apply and the outcome is not recorded" in text
    assert BRANCH.startswith("tiltmeter/fix-api-billing-test-")
    assert f"● {PROVIDER} {NOTICE_ID} unsigned" in text
    assert f"✓ opened {PR_URL}" in text
    assert agent.requests[0].notice is not None
    assert "It is not signed by `api.billing.test`" in github.created[0]["body"]


@respx.mock
def test_fix_from_a_notice_url_records_the_outcome(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    respx.get(NOTICE_URL).respond(200, json=signed())
    respx.get(KEY_URL).respond(200, text=KEY_FILE)
    outcome = runner.invoke(
        app,
        ["fix", "--notice", NOTICE_URL, "--repo", str(repo), "-c", str(config), "--json", "--yes"],
    )
    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert (payload["status"], payload["notice_id"], payload["provider"]) == (
        "opened",
        NOTICE_ID,
        PROVIDER,
    )
    assert [r.status for r in records(config)] == ["opened"]


def test_fix_takes_a_check_or_a_notice(tmp_path: Path) -> None:
    assert invoke("fix").exit_code == 4
    assert invoke("fix", "2", "--notice", "n.json").exit_code == 4
    assert invoke("fix", "2", "--allow-unsigned").exit_code == 4


def test_the_signing_key_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key.pem"
    write_private_key(key, KEY)
    monkeypatch.setenv("TILTMETER_SIGNING_KEY", str(key))
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    assert invoke("notice", "sign", str(path)).exit_code == 0
    assert json.loads(path.read_text(encoding="utf-8")) == signed()


def test_an_unsigned_file_never_stands_in_for_the_provider_notice(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, _ = fakes
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    agent.edit = lambda root: None
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    outcome = runner.invoke(
        app,
        ["fix", "--notice", str(path), "--allow-unsigned", "--repo", str(repo), "-c", str(config)],
    )
    assert outcome.exit_code == 0, outcome.output
    assert records(config) == []


@respx.mock
def test_a_commit_that_was_not_pushed_is_tried_again(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert outcome.exit_code == 3, outcome.output
    [record] = records(config)
    assert record.status == "failed"
    assert record.detail is not None
    assert record.detail.startswith(f"committed to {BRANCH} but not pushed:")
    assert not record.done


@respx.mock
def test_a_fix_of_old_unused_code_is_held_back_and_not_tried_again(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes

    def edit_client(root: Path) -> None:  # which nothing imports
        path = root / "client.py"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('["phone"]', '["phone_number"]'), encoding="utf-8")

    agent.edit = edit_client
    config = subscribed(tmp_path)
    repo, remote = make_repo(tmp_path, date="2024-01-15T12:00:00+00:00")
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert outcome.exit_code == 1, outcome.output
    assert "the pull request was held back" in flat(outcome.output)
    assert github.created == []
    assert git(remote, "branch", "--list", BRANCH) == ""
    [record] = records(config)
    assert record.status == "committed"
    assert record.detail == f"committed to {BRANCH}, not pushed: the changed code looks unused"
    assert record.done
    again = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert "No new notices." in again.output
    assert len(agent.requests) == 1


# -- openapi subscriptions --------------------------------------------------------


def follows_spec(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "tiltmeter.yaml"
    lines = [f"subscriptions:\n  - openapi: {SPEC_URL}\n"]
    lines += [f"    {key}: {json.dumps(value)}\n" for key, value in extra.items()]
    path.write_text("".join(lines), encoding="utf-8")
    return path


def baseline_of(config: Path) -> Any:
    with Store(config.parent / ".tiltmeter" / "tiltmeter.db") as store:
        return store.spec_baseline(SPEC_URL)


@respx.mock
def test_a_spec_subscription_records_a_baseline_then_fixes_what_changes(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    config = follows_spec(tmp_path)
    repo, _ = make_repo(tmp_path)
    route = respx.get(SPEC_URL).respond(200, json=billing_spec("1.0"))

    checked = invoke("notices", "check", "-c", str(config))
    assert checked.exit_code == 0, checked.output
    assert "no baseline yet" in flat(checked.output)
    assert baseline_of(config) is None  # check writes nothing

    first = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert first.exit_code == 0, first.output
    assert baseline_of(config).version == "1.0"

    route.respond(200, json=billing_spec("1.1", renamed=True))
    preview = invoke("notices", "check", "-c", str(config), "--repo", str(repo))
    assert preview.exit_code == 1, preview.output
    assert "spec diff" in flat(preview.output)
    assert baseline_of(config).version == "1.0"

    applied = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert applied.exit_code == 0, applied.output
    [request] = agent.requests
    assert request.notice is not None
    assert request.notice.kind == "spec"
    assert [api.url for api in request.apis] == [f"https://{PROVIDER}/v1/customers/{{id}}"]
    [pr] = github.created
    assert pr["title"] == f"Adapt to the {PROVIDER} API change in spec 1.1"
    assert "Tiltmeter compared two versions of the OpenAPI spec" in pr["body"]
    assert "probably renamed to `$.phone_number`" in pr["body"]
    assert "1 other changed endpoints are left out" in pr["body"]  # /v1/invoices is unused
    assert baseline_of(config).version == "1.1"
    [record] = records(config)
    assert record.status == "opened"
    assert record.notice_id.startswith("spec-")

    again = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert "No new notices." in again.output
    assert len(agent.requests) == 1


@respx.mock
def test_a_baseline_in_the_config_is_where_the_first_run_starts(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    old_url = SPEC_URL.replace(".json", "-1.0.json")
    config = follows_spec(tmp_path, baseline=old_url)
    repo, _ = make_repo(tmp_path)
    respx.get(old_url).respond(200, json=billing_spec("1.0"))
    respx.get(SPEC_URL).respond(200, json=billing_spec("1.1", renamed=True))

    checked = invoke("notices", "check", "-c", str(config), "--repo", str(repo))
    assert checked.exit_code == 1, checked.output
    assert "compared with the baseline the subscription names" in flat(checked.output)
    assert baseline_of(config) is None

    applied = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert applied.exit_code == 0, applied.output
    [request] = agent.requests
    assert request.notice is not None and request.notice.spec is not None  # noqa: PT018
    assert (request.notice.spec.old_version, request.notice.spec.new_version) == ("1.0", "1.1")
    assert request.notice.spec.baseline_url == old_url
    assert f"compared with: {old_url}" in render_fix_request(request)
    [pr] = github.created
    assert f"at `{old_url}` and `{SPEC_URL}`" in pr["body"]
    assert baseline_of(config).version == "1.1"

    # From then on the stored baseline is used, and the one in the config is not read.
    respx.get(old_url).respond(500)
    again = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert again.exit_code == 0, again.output
    assert "No new notices." in again.output


@respx.mock
def test_a_baseline_that_cannot_be_read_is_an_error(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    old_url = SPEC_URL.replace(".json", "-1.0.json")
    config = follows_spec(tmp_path, baseline=old_url)
    respx.get(old_url).respond(404)
    respx.get(SPEC_URL).respond(200, json=billing_spec("1.1"))
    checked = invoke("notices", "check", "-c", str(config))
    assert checked.exit_code != 0
    assert f"the baseline spec: {old_url} answered HTTP 404" in flat(checked.output)


@respx.mock
def test_a_failed_spec_fix_is_retried_from_the_change_as_it_was_found(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, _ = fakes
    config = follows_spec(tmp_path)
    repo, _ = make_repo(tmp_path)
    route = respx.get(SPEC_URL).respond(200, json=billing_spec("1.0"))
    invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")

    route.respond(200, json=billing_spec("1.1", renamed=True))
    agent.error = RuntimeError("overloaded")
    failed = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert failed.exit_code == 3
    assert [r.status for r in records(config)] == ["failed"]

    # The spec moves on; the retry still works on the 1.0 to 1.1 change.
    route.respond(200, json=billing_spec("1.2", renamed=True, email={"type": "string"}))
    agent.error = None
    retried = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert retried.exit_code == 0, retried.output
    first, second = agent.requests[1], agent.requests[2:] or [None]
    assert first.notice is not None and first.notice.spec is not None  # noqa: PT018
    assert first.notice.spec.new_version == "1.1"
    assert second == [None]  # adding an optional field is no breaking change
    assert baseline_of(config).version == "1.2"
    assert [r.status for r in records(config)] == ["opened"]


@respx.mock
def test_dry_runs_and_cosmetic_spec_changes(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    config = follows_spec(tmp_path)
    repo, _ = make_repo(tmp_path)
    route = respx.get(SPEC_URL).respond(200, json=billing_spec("1.0"))
    invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--dry-run")
    assert baseline_of(config) is None
    invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    digest = baseline_of(config).digest

    route.respond(200, json={**billing_spec("1.0"), "info": {"title": "Renamed", "version": "1.0"}})
    quiet = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert quiet.exit_code == 0, quiet.output
    assert "No new notices." in quiet.output
    assert baseline_of(config).digest != digest
    assert records(config) == []


@respx.mock
def test_versioned_apis_wait_until_the_repository_moves_to_the_new_version(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, _ = fakes
    config = follows_spec(tmp_path, versioned=True)
    repo, _ = make_repo(tmp_path)
    route = respx.get(SPEC_URL).respond(200, json=billing_spec("2026-08-01"))
    invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    route.respond(200, json=billing_spec("2026-09-01", renamed=True))
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert outcome.exit_code == 0, outcome.output
    assert agent.requests == []
    [record] = records(config)
    assert record.status == "unaffected"
    assert "clients pinned to 2026-08-01 keep their responses" in (record.detail or "")


def test_ci_signs_unsigned_drafts_into_a_feed_built_from_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site" / ".well-known"
    site.mkdir(parents=True)
    (site / "api-change-key").write_text(KEY_FILE, encoding="utf-8")
    (site / "api-changes.json").write_text(json.dumps(feed(signed(notice(id="stale")))), "utf-8")
    drafts = []
    for name in ("one", "two"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(notice(id=name)), encoding="utf-8")
        drafts.append(str(path))
    pem = private_key_pem(KEY).decode()
    published = invoke(
        "notice",
        "publish",
        *drafts,
        "--key",
        pem,
        "--feed",
        str(site / "api-changes.json"),
        "--replace",
    )
    assert published.exit_code == 0, published.output
    assert pem not in published.output  # the key is never printed
    built = json.loads((site / "api-changes.json").read_text(encoding="utf-8"))
    assert sorted(n["id"] for n in built["notices"]) == ["one", "two"]  # "stale" is gone
    assert all(n["signature"]["key"] == KEY_URL for n in built["notices"])
    assert "signature" not in json.loads(Path(drafts[0]).read_text(encoding="utf-8"))

    monkeypatch.setenv("TILTMETER_SIGNING_KEY", pem)
    signed_all = invoke("notice", "sign", *drafts)
    assert signed_all.exit_code == 0, signed_all.output
    assert all("signature" in json.loads(Path(d).read_text(encoding="utf-8")) for d in drafts)
    assert invoke("notice", "sign", *drafts, "-o", str(tmp_path / "x.json")).exit_code == 4
    monkeypatch.setenv("TILTMETER_SIGNING_KEY", "-----BEGIN PRIVATE KEY-----\nnope\n")
    bad = invoke("notice", "sign", drafts[0])
    assert bad.exit_code == 4
    assert "the key given as PEM text is not an unencrypted PEM private key" in flat(bad.output)


def test_create_if_changed_is_quiet_when_nothing_changed(tmp_path: Path) -> None:
    spec = str(SPECS / "swagger2" / "old.yaml")
    output = tmp_path / "draft.json"
    quiet = invoke(
        "notice", "create", "--from-openapi", spec, spec, "--if-changed", "-o", str(output)
    )
    assert quiet.exit_code == 0, quiet.output
    assert not output.exists()
    assert invoke("notice", "create", "--from-openapi", spec, spec).exit_code == 4


@respx.mock
def test_a_pushed_branch_needs_yes_before_its_pull_request_opens(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    _, github = fakes
    config = subscribed(tmp_path)
    repo, _ = make_repo(tmp_path)
    git(repo, "push", "--quiet", "origin", f"main:refs/heads/{BRANCH}")
    serve(signed())
    outcome = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--json")
    assert outcome.exit_code == 0, outcome.output
    [applied] = json.loads(outcome.stdout)["applied"]
    assert applied["status"] == "discarded"
    assert github.created == []
    assert records(config) == []  # still new: the next run with --yes opens it


@respx.mock
def test_a_commit_that_stayed_local_is_pushed_on_the_next_run(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    config = subscribed(tmp_path)
    repo, remote = make_repo(tmp_path)
    serve(signed())
    git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    first = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert first.exit_code == 3
    assert git(repo, "branch", "--list", BRANCH).strip()  # committed here, not pushed

    git(repo, "remote", "set-url", "origin", str(remote))
    second = invoke("notices", "apply", "-c", str(config), "--repo", str(repo), "--yes")
    assert second.exit_code == 0, second.output
    assert len(agent.requests) == 1  # the commit is reused, not redone
    assert git(remote, "branch", "--list", BRANCH).strip()
    assert [r.status for r in records(config)] == ["opened"]
    assert len(github.created) == 1


def test_signing_keys_never_show_up_in_errors(tmp_path: Path) -> None:
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    secret = "c2VjcmV0LW5vdC1hLWtleQ=="  # base64, but not of a PEM key
    outcome = invoke("notice", "sign", str(path), "--key", secret)
    assert outcome.exit_code == 4
    assert secret not in outcome.output
    assert "neither PEM text nor an existing file" in flat(outcome.output)
    encoded = base64.b64encode(private_key_pem(KEY)).decode()
    assert invoke("notice", "sign", str(path), "--key", encoded).exit_code == 0


def test_the_change_table_sets_a_rename_apart_from_the_types() -> None:
    from rich.console import Console

    from tiltmeter.console import notice_changes_table
    from tiltmeter.notices.models import NoticeEndpoint

    endpoint = NoticeEndpoint.model_validate(
        {
            "method": "GET",
            "url": f"https://{PROVIDER}/v1/subscriptions/{{id}}",
            "changes": [
                {
                    "path": "$.current_period_end",
                    "kind": "field_removed",
                    "before": "integer",
                    "renamed_to": "$.items.data[*].current_period_end",
                }
            ],
        }
    )
    console = Console(record=True, width=160)
    console.print(notice_changes_table(endpoint))
    assert "integer → —, renamed to $.items.data[*].current_period_end" in console.export_text()
