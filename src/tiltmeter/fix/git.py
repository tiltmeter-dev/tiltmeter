"""Git and GitHub, through their own command-line tools.

The fix happens in a separate worktree, on a new branch created from the current
HEAD in a temporary folder. The user's checkout, index and uncommitted changes are
never touched. Commands run without a shell and never prompt for credentials.

No git hook runs, in the checkout or in the worktree. A hook is a program git starts
with the user's environment and network, and the agent or the tests it wrote can leave
one behind: in a hooks folder the repository keeps under version control, or in
``.git/hooks`` itself. ``--no-verify`` would skip only two of them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

from tiltmeter.errors import FixError
from tiltmeter.fix._proc import failure_reason, run_tool
from tiltmeter.fix.models import FailedCheck, PullFeedback, ReviewNote

# Options for every git command: no folder to look for hooks in, and no file-system
# monitor, which is a hook under another name.
NO_HOOKS: Final = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def _call(
    command: Sequence[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git or gh with the user's environment plus ``env``, and never a prompt."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", **(env or {})}
    if command[0] == "git":
        command = ["git", *NO_HOOKS, *command[1:]]
    return run_tool(command, cwd=cwd, env=env, stdin=stdin, timeout=timeout)


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

    @classmethod
    def clone(cls, url: str, into: Path, *, timeout: float | None = None) -> Git:
        """A copy of ``url`` in ``into`` to read and nothing more: the latest commit of its
        default branch, without tags. Git LFS files stay pointers, since the filter that
        downloads them is a program git would start like a hook."""
        command = ["git", "clone", "--quiet", "--depth", "1", "--no-tags", "--single-branch"]
        completed = _call(
            [*command, "--", url, str(into)],
            cwd=into.parent,
            env={"GIT_LFS_SKIP_SMUDGE": "1"},
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise FixError(f"`git clone` failed: {failure_reason(completed)}")
        return cls(into)

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

    def recent_titles(self, ref: str | None = None, count: int = 15) -> list[str]:
        """The titles of the latest commits on ``ref`` (HEAD by default), merges left out;
        empty when git cannot read them."""
        completed = _call(
            ["git", "log", "--no-merges", f"-n{count}", "--format=%s", ref or "HEAD", "--"],
            cwd=self.root,
        )
        if completed.returncode != 0:
            return []
        return [line.strip()[:200] for line in completed.stdout.splitlines() if line.strip()]

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

    def remote_of(self, repository: str) -> str | None:
        """The remote whose URL is the GitHub repository ``owner/name``, if there is one.
        The URL is the one written in the configuration, before any ``insteadOf``."""
        wanted = repository.lower().removesuffix(".git")
        for remote in self.run("remote").split():
            completed = _call(["git", "config", "--get-all", f"remote.{remote}.url"], cwd=self.root)
            for url in completed.stdout.lower().split():
                found = _GITHUB_REPOSITORY.search(url)
                if found and found[1].removesuffix(".git") == wanted:
                    return remote
        return None

    def fetch(self, remote: str, branch: str) -> str:
        """Fetch ``branch`` from ``remote`` and return the commit it points to."""
        self.run("fetch", "--quiet", "--no-tags", remote, f"refs/heads/{branch}")
        return self.run("rev-parse", "FETCH_HEAD").strip()

    def remote_branch_exists(self, remote: str, name: str) -> bool:
        """Whether ``remote`` already has the branch; False when it cannot be asked."""
        completed = _call(
            ["git", "ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{name}"],
            cwd=self.root,
        )
        return completed.returncode == 0

    def tracked_files(self) -> list[str]:
        """Every file in the index, as repository-relative POSIX paths."""
        return [name for name in self.run("ls-files", "-z").split("\0") if name]

    def last_changed(self, path: str) -> str | None:
        """When a commit on HEAD last changed ``path``, in ISO 8601; None when none did.
        In a shallow clone, the oldest commit there stands in for older ones."""
        completed = _call(
            ["git", "--literal-pathspecs", "log", "-1", "--format=%cI", "HEAD", "--", path],
            cwd=self.root,
        )
        stamp = completed.stdout.strip() if completed.returncode == 0 else ""
        if not stamp:
            return None
        # Newer git spells UTC as "Z" in %cI, older git as "+00:00"; keep one form.
        try:
            return datetime.fromisoformat(stamp).isoformat()
        except ValueError:
            return stamp

    # -- worktrees ------------------------------------------------------------

    def add_worktree(self, path: Path, branch: str, start: str = "HEAD") -> Git:
        """Check out a new ``branch`` from ``start`` (HEAD by default) into ``path``."""
        self.run("worktree", "add", "--quiet", "-b", branch, str(path), start)
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
        # Local hooks are skipped like any bot commit (see NO_HOOKS); CI checks the pull request.
        self.run("commit", "--quiet", "--no-verify", "-m", title, "-m", body)

    def push(self, remote: str, branch: str) -> None:
        self.run("push", "--quiet", "--set-upstream", remote, branch)

    def push_to(self, remote: str, branch: str, target: str) -> None:
        """Push ``branch`` onto the remote branch ``target``. Never forced: a remote that
        moved on in the meantime refuses it."""
        self.run("push", "--quiet", remote, f"refs/heads/{branch}:refs/heads/{target}")


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

    def find_pull_request(self, *, cwd: Path, head: str) -> str | None:
        """The URL of a pull request from ``head``, open or not, if there is one."""
        ...

    def feedback(self, *, cwd: Path, pull: str) -> PullFeedback:
        """What reviewers and checks said about the open pull request ``pull``, a number
        or a URL."""
        ...

    def comment(self, *, cwd: Path, pull: str, body: str) -> None:
        """Comment on a pull request."""
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
        return _pull_request_url(_checked(command, cwd=cwd, stdin=body))

    def find_pull_request(self, *, cwd: Path, head: str) -> str | None:
        command = ["gh", "pr", "list", "--head", head, "--state", "all", "--json", "url"]
        try:
            found = json.loads(_checked(command, cwd=cwd) or "[]")
        except ValueError as exc:
            raise FixError(f"`gh pr list` answered something unexpected: {exc}") from exc
        urls = [item.get("url") for item in found if isinstance(item, dict)]
        return next((url for url in urls if isinstance(url, str)), None)

    def feedback(self, *, cwd: Path, pull: str) -> PullFeedback:
        viewed = self._json(["gh", "pr", "view", pull, "--json", _PULL_FIELDS], cwd, "pr view")
        if not isinstance(viewed, dict):
            raise FixError("`gh pr view` answered something unexpected")
        if viewed.get("state") != "OPEN":
            state = str(viewed.get("state") or "not open").lower()
            raise FixError(f"pull request #{viewed.get('number')} is {state}: nothing to answer")
        found = _PULL_URL.fullmatch(str(viewed.get("url") or ""))
        if found is None:
            raise FixError("`gh pr view` gave no pull request URL")
        owner, name, number = found[1], found[2], found[3]
        query = ["gh", "api", "graphql", "-f", f"query={_THREADS}"]
        query += ["-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
        try:
            threads = self._json(query, cwd, "api graphql")
        except FixError:
            threads = {}  # notes on lines are a bonus; reviews and comments are enough
        logs = {
            check: self._failed_log(check, f"{owner}/{name}", cwd) for check in failed_runs(viewed)
        }
        return parse_feedback(viewed, threads, logs)

    def comment(self, *, cwd: Path, pull: str, body: str) -> None:
        _checked(["gh", "pr", "comment", pull, "--body-file", "-"], cwd=cwd, stdin=body)

    def _json(self, command: Sequence[str], cwd: Path, what: str) -> Any:
        try:
            return json.loads(_checked(command, cwd=cwd) or "null")
        except ValueError as exc:
            raise FixError(f"`gh {what}` answered something unexpected: {exc}") from exc

    def _failed_log(self, details_url: str, repository: str, cwd: Path) -> str:
        """The end of a failed GitHub Actions job's log, or nothing when it cannot be read."""
        job = _ACTIONS_JOB.search(details_url)
        if job is None:
            return ""
        command = ["gh", "run", "view", "--repo", repository, "--job", job[1], "--log-failed"]
        completed = _call(command, cwd=cwd)
        return completed.stdout[-MAX_LOG:] if completed.returncode == 0 else ""


