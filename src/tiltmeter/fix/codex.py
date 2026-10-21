"""The fix agent on Codex: ``codex exec`` adapts the code inside the worktree.

Codex reads files through its shell, so the shell stays, inside Codex's own sandbox
(bubblewrap, Landlock and seccomp on Linux; Seatbelt on macOS) with a permission profile
passed on the command line:

- nothing on disk is readable except the worktree, the system paths commands need and
  Codex's own installation;
- only the worktree is writable, and ``fix.ignore`` and ``.git`` are denied inside it;
- commands get no network and only a core set of environment variables;
- tools that would act outside that sandbox (apps, browser, computer use, plugins,
  subagents, image viewing, web search) are turned off.

The user's ``config.toml``, rules and hooks are not loaded, the session is not saved,
and the run ends with the report as structured output. It uses the user's Codex login,
or ``CODEX_API_KEY`` when it is set.

Only the standard library is needed, so this agent works without the ``fix`` extra.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from tiltmeter.errors import FixError
from tiltmeter.fix._proc import failure_reason, run_tool
from tiltmeter.fix.claude_code import report_in_text
from tiltmeter.fix.ignore import ALWAYS_BLOCKED
from tiltmeter.fix.models import FixReport, FixRequest
from tiltmeter.fix.prompts import CODEX_WORKSPACE, render_fix_request, system_prompt

if TYPE_CHECKING:
    import subprocess

    from tiltmeter.config import FixConfig

CODEX: Final = "codex"
INSTALL_URL: Final = "https://developers.openai.com/codex/cli"
PROFILE: Final = "tiltmeter-fix"

# Tools that run outside the command sandbox or reach other services.
DISABLED_FEATURES: Final = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_suggest",
    "view_image",
    "workspace_dependencies",
)

# Codex takes the same names, except that it stops at xhigh.
_EFFORT: Final = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}
# Deny globs are expanded when the sandbox starts on Linux; this keeps them unbounded in practice.
_GLOB_SCAN_DEPTH: Final = 64


def install_root(executable: str) -> Path:
    """The folder Codex is installed in, which its sandbox must be able to read.

    The sandbox starts commands by running Codex's own binary again, so the binary has
    to stay readable. ``codex`` resolves to ``<root>/bin/codex`` for the standalone
    installer, to ``<root>/bin/codex.js`` for npm and to ``<root>/<binary>`` for Homebrew.
    """
    return Path(os.path.realpath(executable)).parent.parent


def deny_globs(ignore: Iterable[str]) -> list[str]:
    """``fix.ignore`` and ``.git`` as globs relative to the worktree root."""
    globs: list[str] = []
    for pattern in (*ALWAYS_BLOCKED, *ignore):
        anchored = pattern.strip("/")
        base = anchored if "/" in anchored else f"**/{anchored}"
        globs += [base, f"{base}/**"]
    return list(dict.fromkeys(globs))


def permission_profile(ignore: Iterable[str], readable: Iterable[Path]) -> dict[str, Any]:
    """The filesystem rules: deny everything, then open what the fix needs."""
    filesystem: dict[str, Any] = {":root": "deny", ":minimal": "read"}
    filesystem.update({str(path): "read" for path in readable})
    filesystem["glob_scan_max_depth"] = _GLOB_SCAN_DEPTH
    filesystem[":workspace_roots"] = {".": "write", **dict.fromkeys(deny_globs(ignore), "deny")}
    return {"filesystem": filesystem, "network": {"enabled": False}}


def toml_value(value: Any) -> str:
    """A TOML inline value for ``codex -c``. JSON strings are valid TOML strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        items = ", ".join(f"{json.dumps(str(key))} = {toml_value(v)}" for key, v in value.items())
        return "{" + items + "}"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def parse_result(completed: subprocess.CompletedProcess[str], last_message: str) -> FixReport:
    """The report at the end of a ``codex exec --json`` run.

    Only a failed turn or a non-zero exit fails the run. Stray ``error`` events, such as
    reconnection notices, only explain a failure when there is one.
    """
    failed: str | None = None
    last_error: str | None = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.failed":
            error = event.get("error")
            failed = str(error.get("message") if isinstance(error, dict) else error)
        elif event.get("type") == "error":
            last_error = str(event.get("message"))
    if completed.returncode != 0 or failed is not None:
        reason = failed or last_error or failure_reason(completed)
        raise FixError(f"Codex failed (exit code {completed.returncode}): {reason}")
    report = report_in_text(last_message)
    if report is None:
        raise FixError("the agent stopped without reporting what it changed")
    try:
        return FixReport.model_validate(report)
    except ValidationError as exc:
        raise FixError(f"the agent's report did not match the expected shape: {exc}") from exc


