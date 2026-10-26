"""Run the repository's tests on the agent's edits, before anything is committed.

The command comes from ``fix.test_command`` and runs in the fix's worktree without a
shell. It runs code the agent just wrote from third-party input, so it gets as little as
possible: no network unless ``fix.test_network`` allows it (an unshared network namespace
on Linux, ``sandbox-exec`` on macOS; without either the tests are skipped), an
environment with only ``PATH``, the locale and the names in ``fix.test_env``, and an
empty home folder. Whatever the run writes to the worktree is thrown away afterwards, so
only the agent's own edits are ever committed.

``PATH`` leaves out Tiltmeter's own virtual environment when it lives outside the
repository, as with ``uvx``, ``pipx`` or the GitHub Action: ``python`` must be the
project's, not the one Tiltmeter runs on.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Final

from tiltmeter.fix.git import Git
from tiltmeter.fix.models import TestRun

# Variables the tests always get, when they are set: what finds programs and decodes text.
BASE_ENV: Final = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
)
MAX_OUTPUT: Final = 4_000
_UNSHARE: Final = ("unshare", "--net", "--map-root-user", "--")
_SANDBOX: Final = ("sandbox-exec", "-p", "(version 1)(allow default)(deny network*)")


@cache
def offline_prefix() -> tuple[str, ...] | None:
    """What runs a command with no network here, or None when nothing can."""
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        try:
            probe = subprocess.run(
                [*_UNSHARE, "true"], capture_output=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return _UNSHARE if probe.returncode == 0 else None
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return _SANDBOX
    return None


def test_environment(names: Sequence[str], home: Path, project: Path) -> dict[str, str]:
    """The environment the tests run with: the base variables, the named ones, a new home."""
    env = {name: os.environ[name] for name in (*BASE_ENV, *names) if name in os.environ}
    env |= {"HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(home)}
    if "PATH" in env:
        env["PATH"] = _without_own_environment(env["PATH"], project)
    return env


def _without_own_environment(path: str, project: Path) -> str:
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or prefix.is_relative_to(project.resolve()):
        return path  # no virtual environment, or the project's own
    scripts = prefix / ("Scripts" if os.name == "nt" else "bin")
    kept = [entry for entry in path.split(os.pathsep) if entry and Path(entry).resolve() != scripts]
    return os.pathsep.join(kept)


def run_tests(
    worktree: Git,
    command: Sequence[str],
    *,
    timeout: float,
    env_names: Sequence[str] = (),
    network: bool = False,
    home: Path,
    project: Path,
) -> TestRun:
    """Run ``command`` on the worktree's current edits and put the edits back as they were.

    ``project`` is the user's checkout, which tells a project's virtual environment from
    Tiltmeter's own."""
    shown = shlex.join(command)
    prefix: tuple[str, ...] = ()
    if not network:
        found = offline_prefix()
        if found is None:
            return TestRun(
                command=shown,
                status="skipped",
                reason="nothing here can run them without network access (set "
                "fix.test_network: true to allow it)",
            )
        prefix = found
    home.mkdir(parents=True, exist_ok=True)
    worktree.stage_all()  # the agent's edits, to restore once the tests are done
    env = test_environment(env_names, home, project)
    try:
        passed, output = _run([*prefix, *command], worktree.root, env, timeout)
    finally:
        worktree.run("checkout", "--", ".")
        worktree.run("clean", "-f", "-d", "-x", "-q")
    return TestRun(
        command=shown,
        status="passed" if passed else "failed",
        output=output[-MAX_OUTPUT:],
        offline=not network,
    )


def _run(command: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[bool, str]:
    """Whether the command succeeded, and its output. A timeout ends the whole group."""
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"could not start {command[0]}: {exc.strerror or exc}"
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(process)
        output, _ = process.communicate()
        return False, f"{output}\nThe tests did not finish within {timeout:g} seconds."
    return process.returncode == 0, output


def _stop(process: subprocess.Popen[str]) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
