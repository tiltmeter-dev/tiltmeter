from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.fix.kit import PR_URL, FakeFixAgent, FakeGitHub, git, make_repo
from tests.test_cli import URL, flat, invoke, runner, write_config
from tiltmeter.cli import app, main
from tiltmeter.fix.agent import DeepFixAgent
from tiltmeter.fix.claude_code import ClaudeCodeFixAgent
from tiltmeter.fix.codex import CodexFixAgent

BRANCH = "tiltmeter/fix-items-2"


@pytest.fixture
def drifted(tmp_path: Path) -> Path:
    """A config whose check #2 found ``phone`` renamed to ``phone_number``."""
    config = write_config(tmp_path)
    with respx.mock:
        route = respx.get(URL).respond(200, json={"id": 1, "phone": "555"})
        assert invoke("check", "-c", str(config)).exit_code == 0
        route.respond(200, json={"id": 1, "phone_number": "555"})
        assert invoke("check", "-c", str(config), "--no-llm").exit_code == 2
    return config


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeFixAgent, FakeGitHub]:
    agent, github = FakeFixAgent(), FakeGitHub()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(DeepFixAgent, "from_config", classmethod(lambda cls, _: agent))
    monkeypatch.setattr("tiltmeter.fix.git.GhCli", lambda: github)
    return agent, github


def fix(config: Path, repo: Path, *args: str, stdin: str | None = None) -> Any:
    return runner.invoke(
        app, ["fix", "2", "-c", str(config), "--repo", str(repo), *args], input=stdin
    )


