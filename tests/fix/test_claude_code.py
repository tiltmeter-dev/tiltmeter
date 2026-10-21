from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fix.kit import CLIENT, MODELS, rename_request
from tiltmeter.config import FixConfig
from tiltmeter.errors import FixError
from tiltmeter.fix.claude_code import (
    SESSION_VARIABLES,
    ClaudeCodeFixAgent,
    claude_settings,
    clean_environment,
    parse_result,
)
from tiltmeter.fix.models import FixReport
from tiltmeter.fix.prompts import SYSTEM_PROMPT, render_fix_request

FAKE = Path(__file__).with_name("fake_claude.py")
PARENT_SESSION = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_EFFORT": "max"}


def make_agent(
    tmp_path: Path, scenario: str = "success", **overrides: Any
) -> tuple[ClaudeCodeFixAgent, Path]:
    log = tmp_path / "claude-run.json"
    environ = {
        **os.environ,
        **PARENT_SESSION,
        "FAKE_CLAUDE_LOG": str(log),
        "FAKE_CLAUDE_SCENARIO": scenario,
    }
    options: dict[str, Any] = {
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "max_turns": 40,
        "timeout": 60.0,
        "ignore": [".env*", "infra/prod"],
        "environ": environ,
    }
    options.update(overrides)
    return ClaudeCodeFixAgent([sys.executable, str(FAKE)], **options), log


def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "client.py").write_text(CLIENT, encoding="utf-8")
    (root / "models.py").write_text(MODELS, encoding="utf-8")
    return root


async def test_claude_runs_headless_with_file_tools_only(tmp_path: Path) -> None:
    agent, log = make_agent(tmp_path)
    root = worktree(tmp_path)
    result = await agent.run(rename_request(), root)
    assert result.summary == "Read phone_number instead of phone."
    check_restricted_run(log, root)


def check_restricted_run(log: Path, root: Path) -> None:
    assert 'customer["phone_number"]' in (root / "client.py").read_text(encoding="utf-8")
    assert sorted(path.name for path in root.iterdir()) == ["client.py", "models.py"]

    seen = json.loads(log.read_text(encoding="utf-8"))
    args: list[str] = seen["argv"]

    def value(name: str) -> str:
        return args[args.index(name) + 1]

    assert args[0] == "-p"
    assert value("--output-format") == "json"
    assert json.loads(value("--json-schema")) == FixReport.model_json_schema()
    assert value("--tools") == "Read,Edit,Write,Glob,Grep"
    assert value("--disallowedTools") == "mcp__*"
    assert value("--permission-mode") == "acceptEdits"
    assert value("--permission-prompts") == "none"
    assert (value("--max-turns"), value("--model"), value("--effort")) == (
        "40",
        "claude-sonnet-5-5",
        "medium",
    )
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in args
    assert "--bare" not in args  # it would ignore the user's Claude Code login

    assert seen["stdin"] == render_fix_request(rename_request())
    assert Path(seen["cwd"]).resolve() == root.resolve()
    assert not set(PARENT_SESSION) & set(seen["env"])
    assert "GIT_TERMINAL_PROMPT" in seen["env"]

    assert "there is no shell" in seen["system"]
    assert "The repository root is /." not in seen["system"]
    deny = set(seen["settings"]["permissions"]["deny"])
    assert {
        "Read(.env*)",
        "Read(**/.env*)",
        "Edit(.env*)",
        "Read(./infra/prod)",
        "Read(./infra/prod/**)",
        "Read(.git)",
        "Edit(**/.git/**)",
    } <= deny
    assert seen["settings"]["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert seen["settings"]["disableAllHooks"] is True
    settings_file = Path(value("--settings"))
    assert not settings_file.exists()  # removed after the run
    assert not settings_file.resolve().is_relative_to(root.resolve())


async def test_effort_is_left_to_claude_when_unset(tmp_path: Path) -> None:
    agent, log = make_agent(tmp_path, effort=None)
    await agent.run(rename_request(), worktree(tmp_path))
    assert "--effort" not in json.loads(log.read_text(encoding="utf-8"))["argv"]


async def test_a_report_in_the_final_message_is_accepted(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path, "text")
    result = await agent.run(rename_request(), worktree(tmp_path))
    assert result.files_changed == ["client.py", "models.py"]


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("max-turns", r"used its 40 turns without finishing \(raise fix.max_model_calls"),
        ("error", "Claude Code failed: API Error: 529 Overloaded"),
        ("crash", "claude exited with code 1: Error: --json-schema is not valid JSON"),
        ("silent", "without reporting what it changed"),
    ],
)
async def test_failed_runs_explain_why(tmp_path: Path, scenario: str, message: str) -> None:
    agent, _ = make_agent(tmp_path, scenario)
    with pytest.raises(FixError, match=message):
        await agent.run(rename_request(), worktree(tmp_path))


