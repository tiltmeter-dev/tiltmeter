"""Git and GitHub, through their own command-line tools.

The fix happens in a separate worktree, on a new branch created from the current
HEAD in a temporary folder. The user's checkout, index and uncommitted changes are
never touched. Commands run without a shell and never prompt for credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from tiltmeter.errors import FixError
from tiltmeter.fix._proc import failure_reason, run_tool


def _call(
    command: Sequence[str], *, cwd: Path, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    return run_tool(command, cwd=cwd, env=env, stdin=stdin)


def _checked(command: Sequence[str], *, cwd: Path, stdin: str | None = None) -> str:
    completed = _call(command, cwd=cwd, stdin=stdin)
    if completed.returncode != 0:
        raise FixError(f"`{' '.join(command[:3])}` failed: {failure_reason(completed)}")
    return completed.stdout


class Git:
    """A git working tree: the user's checkout, or the worktree a fix is made in."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open(cls, path: Path) -> Git:
        """The repository that contains ``path``, which must have at least one commit."""
        if not path.is_dir():
            raise FixError(f"{path} is not a directory")
        completed = _call(["git", "rev-parse", "--show-toplevel"], cwd=path)
        if completed.returncode != 0:
            raise FixError(f"{path} is not inside a git repository")
        repo = cls(Path(completed.stdout.strip()))
        if not repo.succeeds("rev-parse", "--verify", "--quiet", "HEAD"):
            raise FixError(f"{repo.root} has no commits yet")
        return repo

    def run(self, *args: str) -> str:
        return _checked(["git", *args], cwd=self.root)

    def succeeds(self, *args: str) -> bool:
        return _call(["git", *args], cwd=self.root).returncode == 0

    def current_branch(self) -> str | None:
        """The checked-out branch, or None on a detached HEAD."""
        completed = _call(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=self.root)
        return (completed.stdout.strip() or None) if completed.returncode == 0 else None

    def head(self) -> str:
        return self.run("rev-parse", "--short", "HEAD").strip()

    def has_uncommitted_changes(self) -> bool:
        return bool(self.run("status", "--porcelain", "--untracked-files=no").strip())

    def branch_exists(self, name: str) -> bool:
        return self.succeeds("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")

    def remote(self) -> str | None:
        """``origin`` when it exists, else the first remote, else None."""
        remotes = self.run("remote").split()
        if "origin" in remotes:
            return "origin"
        return remotes[0] if remotes else None

    # -- worktrees ------------------------------------------------------------

    def add_worktree(self, path: Path, branch: str) -> Git:
        """Check out a new ``branch`` from HEAD into ``path``."""
        self.run("worktree", "add", "--quiet", "-b", branch, str(path), "HEAD")
        return Git(path)

    def remove_worktree(self, path: Path) -> None:
        _call(["git", "worktree", "remove", "--force", str(path)], cwd=self.root)
        _call(["git", "worktree", "prune"], cwd=self.root)

    def delete_branch(self, name: str) -> None:
        _call(["git", "branch", "-D", name], cwd=self.root)

    # -- inside a worktree ----------------------------------------------------

    def stage_all(self) -> None:
        self.run("add", "--all")

    def staged_files(self) -> list[str]:
        output = self.run("diff", "--cached", "--name-only", "--no-renames", "-z")
        return [name for name in output.split("\0") if name]

    def staged_counts(self) -> tuple[int, int]:
        """Added and deleted lines in the staged changes (binary files count as zero)."""
        additions = deletions = 0
        for line in self.run("diff", "--cached", "--numstat", "--no-renames").splitlines():
            added, deleted, _ = line.split("\t", 2)
            additions += int(added) if added.isdigit() else 0
            deletions += int(deleted) if deleted.isdigit() else 0
        return additions, deletions

    def staged_diff(self) -> str:
        return self.run("diff", "--cached", "--no-color", "--no-ext-diff")

    def commit(self, title: str, body: str) -> None:
        # Local hooks are skipped like any bot commit; CI checks the pull request.
        self.run("commit", "--quiet", "--no-verify", "-m", title, "-m", body)

    def push(self, remote: str, branch: str) -> None:
        self.run("push", "--quiet", "--set-upstream", remote, branch)


class GitHub(Protocol):
    """Where pull requests are opened."""

    def problem(self, cwd: Path) -> str | None:
        """Why pull requests cannot be opened from ``cwd``, or None when they can."""
        ...

    def create_pull_request(
        self, *, cwd: Path, head: str, base: str | None, title: str, body: str
    ) -> str:
        """Open a pull request and return its URL."""
        ...


class GhCli:
    """Pull requests through the GitHub CLI, which already holds the user's credentials."""

    def problem(self, cwd: Path) -> str | None:
        if shutil.which("gh") is None:
            return "the GitHub CLI (gh) is not installed"
        if _call(["gh", "auth", "status"], cwd=cwd).returncode != 0:
            return "the GitHub CLI is not logged in (run `gh auth login`)"
        return None

    def create_pull_request(
        self, *, cwd: Path, head: str, base: str | None, title: str, body: str
    ) -> str:
        command = ["gh", "pr", "create", "--head", head, "--title", title, "--body-file", "-"]
        if base:
            command += ["--base", base]
        output = _checked(command, cwd=cwd, stdin=body)
        urls = [word for word in output.split() if word.startswith(("https://", "http://"))]
        return urls[-1] if urls else output.strip()