def test_fix_opens_a_pull_request(
    tmp_path: Path, drifted: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    repo, remote = make_repo(tmp_path)
    outcome = fix(drifted, repo, "--yes")
    assert outcome.exit_code == 0, outcome.output
    text = flat(outcome.output)
    assert f"Working on a new branch, {BRANCH}, from main at" in text
    assert "2 files changed +2 -2" in text
    assert "Read phone_number instead of phone." in text
    assert f"✓ opened {PR_URL}" in text
    [request] = agent.requests
    assert request.check_id == 2
    assert [t.renamed_to for t in request.targets] == ["$.phone_number"]
    assert github.created[0]["head"] == BRANCH
    assert git(remote, "branch", "--list", BRANCH).strip()
    shown = flat(invoke("show", "2", "-c", str(drifted)).output)
    assert f"note: fix: pull request {PR_URL}" in shown


def test_fix_asks_before_committing(
    tmp_path: Path, drifted: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    _, github = fakes
    repo, _ = make_repo(tmp_path)
    declined = fix(drifted, repo, stdin="n\n")
    assert declined.exit_code == 0, declined.output
    assert "Commit, push and open a pull request?" in declined.output
    assert "discarded the edits" in flat(declined.output)
    assert git(repo, "branch", "--list", BRANCH) == ""

    accepted = fix(drifted, repo, "--no-pr", stdin="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "Commit to a local branch?" in accepted.output
    assert f"committed to branch {BRANCH}" in flat(accepted.output)
    assert f"git push --set-upstream origin {BRANCH}" in flat(accepted.output)
    assert github.created == []


def test_fix_dry_run_and_json(
    tmp_path: Path, drifted: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    repo, _ = make_repo(tmp_path)
    dry = fix(drifted, repo, "--dry-run", "--json")
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout)
    assert payload["status"] == "discarded"
    assert payload["notes"] == ["--dry-run: nothing was committed"]
    assert payload["preview"]["files"] == ["client.py", "models.py"]
    assert git(repo, "branch", "--list", BRANCH) == ""

    unattended = json.loads(fix(drifted, repo, "--json").stdout)
    assert unattended["status"] == "discarded"
    assert unattended["notes"] == ["pass --yes to commit when printing JSON"]

    opened = json.loads(fix(drifted, repo, "--json", "--yes").stdout)
    assert opened["status"] == "opened"
    assert opened["pr_url"] == PR_URL


def test_fix_exit_codes_for_problems(
    tmp_path: Path,
    drifted: Path,
    fakes: tuple[FakeFixAgent, FakeGitHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _ = fakes
    repo, _ = make_repo(tmp_path)

    agent.edit = lambda root: None
    agent.result = agent.result.model_copy(update={"unresolved": ["export.py: unclear"]})
    nothing = fix(drifted, repo, "--yes")
    assert nothing.exit_code == 1
    assert "the agent changed nothing" in flat(nothing.output)
    assert "export.py: unclear" in flat(nothing.output)

    agent.error = RuntimeError("overloaded")
    failed = fix(drifted, repo, "--yes")
    assert failed.exit_code == 3
    assert "fix failed: overloaded" in flat(failed.output)
    assert git(repo, "branch", "--list", BRANCH) == ""

    assert fix(drifted, tmp_path / "missing").exit_code == 4
    baseline = runner.invoke(app, ["fix", "1", "-c", str(drifted), "--repo", str(repo)])
    assert baseline.exit_code == 0
    assert "nothing to fix" in flat(baseline.output)
    assert "found no changes that code has to follow" in flat(baseline.output)
    assert runner.invoke(app, ["fix", "9", "-c", str(drifted)]).exit_code == 4

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    no_key = fix(drifted, repo)
    assert no_key.exit_code == 4
    assert "ANTHROPIC_API_KEY" in no_key.output
    assert "--agent claude-code" in flat(no_key.output)


def test_fix_with_claude_code_needs_no_api_key(
    tmp_path: Path,
    drifted: Path,
    fakes: tuple[FakeFixAgent, FakeGitHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _ = fakes
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    picked: list[str] = []

    def from_config(cls: type, config: Any, *, environ: Any) -> FakeFixAgent:
        picked.append(config.agent)
        return agent

    monkeypatch.setattr(ClaudeCodeFixAgent, "from_config", classmethod(from_config))
    repo, _ = make_repo(tmp_path)
    outcome = fix(drifted, repo, "--agent", "claude-code", "--yes")
    assert outcome.exit_code == 0, outcome.output
    assert f"✓ opened {PR_URL}" in flat(outcome.output)
    assert picked == ["claude-code"]

    with drifted.open("a", encoding="utf-8") as handle:
        handle.write("fix:\n  agent: claude-code\n")
    git(repo, "branch", "-D", BRANCH)
    from_yaml = fix(drifted, repo, "--yes", "--no-pr")
    assert from_yaml.exit_code == 0, from_yaml.output
    assert picked == ["claude-code", "claude-code"]

    with pytest.raises(SystemExit) as unknown:
        main(["fix", "2", "-c", str(drifted), "--repo", str(repo), "--agent", "opencode"])
    assert unknown.value.code == 4


def test_a_failed_commit_is_reported_without_a_traceback(
    tmp_path: Path,
    drifted: Path,
    fakes: tuple[FakeFixAgent, FakeGitHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tiltmeter.errors import FixError
    from tiltmeter.fix.git import Git

    def no_identity(self: Git, title: str, body: str) -> None:
        raise FixError("`git commit --quiet --no-verify` failed: Please tell me who you are.")

    monkeypatch.setattr(Git, "commit", no_identity)
    repo, _ = make_repo(tmp_path)
    outcome = fix(drifted, repo, "--yes")
    assert outcome.exit_code == 3
    assert "fix failed: `git commit" in flat(outcome.output)
    assert "Please tell me who you are" in flat(outcome.output)
    assert outcome.exception is None or isinstance(outcome.exception, SystemExit)
    assert git(repo, "branch", "--list", BRANCH) == ""


def test_fix_with_codex_needs_no_api_key(
    tmp_path: Path,
    drifted: Path,
    fakes: tuple[FakeFixAgent, FakeGitHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _ = fakes
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    picked: list[str] = []

    def from_config(cls: type, config: Any, *, environ: Any) -> FakeFixAgent:
        picked.append(config.agent)
        return agent

    monkeypatch.setattr(CodexFixAgent, "from_config", classmethod(from_config))
    repo, _ = make_repo(tmp_path)
    outcome = fix(drifted, repo, "--agent", "codex", "--yes")
    assert outcome.exit_code == 0, outcome.output
    assert f"✓ opened {PR_URL}" in flat(outcome.output)
    assert picked == ["codex"]


def config_with_tests(tmp_path: Path, rounds: int = 1) -> Path:
    """Like ``drifted``, with a test command that passes once client.py says FIXED."""
    import sys

    check = (
        "import pathlib, sys\n"
        "text = pathlib.Path('client.py').read_text()\n"
        "print('AttributeError: not fixed yet' if 'FIXED' not in text else 'ok')\n"
        "sys.exit(0 if 'FIXED' in text else 1)\n"
    )
    command = json.dumps([sys.executable, "-c", check])
    extra = f"fix:\n  test_command: {command}\n  test_network: true\n  test_rounds: {rounds}\n"
    config = write_config(tmp_path, extra)
    with respx.mock:
        route = respx.get(URL).respond(200, json={"id": 1, "phone": "555"})
        assert invoke("check", "-c", str(config)).exit_code == 0
        route.respond(200, json={"id": 1, "phone_number": "555"})
        assert invoke("check", "-c", str(config), "--no-llm").exit_code == 2
    return config


def test_failing_tests_go_back_to_the_agent_until_they_pass(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    from tests.fix.kit import rename_phone

    agent, github = fakes
    calls = 0

    def edit(root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            rename_phone(root)
        else:  # the second round, after seeing the failure
            path = root / "client.py"
            path.write_text(path.read_text(encoding="utf-8") + "# FIXED\n", encoding="utf-8")

    agent.edit = edit
    config = config_with_tests(tmp_path)
    repo, _ = make_repo(tmp_path)
    outcome = fix(config, repo, "--yes")
    assert outcome.exit_code == 0, outcome.output
    first, second = agent.requests
    assert first.test_failure is None
    assert second.test_failure is not None
    assert "AttributeError: not fixed yet" in second.test_failure.output
    assert "passed after 2 rounds" in flat(outcome.output)
    [pr] = github.created
    assert "## Tests" in pr["body"]
    assert "passed on these edits after the agent saw them fail" in pr["body"]
    diff = git(repo, "diff", "main", BRANCH)
    assert "phone_number" in diff and "# FIXED" in diff  # both rounds are in the commit  # noqa: PT018


def test_tests_that_still_fail_are_left_for_a_human(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    config = config_with_tests(tmp_path, rounds=1)
    repo, _ = make_repo(tmp_path)
    outcome = fix(config, repo, "--yes")
    assert outcome.exit_code == 0, outcome.output
    assert len(agent.requests) == 2  # the first edit and one more round
    assert "fails after 2 rounds" in flat(outcome.output)
    [pr] = github.created
    assert "fails on these edits. The end of its output:" in pr["body"]
    left = pr["body"].split("## Left for a human", 1)[1]
    assert left.lstrip().startswith("- The tests still fail with these edits")
