"""The fix agent on Claude Code: ``claude -p`` adapts the code inside the worktree.

Claude Code runs headless with file tools only. ``--restricted`` and ``--tools`` remove
the shell, web and code-running tools and keep the file tools inside the worktree; deny
rules passed as settings block ``fix.ignore`` and ``.git``; MCP servers, hooks and the
user's own settings are left out. The run uses the user's Claude Code login, or
``ANTHROPIC_API_KEY`` when it is set, and ends with the report as structured output.

Only the standard library is needed, so this agent works without the ``fix`` extra.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from tiltmeter.errors import FixError
from tiltmeter.fix._proc import failure_reason, run_tool
from tiltmeter.fix.ignore import ALWAYS_BLOCKED
from tiltmeter.fix.models import FixReport, FixRequest
from tiltmeter.fix.prompts import CLAUDE_CODE_WORKSPACE, render_fix_request, system_prompt

if TYPE_CHECKING:
    import subprocess

    from tiltmeter.config import FixConfig

CLAUDE: Final = "claude"
INSTALL_URL: Final = "https://code.claude.com/docs/en/quickstart"
TOOLS: Final = "Read,Edit,Write,Glob,Grep"

# What a running Claude Code session exports to the processes it starts. Dropping them
# makes a fix launched from inside Claude Code run as a session of its own.
SESSION_VARIABLES: Final = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
)

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def clean_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment for ``claude``: the user's, minus the variables of a parent session."""
    env = {key: value for key, value in environ.items() if key not in SESSION_VARIABLES}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def claude_settings(ignore: Iterable[str]) -> dict[str, Any]:
    """Settings for the run: deny rules for protected paths, no outside reads, no hooks.

    A pattern without a slash matches a name at any depth, like in ``fix.ignore``; one
    with a slash is anchored at the working directory, which is the worktree root.
    """
    globs: list[str] = []
    for pattern in (*ALWAYS_BLOCKED, *ignore):
        anchored = pattern.strip("/")
        base = [f"./{anchored}"] if "/" in anchored else [anchored, f"**/{anchored}"]
        globs += [*base, *(f"{glob}/**" for glob in base)]
    rules = [f"{tool}({glob})" for glob in dict.fromkeys(globs) for tool in ("Read", "Edit")]
    return {
        "permissions": {"deny": rules, "blockReadsOutsideWorkingDirectories": True},
        "disableAllHooks": True,
    }


def report_in_text(text: object) -> dict[str, Any] | None:
    """A report written as JSON in the final message, when structured output is missing."""
    if not isinstance(text, str):
        return None
    candidates = [text.strip(), *_FENCED_JSON.findall(text)]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_result(completed: subprocess.CompletedProcess[str], *, max_turns: int) -> FixReport:
    """The report at the end of a ``claude -p --output-format json`` run."""
    try:
        data = json.loads(completed.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise FixError(
            f"claude exited with code {completed.returncode}: {failure_reason(completed)}"
        )
    subtype = data.get("subtype")
    if subtype == "error_max_turns":
        raise FixError(
            f"the agent used its {max_turns} turns without finishing "
            "(raise fix.max_model_calls for large repositories)"
        )
    if data.get("is_error") or subtype not in (None, "success"):
        errors = data.get("errors") or [data.get("result") or subtype or "unknown error"]
        raise FixError(f"Claude Code failed: {'; '.join(str(error) for error in errors)}")
    report = data.get("structured_output")
    if not isinstance(report, Mapping):
        report = report_in_text(data.get("result"))
    if report is None:
        raise FixError("the agent stopped without reporting what it changed")
    try:
        return FixReport.model_validate(report)
    except ValidationError as exc:
        raise FixError(f"the agent's report did not match the expected shape: {exc}") from exc


class ClaudeCodeFixAgent:
    """A :class:`~tiltmeter.fix.agent.FixAgent` that runs ``claude -p`` in the worktree."""

    def __init__(
        self,
        executable: Sequence[str],
        *,
        model: str,
        effort: str | None,
        max_turns: int,
        timeout: float,
        ignore: Sequence[str] = (),
        environ: Mapping[str, str],
    ) -> None:
        self._executable = list(executable)
        self._model = model
        self._effort = effort
        self._max_turns = max_turns
        self._timeout = timeout
        self._ignore = list(ignore)
        self._env = clean_environment(environ)

    @property
    def model_name(self) -> str:
        return f"Claude Code with {self._model}"

    @classmethod
    def from_config(cls, config: FixConfig, *, environ: Mapping[str, str]) -> ClaudeCodeFixAgent:
        """Find ``claude`` and make sure it can run before any work starts."""
        executable = shutil.which(CLAUDE, path=environ.get("PATH"))
        if executable is None:
            raise FixError(
                f"Claude Code is not installed (claude is not on PATH); see {INSTALL_URL}"
            )
        env = clean_environment(environ)
        if not env.get("ANTHROPIC_API_KEY"):
            status = run_tool([executable, "auth", "status"], cwd=Path.cwd(), env=env, timeout=60)
            if status.returncode != 0:
                raise FixError(
                    "Claude Code is not logged in: run `claude` once and log in, "
                    "or set ANTHROPIC_API_KEY"
                )
        return cls(
            [executable],
            model=config.model,
            effort=config.effort,
            max_turns=config.max_model_calls,
            timeout=config.timeout * config.max_model_calls,
            ignore=config.ignore,
            environ=environ,
        )

    def command(self, *, system_file: Path, settings_file: Path) -> list[str]:
        """The ``claude`` invocation. The fix request itself goes in through stdin."""
        schema = json.dumps(FixReport.model_json_schema(), separators=(",", ":"))
        command = [
            *self._executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--restricted",
            "--tools",
            TOOLS,
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
            "--append-system-prompt-file",
            str(system_file),
            "--settings",
            str(settings_file),
            "--no-session-persistence",
            "--max-turns",
            str(self._max_turns),
            "--model",
            self._model,
        ]
        if self._effort is not None:
            command += ["--effort", self._effort]
        return command

    async def run(self, request: FixRequest, root: Path) -> FixReport:
        # The instructions and settings live outside the worktree, so they never reach the diff.
        with tempfile.TemporaryDirectory(prefix="tiltmeter-claude-") as tmp:
            system_file = Path(tmp) / "system.md"
            system_file.write_text(system_prompt(CLAUDE_CODE_WORKSPACE), encoding="utf-8")
            settings_file = Path(tmp) / "settings.json"
            settings_file.write_text(json.dumps(claude_settings(self._ignore)), encoding="utf-8")
            completed = await asyncio.to_thread(
                run_tool,
                self.command(system_file=system_file, settings_file=settings_file),
                cwd=root,
                env=self._env,
                stdin=render_fix_request(request),
                timeout=self._timeout,
            )
        return parse_result(completed, max_turns=self._max_turns)
