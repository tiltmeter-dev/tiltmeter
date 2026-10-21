from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests.fix.kit import CLIENT, MODELS, rename_request
from tiltmeter.config import FixConfig
from tiltmeter.errors import FixError
from tiltmeter.fix.codex import (
    DISABLED_FEATURES,
    PROFILE,
    CodexFixAgent,
    deny_globs,
    install_root,
    parse_result,
    permission_profile,
    toml_value,
)
from tiltmeter.fix.models import FixReport
from tiltmeter.fix.prompts import CODEX_WORKSPACE, render_fix_request

FAKE = Path(__file__).with_name("fake_codex.py")
INSTALL = Path("/opt/codex/releases/1.0")


def make_agent(
    tmp_path: Path, scenario: str = "success", **overrides: Any
) -> tuple[CodexFixAgent, Path]:
    log = tmp_path / "codex-run.json"
    environ = {**os.environ, "FAKE_CODEX_LOG": str(log), "FAKE_CODEX_SCENARIO": scenario}
    options: dict[str, Any] = {
        "model": None,
        "effort": "medium",
        "timeout": 60.0,
        "ignore": [".env*", "infra/prod"],
        "readable": [INSTALL],
        "environ": environ,
    }
    options.update(overrides)
    return CodexFixAgent([sys.executable, str(FAKE)], **options), log


def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir(parents=True)
    (root / "client.py").write_text(CLIENT, encoding="utf-8")
    (root / "models.py").write_text(MODELS, encoding="utf-8")
    return root


def overrides(args: list[str]) -> dict[str, Any]:
    """The ``--config key=value`` pairs, parsed the way Codex parses them: as TOML."""
    values: dict[str, Any] = {}
    for index, arg in enumerate(args):
        if arg == "--config":
            key, _, value = args[index + 1].partition("=")
            values[key] = tomllib.loads(f"value = {value}")["value"]
    return values


async def test_codex_runs_in_a_sandbox_that_only_sees_the_worktree(tmp_path: Path) -> None:
    agent, log = make_agent(tmp_path)
    root = worktree(tmp_path)
    result = await agent.run(rename_request(), root)
    assert result.summary == "Read phone_number instead of phone."
    check_sandboxed_run(log, root)


def check_sandboxed_run(log: Path, root: Path) -> None:
    assert 'customer["phone_number"]' in (root / "client.py").read_text(encoding="utf-8")
    assert sorted(path.name for path in root.iterdir()) == ["client.py", "models.py"]

    seen = json.loads(log.read_text(encoding="utf-8"))
    args: list[str] = seen["argv"]

    def value(name: str) -> str:
        return args[args.index(name) + 1]

    assert args[0] == "exec"
    assert args[-1] == "-"  # the prompt comes in through stdin
    for flag in ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
        assert flag in args
    for flag in ("--sandbox", "--full-auto", "--add-dir", "--model"):
        assert flag not in args  # --sandbox would replace the permission profile
    assert not [arg for arg in args if arg.startswith("--dangerously")]
    assert Path(value("--cd")).resolve() == root.resolve()
    assert Path(seen["cwd"]).resolve() == root.resolve()
    assert seen["schema"] == FixReport.model_json_schema()
    for path in (Path(value("--output-schema")), Path(value("--output-last-message"))):
        assert not path.exists()  # removed after the run
        assert not path.resolve().is_relative_to(root.resolve())

    disabled = [args[index + 1] for index, arg in enumerate(args) if arg == "--disable"]
    assert disabled == list(DISABLED_FEATURES)
    assert {"apps", "browser_use", "computer_use", "hooks", "plugins", "view_image"} <= set(
        disabled
    )

    config = overrides(args)
    assert config["default_permissions"] == PROFILE
    assert config["approval_policy"] == "never"
    assert config["shell_environment_policy.inherit"] == "core"
    assert config["web_search"] == "disabled"
    assert config["model_reasoning_effort"] == "medium"
    profile = config[f"permissions.{PROFILE}"]
    assert profile["network"] == {"enabled": False}
    filesystem = profile["filesystem"]
    assert filesystem[":root"] == "deny"
    assert filesystem[":minimal"] == "read"
    assert filesystem[str(INSTALL)] == "read"
    workspace = filesystem[":workspace_roots"]
    assert workspace["."] == "write"
    assert {"**/.env*", "**/.env*/**", "infra/prod", "infra/prod/**", "**/.git"} <= {
        glob for glob, access in workspace.items() if access == "deny"
    }

    prompt: str = seen["stdin"]
    assert prompt.startswith("You are Tiltmeter's fixer.")
    assert CODEX_WORKSPACE in prompt
    assert prompt.endswith(render_fix_request(rename_request()))
    assert "GIT_TERMINAL_PROMPT" in seen["env"]