async def test_a_hung_claude_is_stopped(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path, "slow", timeout=0.5)
    with pytest.raises(FixError, match=r"did not finish within 0\.5 seconds"):
        await agent.run(rename_request(), worktree(tmp_path))


def test_reports_of_the_wrong_shape_are_rejected() -> None:
    output = {"type": "result", "subtype": "success", "structured_output": {"summary": "x"}}
    completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(output), stderr="")
    with pytest.raises(FixError, match="did not match the expected shape"):
        parse_result(completed, max_turns=3)


def test_settings_cover_names_at_any_depth_and_anchored_paths() -> None:
    deny = claude_settings(["secrets*", "/infra/prod/"])["permissions"]["deny"]
    assert {"Read(secrets*)", "Read(**/secrets*/**)", "Read(./infra/prod/**)"} <= set(deny)
    assert {"Read(.git)", "Read(**/.git)", "Edit(.git/**)"} <= set(deny)
    assert all(rule.startswith(("Read(", "Edit(")) for rule in deny)
    assert len(deny) == len(set(deny))


def test_the_environment_drops_the_parent_session_only() -> None:
    env = clean_environment(
        {**PARENT_SESSION, "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-1", "HOME": "/h"}
    )
    assert not set(SESSION_VARIABLES) & set(env)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-1"
    assert (env["PATH"], env["HOME"], env["GIT_TERMINAL_PROMPT"]) == ("/usr/bin", "/h", "0")


def test_the_deep_agents_prompt_is_unchanged() -> None:
    assert "Use glob to find candidate files. The repository root is /." in SYSTEM_PROMPT


class AuthStatus:
    """Records ``claude auth status`` calls and answers with a fixed exit code."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), dict(kwargs["env"])))
        return subprocess.CompletedProcess(command, self.code, stdout="{}", stderr="")


def installed(monkeypatch: pytest.MonkeyPatch, status: AuthStatus) -> None:
    monkeypatch.setattr("tiltmeter.fix.claude_code.shutil.which", lambda *_, **__: "/bin/claude")
    monkeypatch.setattr("tiltmeter.fix.claude_code.run_tool", status)


def test_from_config_needs_claude_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tiltmeter.fix.claude_code.shutil.which", lambda *_, **__: None)
    with pytest.raises(FixError, match=r"Claude Code is not installed .*code.claude.com"):
        ClaudeCodeFixAgent.from_config(FixConfig(agent="claude-code"), environ={})


def test_from_config_needs_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    status = AuthStatus(1)
    installed(monkeypatch, status)
    with pytest.raises(FixError, match="Claude Code is not logged in"):
        ClaudeCodeFixAgent.from_config(FixConfig(agent="claude-code"), environ=PARENT_SESSION)
    [(command, env)] = status.calls
    assert command == ["/bin/claude", "auth", "status"]
    assert not set(PARENT_SESSION) & set(env)


def test_from_config_with_a_login_or_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    status = AuthStatus(0)
    installed(monkeypatch, status)
    config = FixConfig(agent="claude-code", model="claude-opus-5-5", max_model_calls=10)
    agent = ClaudeCodeFixAgent.from_config(config, environ={})
    assert agent.model_name == "Claude Code with claude-opus-5-5"
    assert len(status.calls) == 1

    keyed = ClaudeCodeFixAgent.from_config(config, environ={"ANTHROPIC_API_KEY": "sk-ant-1"})
    assert len(status.calls) == 1  # an API key needs no login check
    command = keyed.command(system_file=Path("s.md"), settings_file=Path("s.json"))
    assert command[0] == "/bin/claude"
    assert command[command.index("--max-turns") + 1] == "10"
