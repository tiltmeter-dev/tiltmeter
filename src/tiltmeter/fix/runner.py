"""One fix attempt, from a fresh worktree to a pull request.

:class:`FixSession` checks out a new branch from HEAD into a temporary folder,
lets the caller run the agent there, stages its edits for review, and then either
commits, pushes and opens a pull request, or throws everything away. Closing the
session removes the worktree, and the branch too unless something was committed.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from tiltmeter.errors import FixError
from tiltmeter.fix.git import Git, GitHub
from tiltmeter.fix.ignore import is_ignored
from tiltmeter.fix.models import FixOutcome, FixPreview, FixReport, FixRequest, FixStatus

PROJECT_URL: Final = "https://github.com/tiltmeter-dev/tiltmeter"
DEFAULT_BRANCH_PREFIX: Final = "tiltmeter/fix"


def default_branch(request: FixRequest, prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", request.endpoint_id).strip("-").lower() or "api"
    return f"{prefix}-{slug}-{request.check_id}"


def commit_title(request: FixRequest) -> str:
    return f"Adapt to the {request.endpoint_id} API change"


def commit_body(request: FixRequest, report: FixReport) -> str:
    return (
        f"{report.summary}\n\n"
        f"Tiltmeter check #{request.check_id} found the change in "
        f"{request.method} {request.url}."
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def pull_request_body(request: FixRequest, preview: FixPreview, *, model_name: str) -> str:
    lines = [
        "## What changed in the API",
        "",
        f"Tiltmeter check #{request.check_id} found drift in `{request.endpoint_id}` "
        f"(`{request.method} {request.url}`).",
        "",
        "| Severity | Change | Path | Before | After |",
        "|---|---|---|---|---|",
    ]
    for target in request.targets:
        after = target.after or "(absent)"
        if target.renamed_to:
            after += f", probably renamed to `{target.renamed_to}`"
        lines.append(
            f"| {target.severity.value} | {target.kind.value} | `{_cell(target.path)}` "
            f"| {_cell(target.before or '(absent)')} | {_cell(after)} |"
        )
    if request.explanation is not None:
        lines += ["", request.explanation.summary]
    lines += ["", "## What this pull request changes", "", preview.report.summary, ""]
    lines += [f"- `{name}`" for name in preview.files]
    if preview.report.unresolved:
        lines += ["", "## Left for a human", ""]
        lines += [f"- {item}" for item in preview.report.unresolved]
    lines += [
        "",
        "---",
        f"Opened by [Tiltmeter]({PROJECT_URL}) with `tiltmeter fix {request.check_id}`. "
        f"An AI agent ({model_name}) wrote these edits; review them like any other change.",
    ]
    return "\n".join(lines) + "\n"


class FixSession:
    """A worktree on a new branch where the agent works, and what happens to its edits."""

    def __init__(
        self,
        repo: Git,
        worktree: Git,
        request: FixRequest,
        *,
        branch: str,
        base: str | None,
        workspace: Path,
    ) -> None:
        self.repo = repo
        self.worktree = worktree
        self.request = request
        self.branch = branch
        self.base = base
        self._workspace = workspace
        self._committed = False
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path,
        request: FixRequest,
        *,
        branch: str | None = None,
        prefix: str = DEFAULT_BRANCH_PREFIX,
        base: str | None = None,
    ) -> FixSession:
        """Check out a new branch from HEAD of the repository at ``path``."""
        repo = Git.open(path)
        name = branch or default_branch(request, prefix)
        if repo.branch_exists(name):
            raise FixError(f"branch {name} already exists; delete it or pass --branch")
        workspace = Path(tempfile.mkdtemp(prefix="tiltmeter-fix-"))
        try:
            worktree = repo.add_worktree(workspace / "repo", name)
        except FixError:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        return cls(
            repo,
            worktree,
            request,
            branch=name,
            base=base or repo.current_branch(),
            workspace=workspace,
        )

    @property
    def root(self) -> Path:
        """Where the agent works."""
        return self.worktree.root

    def review(self, report: FixReport, *, ignore: Sequence[str] = ()) -> FixPreview:
        """Stage everything the agent changed and describe it."""
        self.worktree.stage_all()
        files = self.worktree.staged_files()
        additions, deletions = self.worktree.staged_counts() if files else (0, 0)
        return FixPreview(
            branch=self.branch,
            base=self.base,
            files=files,
            additions=additions,
            deletions=deletions,
            diff=self.worktree.staged_diff() if files else "",
            report=report,
            ignored=[name for name in files if is_ignored(name, ignore)],
        )

    def outcome(self, status: FixStatus, preview: FixPreview | None, **fields: Any) -> FixOutcome:
        return FixOutcome(
            status=status,
            check_id=self.request.check_id,
            endpoint_id=self.request.endpoint_id,
            branch=self.branch if self._committed else None,
            base=self.base,
            preview=preview,
            **fields,
        )

    def deliver(
        self, preview: FixPreview, *, push: bool, github: GitHub | None, model_name: str
    ) -> FixOutcome:
        """Commit the reviewed edits, then push and open a pull request when asked to.

        Every step after the commit degrades instead of raising: the outcome says how
        far it got, why it stopped and what is left to run by hand.
        """
        if preview.empty:
            raise FixError("there is nothing to commit")
        if preview.ignored:
            raise FixError(f"refusing to commit ignored files: {', '.join(preview.ignored)}")
        self.worktree.commit(commit_title(self.request), commit_body(self.request, preview.report))
        self._committed = True
        remote = self.repo.remote()
        open_pr = f"gh pr create --head {self.branch} --fill" + (
            f" --base {self.base}" if self.base else ""
        )
        push_branch = f"git push --set-upstream {remote or 'origin'} {self.branch}"
        if not push:
            return self.outcome("committed", preview, commands=[push_branch, open_pr])
        if remote is None:
            error = "the repository has no remote to push to"
            return self.outcome("committed", preview, error=error, commands=[push_branch, open_pr])
        try:
            # From the main checkout: relative remote URLs resolve against it.
            self.repo.push(remote, self.branch)
        except FixError as exc:
            return self.outcome(
                "committed", preview, error=str(exc), commands=[push_branch, open_pr]
            )
        problem = github.problem(self.repo.root) if github is not None else None
        if github is None or problem is not None:
            return self.outcome(
                "pushed", preview, commands=[open_pr], notes=[problem] if problem else []
            )
        try:
            url = github.create_pull_request(
                cwd=self.repo.root,
                head=self.branch,
                base=self.base,
                title=commit_title(self.request),
                body=pull_request_body(self.request, preview, model_name=model_name),
            )
        except FixError as exc:
            return self.outcome("pushed", preview, error=str(exc), commands=[open_pr])
        return self.outcome("opened", preview, pr_url=url)

    def close(self) -> None:
        """Remove the worktree, and the branch unless it holds a commit."""
        if self._closed:
            return
        self._closed = True
        self.repo.remove_worktree(self.root)
        shutil.rmtree(self._workspace, ignore_errors=True)
        if not self._committed:
            self.repo.delete_branch(self.branch)

    def __enter__(self) -> FixSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