async def test_model_and_effort_are_passed_on(tmp_path: Path) -> None:
    agent, log = make_agent(tmp_path, model="gpt-5.5-codex", effort="max")
    await agent.run(rename_request(), worktree(tmp_path))
    args = json.loads(log.read_text(encoding="utf-8"))["argv"]
    assert args[args.index("--model") + 1] == "gpt-5.5-codex"
    assert overrides(args)["model_reasoning_effort"] == "xhigh"  # Codex stops at xhigh
    assert agent.model_name == "Codex with gpt-5.5-codex"

    unset, log = make_agent(tmp_path, effort=None)
    await unset.run(rename_request(), worktree(tmp_path / "again"))
    assert "model_reasoning_effort" not in overrides(json.loads(log.read_text())["argv"])
    assert unset.model_name == "Codex"


async def test_a_report_in_a_fenced_block_is_accepted(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path, "text")
    result = await agent.run(rename_request(), worktree(tmp_path))
    assert result.files_changed == ["client.py", "models.py"]


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("failed", r"Codex failed \(exit code 1\): You've hit your usage limit\."),
        ("crash", "Codex failed \\(exit code 2\\): error: unexpected argument '--ignore-rules'"),
        ("silent", "without reporting what it changed"),
    ],
)
async def test_failed_runs_explain_why(tmp_path: Path, scenario: str, message: str) -> None:
    agent, _ = make_agent(tmp_path, scenario)
    with pytest.raises(FixError, match=message):
        await agent.run(rename_request(), worktree(tmp_path))


async def test_a_hung_codex_is_stopped(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path, "slow", timeout=0.5)
    with pytest.raises(FixError, match=r"did not finish within 0\.5 seconds"):
        await agent.run(rename_request(), worktree(tmp_path))


def test_reports_of_the_wrong_shape_are_rejected() -> None:
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with pytest.raises(FixError, match="did not match the expected shape"):
        parse_result(completed, json.dumps({"summary": "x"}))


def test_deny_globs_cover_names_at_any_depth_and_anchored_paths() -> None:
    globs = deny_globs(["secrets*", "/infra/prod/"])
    assert globs == [
        "**/.git",
        "**/.git/**",
        "**/secrets*",
        "**/secrets*/**",
        "infra/prod",
        "infra/prod/**",
    ]


def test_the_profile_survives_a_round_trip_through_toml() -> None:
    profile = permission_profile(['we"ird*', "a/b"], [Path("/opt/codex")])
    assert tomllib.loads(f"value = {toml_value(profile)}")["value"] == profile


def test_install_root_follows_links(tmp_path: Path) -> None:
    release = tmp_path / "releases" / "1.0"
    (release / "bin").mkdir(parents=True)
    binary = release / "bin" / "codex"
    binary.write_text("", encoding="utf-8")
    link = tmp_path / "codex"
    try:
        link.symlink_to(binary)
    except OSError:
        pytest.skip("symlinks are not available")
    assert install_root(str(link)) == release.resolve()


class LoginStatus:
    """Records ``codex login status`` calls and answers with a fixed exit code."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, self.code, stdout="", stderr="")


def installed(monkeypatch: pytest.MonkeyPatch, status: LoginStatus) -> None:
    monkeypatch.setattr("tiltmeter.fix.codex.shutil.which", lambda *_, **__: "/opt/c/bin/codex")
    monkeypatch.setattr("tiltmeter.fix.codex.run_tool", status)


def test_from_config_needs_codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tiltmeter.fix.codex.shutil.which", lambda *_, **__: None)
    with pytest.raises(FixError, match=r"Codex is not installed .*developers.openai.com"):
        CodexFixAgent.from_config(FixConfig(agent="codex"), environ={})


def test_from_config_needs_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    status = LoginStatus(1)
    installed(monkeypatch, status)
    with pytest.raises(FixError, match="Codex is not logged in"):
        CodexFixAgent.from_config(FixConfig(agent="codex"), environ={})
    assert status.calls == [["/opt/c/bin/codex", "login", "status"]]


def test_from_config_with_a_login_or_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    status = LoginStatus(0)
    installed(monkeypatch, status)
    # The default fix.model is a Claude model, so Codex picks its own.
    agent = CodexFixAgent.from_config(FixConfig(agent="codex"), environ={})
    assert agent.model_name == "Codex"
    assert len(status.calls) == 1

    config = FixConfig(agent="codex", model="gpt-5.5-codex")
    keyed = CodexFixAgent.from_config(config, environ={"CODEX_API_KEY": "sk-1"})
    assert len(status.calls) == 1  # an API key needs no login check
    assert keyed.model_name == "Codex with gpt-5.5-codex"
    command = keyed.command(Path("wt"), schema_file=Path("s"), last_message_file=Path("m"))
    assert command[0] == "/opt/c/bin/codex"
    profile = overrides(command)[f"permissions.{PROFILE}"]
    assert profile["filesystem"][str(Path("/opt/c").resolve())] == "read"