class CodexFixAgent:
    """A :class:`~tiltmeter.fix.agent.FixAgent` that runs ``codex exec`` in the worktree."""

    def __init__(
        self,
        executable: Sequence[str],
        *,
        model: str | None,
        effort: str | None,
        timeout: float,
        ignore: Sequence[str] = (),
        readable: Sequence[Path] = (),
        environ: Mapping[str, str],
    ) -> None:
        self._executable = list(executable)
        self._model = model
        self._effort = effort
        self._timeout = timeout
        self._ignore = list(ignore)
        self._readable = list(readable)
        self._env = {**environ, "GIT_TERMINAL_PROMPT": "0"}

    @property
    def model_name(self) -> str:
        return f"Codex with {self._model}" if self._model else "Codex"

    @classmethod
    def from_config(cls, config: FixConfig, *, environ: Mapping[str, str]) -> CodexFixAgent:
        """Find ``codex`` and make sure it can run before any work starts."""
        executable = shutil.which(CODEX, path=environ.get("PATH"))
        if executable is None:
            raise FixError(f"Codex is not installed (codex is not on PATH); see {INSTALL_URL}")
        env = {**environ, "GIT_TERMINAL_PROMPT": "0"}
        if not environ.get("CODEX_API_KEY"):
            status = run_tool([executable, "login", "status"], cwd=Path.cwd(), env=env, timeout=60)
            if status.returncode != 0:
                raise FixError("Codex is not logged in: run `codex login`, or set CODEX_API_KEY")
        # fix.model defaults to a Claude model; Codex then picks its own default.
        model = None if config.model.startswith("claude") else config.model
        return cls(
            [executable],
            model=model,
            effort=config.effort,
            timeout=config.timeout * config.max_model_calls,
            ignore=config.ignore,
            readable=[install_root(executable)],
            environ=environ,
        )

    def command(self, root: Path, *, schema_file: Path, last_message_file: Path) -> list[str]:
        """The ``codex exec`` invocation. The instructions and request go in through stdin."""
        overrides = {
            "default_permissions": PROFILE,
            f"permissions.{PROFILE}": permission_profile(self._ignore, self._readable),
            "approval_policy": "never",
            "shell_environment_policy.inherit": "core",
            "web_search": "disabled",
        }
        if self._effort is not None:
            overrides["model_reasoning_effort"] = _EFFORT.get(self._effort, self._effort)
        command = [
            *self._executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--cd",
            str(root),
            "--output-schema",
            str(schema_file),
            "--output-last-message",
            str(last_message_file),
        ]
        for key, value in overrides.items():
            command += ["--config", f"{key}={toml_value(value)}"]
        for feature in DISABLED_FEATURES:
            command += ["--disable", feature]
        if self._model is not None:
            command += ["--model", self._model]
        return [*command, "-"]

    async def run(self, request: FixRequest, root: Path) -> FixReport:
        prompt = f"{system_prompt(CODEX_WORKSPACE)}\n\n{render_fix_request(request)}"
        # The schema and the final message live outside the worktree, so they never
        # reach the diff.
        with tempfile.TemporaryDirectory(prefix="tiltmeter-codex-") as tmp:
            schema_file = Path(tmp) / "report.schema.json"
            schema_file.write_text(json.dumps(FixReport.model_json_schema()), encoding="utf-8")
            last_message_file = Path(tmp) / "last-message.txt"
            completed = await asyncio.to_thread(
                run_tool,
                self.command(root, schema_file=schema_file, last_message_file=last_message_file),
                cwd=root,
                env=self._env,
                stdin=prompt,
                timeout=self._timeout,
            )
            last_message = (
                last_message_file.read_text(encoding="utf-8") if last_message_file.exists() else ""
            )
        return parse_result(completed, last_message)
