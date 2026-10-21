"""Run the command-line tools a fix relies on: git, gh and agent CLIs.

Commands run without a shell, with their output captured and decoded as UTF-8, and a
missing executable or a timeout becomes a :class:`~tiltmeter.errors.FixError`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from tiltmeter.errors import FixError


def run_tool(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=dict(env),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FixError(f"{command[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise FixError(
            f"{Path(command[0]).name} did not finish within {timeout:g} seconds"
        ) from exc


def failure_reason(completed: subprocess.CompletedProcess[str]) -> str:
    """The most telling line of a failed command's output."""
    lines = [line.strip() for line in (completed.stderr or completed.stdout).splitlines()]
    lines = [line for line in lines if line]
    for line in lines:
        if line.startswith(("fatal:", "error:", "Error:")):
            return line
    return lines[-1] if lines else f"exit code {completed.returncode}"