# A review can be long, and anyone may write one: what reaches the agent is bounded.
MAX_NOTES: Final = 40
MAX_NOTE: Final = 2_000
MAX_FAILED_CHECKS: Final = 3
MAX_LOG: Final = 3_000
_PULL_FIELDS: Final = (
    "number,url,title,state,headRefName,baseRefName,headRepository,headRepositoryOwner,"
    "reviews,comments,statusCheckRollup"
)
_PULL_URL: Final = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
_GITHUB_REPOSITORY: Final = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)/?$")
_ACTIONS_JOB: Final = re.compile(r"/actions/runs/\d+/job/(\d+)")
_FAILED: Final = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"})
_THREADS: Final = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved isOutdated path line "
    "originalLine comments(first:20){nodes{author{login} authorAssociation body "
    "viewerDidAuthor}}}}}}}"
)


def _failed_checks(viewed: dict[str, Any]) -> list[tuple[str, str]]:
    """The name and details URL of each check that failed, each name once."""
    found: dict[str, str] = {}
    for check in viewed.get("statusCheckRollup") or []:
        if not isinstance(check, dict):
            continue
        verdict = str(check.get("conclusion") or check.get("state") or "").upper()
        name = str(check.get("name") or check.get("context") or "")
        if name and verdict in _FAILED:
            found.setdefault(name, str(check.get("detailsUrl") or check.get("targetUrl") or ""))
    return list(found.items())[:MAX_FAILED_CHECKS]


def failed_runs(viewed: dict[str, Any]) -> list[str]:
    """The details URLs of the failed checks whose logs are worth asking for."""
    return [url for _, url in _failed_checks(viewed) if url]


def _note(
    kind: str, item: dict[str, Any], *, path: str | None = None, line: Any = None
) -> ReviewNote | None:
    """One thing someone wrote, unless it is empty, hidden or the viewer's own."""
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    login = str((author or {}).get("login") or "")
    body = str(item.get("body") or "").strip()
    if not login or not body or item.get("viewerDidAuthor") or item.get("isMinimized"):
        return None
    bot = login.endswith("[bot]") or bool((author or {}).get("is_bot"))
    state = str(item.get("state") or "").replace("_", " ").lower() if kind == "review" else None
    return ReviewNote(
        author=login,
        role="bot" if bot else str(item.get("authorAssociation") or "none").lower(),
        kind="review" if kind == "review" else "line" if kind == "line" else "comment",
        body=body[:MAX_NOTE],
        verdict=state or None,
        path=path,
        line=line if isinstance(line, int) else None,
    )


def parse_feedback(
    viewed: dict[str, Any], threads: Any, logs: dict[str, str] | None = None
) -> PullFeedback:
    """Turn what ``gh pr view --json`` and the review-threads query answered into feedback.

    Left out: what the person running Tiltmeter wrote, hidden comments, reviews that
    approve without a word, and threads that are resolved. The latest notes are kept when
    there are too many. ``logs`` maps a failed check's details URL to the end of its log.
    """
    notes: list[ReviewNote] = []
    for review in viewed.get("reviews") or []:
        if isinstance(review, dict) and (note := _note("review", review)) is not None:
            notes.append(note)
    for comment in viewed.get("comments") or []:
        if isinstance(comment, dict) and (note := _note("comment", comment)) is not None:
            notes.append(note)
    try:
        nodes = threads["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        nodes = []
    for thread in nodes or []:
        if not isinstance(thread, dict) or thread.get("isResolved"):
            continue
        line = thread.get("line") or thread.get("originalLine")
        for comment in (thread.get("comments") or {}).get("nodes") or []:
            if isinstance(comment, dict):
                note = _note("line", comment, path=str(thread.get("path") or ""), line=line)
                if note is not None:
                    notes.append(note)
    owner = (viewed.get("headRepositoryOwner") or {}).get("login") or ""
    name = (viewed.get("headRepository") or {}).get("name") or ""
    return PullFeedback(
        number=int(viewed.get("number") or 0),
        url=str(viewed.get("url") or ""),
        title=str(viewed.get("title") or ""),
        repository=f"{owner}/{name}",
        branch=str(viewed.get("headRefName") or ""),
        base=str(viewed.get("baseRefName") or ""),
        notes=notes[-MAX_NOTES:],
        failed_checks=[
            FailedCheck(name=name, log=(logs or {}).get(url, ""))
            for name, url in _failed_checks(viewed)
        ],
    )


def _pull_request_url(output: str) -> str:
    urls = [word for word in output.split() if word.startswith(("https://", "http://"))]
    return urls[-1] if urls else output.strip()
