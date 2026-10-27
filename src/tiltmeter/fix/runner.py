"""One fix attempt, from a fresh worktree to a pull request.

:class:`FixSession` checks out a new branch from HEAD into a temporary folder,
lets the caller run the agent there, stages its edits for review, and then either
commits, pushes and opens a pull request, or throws everything away. Closing the
session removes the worktree, and the branch too unless something was committed.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import textwrap
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from urllib.parse import urlsplit

from tiltmeter.core.models import ChangeKind
from tiltmeter.errors import FixError
from tiltmeter.fix.git import Git, GitHub
from tiltmeter.fix.ignore import is_ignored
from tiltmeter.fix.models import (
    FixApi,
    FixOutcome,
    FixPreview,
    FixReport,
    FixRequest,
    FixStatus,
    FixTarget,
    NoticeSource,
    TestRun,
    UnusedFile,
)
from tiltmeter.fix.usage import STALE_DAYS, likely_dead, unused_files
from tiltmeter.notices.models import SpecInfo

PROJECT_URL: Final = "https://github.com/tiltmeter-dev/tiltmeter"
DEFAULT_BRANCH_PREFIX: Final = "tiltmeter/fix"
MAX_SLUG: Final = 80
PLAIN_WIDTH: Final = 100
# Rows of change tables in one pull request; GitHub refuses bodies past 65,536 characters.
MAX_CHANGES_IN_BODY: Final = 100
HELD_BACK: Final = (
    "the edits touch only code that nothing else in the repository imports or names, "
    f"unchanged for over {STALE_DAYS} days, so nothing was pushed; pass --allow-unused to "
    "open the pull request anyway"
)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def default_branch(request: FixRequest, prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    if request.notice is not None:
        notice = request.notice
        slug = _slug(f"{notice.provider}-{notice.id}")[:MAX_SLUG].strip("-")
        # Different notices can share a slug; the digest keeps their branches apart.
        digest = hashlib.sha256(f"{notice.provider}\0{notice.id}".encode()).hexdigest()[:8]
        return f"{prefix}-{slug}-{digest}"
    slug = _slug(request.subject) or "api"
    return f"{prefix}-{slug}-{request.check_id}"


MAX_TITLE_LENGTH: Final = 100


def commit_title(request: FixRequest, report: FixReport | None = None) -> str:
    """The agent's title in the repository's convention when it gave a usable one,
    otherwise Tiltmeter's own."""
    proposed = _proposed_title(report)
    if proposed is not None:
        return proposed
    retired = _retired_title(request)
    if retired is not None:
        return retired
    title = f"Adapt to the {request.subject} API change"
    notice = request.notice
    if notice is not None and notice.spec is not None and notice.spec.new_version:
        title += f" in spec {notice.spec.new_version}"
    elif notice is not None and notice.effective_at:
        title += f" effective {notice.effective_at[:10]}"
    return title


def _proposed_title(report: FixReport | None) -> str | None:
    """The agent's commit_title, unless it is too short, too long or could link, mention or
    format something: the agent wrote it from untrusted repository text."""
    if report is None or report.commit_title is None:
        return None
    title = " ".join(report.commit_title.split())
    if not 8 <= len(title) <= MAX_TITLE_LENGTH:
        return None
    if any(mark in title for mark in ("@", "`", "<", ">", "](", "://")):
        return None
    return title


def _retired_title(request: FixRequest) -> str | None:
    """ "Replace the models api.anthropic.com retired", when values are all that changed."""
    targets = request.targets
    if not targets or any(t.kind is not ChangeKind.VALUE_RETIRED for t in targets):
        return None
    names = {segment for t in targets if (segment := t.path.rsplit(".", 1)[-1]).isidentifier()}
    if len(names) != 1:
        return f"Replace the values {request.subject} retired"
    name = names.pop()
    plural = name[:-1] + "ies" if name.endswith("y") else name if name.endswith("s") else name + "s"
    return f"Replace the {plural} {request.subject} retired"


def commit_body(request: FixRequest, report: FixReport) -> str:
    if request.notice is not None and request.notice.spec is not None:
        found = (
            f"Tiltmeter found the change by comparing two versions of "
            f"{request.notice.provider}'s OpenAPI spec ({request.notice.spec.url})."
        )
    elif request.notice is not None and request.notice.key is not None:
        found = f"{request.notice.provider} announced the change in notice {request.notice.id}."
    elif request.notice is not None:
        found = (
            f"Notice {request.notice.id} about {request.notice.provider}'s API describes the "
            "change. It was read from a file and is not signed by the provider."
        )
    else:
        api = request.apis[0]
        found = f"Tiltmeter check #{request.check_id} found the change in {api.method} {api.url}."
    return f"{report.summary}\n\n{found}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _code(text: str) -> str:
    """Third-party text as inline code, which renders no links, mentions or HTML."""
    return f"`{_cell(text).replace('`', "'")}`"


def _plain_text(lines: Sequence[str]) -> list[str]:
    """Third-party text as a plain code block: no links, mentions, images or HTML."""
    body = [textwrap.fill(line.replace("`", "'"), PLAIN_WIDTH) for line in lines]
    return ["```text", *body, "```"]


def _changes_table(
    api: FixApi,
    shown: Sequence[FixTarget] | None = None,
    *,
    announced: bool,
    guessed: bool = False,
) -> list[str]:
    """The changes of one API, or the ``shown`` ones. A notice may leave a type out: that
    is "—", not absent.

    A notice's values are the provider's words, so they are shown as inline code.
    """
    unknown = "—" if announced else "(absent)"
    value = _code if announced else _cell
    targets = api.targets if shown is None else shown
    if not targets:
        return [f"{len(api.targets)} changes, not listed."]
    lines = ["| Severity | Change | Path | Before | After |", "|---|---|---|---|---|"]
    sure = announced and not guessed
    for target in targets:
        removed = target.kind is ChangeKind.FIELD_REMOVED
        before = value(target.before) if target.before else unknown
        after = value(target.after) if target.after else ("(absent)" if removed else unknown)
        if target.kind is ChangeKind.ENDPOINT_REMOVED:
            after = "(gone)"
        elif target.kind is ChangeKind.VALUE_RETIRED:
            after = "(no longer accepted)"
        if target.renamed_to:
            renamed = "renamed to" if sure else "probably renamed to"
            after += f", {renamed} {_code(target.renamed_to)}"
        if target.moved_to:
            moved = "moved to" if sure else "probably moved to"
            after += f", {moved} {_code(target.moved_to)}"
        if target.replaced_by:
            after += f", send {_code(target.replaced_by)} instead"
        lines.append(
            f"| {target.severity.value} | {target.kind.value} | {_code(target.path)} "
            f"| {before} | {after} |"
        )
    if len(targets) < len(api.targets):
        lines += ["", f"And {len(api.targets) - len(targets)} more changes, not listed."]
    return lines


def _notice_section(request: FixRequest, notice: NoticeSource) -> list[str]:
    when = f"published {notice.published_at[:10]}"
    if notice.effective_at:
        when += f" and effective {notice.effective_at[:10]}"
    if notice.key:
        # Signed by the provider: these are its words.
        source = (
            f"`{notice.provider}` announced this change in notice `{notice.id}`, {when}. "
            f"Its signature was verified against `{notice.key}`."
        )
        quoted, noted = "The provider's summary, quoted as they wrote it:", "The provider's notes:"
    else:
        # Anyone may write an unsigned notice: never put it in the provider's mouth.
        source = (
            f"Notice `{notice.id}` about `{notice.provider}`'s API describes this change, "
            f"{when}. It is not signed by `{notice.provider}`: it was read from a file the "
            "person running Tiltmeter trusts."
        )
        quoted, noted = "The notice's summary:", "The notice's notes:"
    lines = [
        "## What changed in the API",
        "",
        source,
        "",
        quoted,
        "",
        *_plain_text([notice.summary]),
    ]
    for api, shown in zip(request.apis, request.shown(MAX_CHANGES_IN_BODY), strict=True):
        lines += ["", f"**{_code(f'{api.method} {api.url}')}**", ""]
        lines += _changes_table(api, shown, announced=True)
    # A note on a whole endpoint names it; the same note on several endpoints shows once.
    notes = list(
        dict.fromkeys(
            f"{_where(api, target)}: {target.note}"
            for api in request.apis
            for target in api.targets
            if target.note
        )
    )
    if notes:
        lines += ["", noted, "", *_plain_text(notes)]
    if notice.sdks:
        releases = ", ".join(f"{s.ecosystem} `{s.name}` {s.fixed_in}" for s in notice.sdks)
        lines += ["", f"SDK releases that already follow the change: {releases}."]
    return lines


def _where(api: FixApi, target: FixTarget) -> str:
    if target.kind is ChangeKind.VALUE_RETIRED and target.before:
        return target.before
    return f"{api.method} {urlsplit(api.url).path}" if target.path == "$" else target.path


def _spec_section(request: FixRequest, notice: NoticeSource, spec: SpecInfo) -> list[str]:
    versions = f"{spec.old_version or '?'} to {spec.new_version or '?'}"
    where = f"at {_code(spec.url)}"
    if spec.baseline_url:
        where = f"at {_code(spec.baseline_url)} and {_code(spec.url)}"
    lines = [
        "## What changed in the API",
        "",
        f"Tiltmeter compared two versions of the OpenAPI spec `{notice.provider}` publishes "
        f"{where}: {_code(versions)}, fetched {spec.old_fetched_at[:10]} and "
        f"{spec.new_fetched_at[:10]}. The provider did not announce this change: a spec can "
        "run ahead of production or lag behind it, and renames are guessed from similar "
        "names.",
    ]
    if spec.new_api_version:
        lines += [
            "",
            f"This is a new API version. Clients that pin {_code(spec.old_version or '?')} keep "
            "their responses until they upgrade.",
        ]
    for api, shown in zip(request.apis, request.shown(MAX_CHANGES_IN_BODY), strict=True):
        lines += ["", f"**{_code(f'{api.method} {api.url}')}**", ""]
        lines += _changes_table(api, shown, announced=True, guessed=True)
    if spec.omitted_endpoints:
        lines += [
            "",
            f"{spec.omitted_endpoints} other changed endpoints are left out: nothing here "
            "seems to use them.",
        ]
    return lines


def _check_section(request: FixRequest) -> list[str]:
    api = request.apis[0]
    lines = [
        "## What changed in the API",
        "",
        f"Tiltmeter check #{request.check_id} found drift in `{api.endpoint_id}` "
        f"(`{api.method} {api.url}`).",
        "",
        *_changes_table(api, request.shown(MAX_CHANGES_IN_BODY)[0], announced=False),
    ]
    if request.explanation is not None:
        lines += ["", request.explanation.summary]
    return lines


def _tests_section(tests: TestRun) -> list[str]:
    command = _code(tests.command)
    if tests.status == "skipped":
        return [f"{command} did not run: {tests.reason}."]
    again = " after the agent saw them fail and edited again" if tests.rounds > 1 else ""
    if tests.status == "passed":
        how = "without network access" if tests.offline else "with network access"
        return [f"{command} passed on these edits{again}. It ran {how} and without secrets."]
    tail = tests.output.strip().splitlines()[-30:] or ["(no output)"]
    return [f"{command} fails on these edits. The end of its output:", "", *_plain_text(tail)]


def _unused_line(unused: Sequence[UnusedFile]) -> str:
    named = ", ".join(
        f"{_code(item.path)} (last changed {item.last_changed[:10]})" for item in unused
    )
    return f"Possibly unused: nothing else in the repository imports or names {named}."


def pull_request_body(request: FixRequest, preview: FixPreview, *, model_name: str) -> str:
    notice = request.notice
    if notice is not None and notice.spec is not None:
        lines = _spec_section(request, notice, notice.spec)
    elif notice is not None:
        lines = _notice_section(request, notice)
    else:
        lines = _check_section(request)
    lines += ["", "## What this pull request changes", "", preview.report.summary, ""]
    lines += [f"- `{name}`" for name in preview.files]
    if preview.unused:
        lines += ["", _unused_line(preview.unused)]
    if preview.tests is not None:
        lines += ["", "## Tests", "", *_tests_section(preview.tests)]
    unresolved = list(preview.report.unresolved)
    if preview.tests is not None and preview.tests.status == "failed":
        unresolved.insert(0, "The tests still fail with these edits: see Tests above.")
    lines += ["", "## Left for a human", ""]
    if unresolved:
        lines += [f"- {item}" for item in unresolved]
    else:
        lines.append("Nothing: the agent flagged nothing it could not decide.")
    if notice is not None and notice.spec is not None:
        origin = f"from a diff of {notice.provider}'s OpenAPI spec"
    elif notice is not None and notice.key:
        origin = f"from {notice.provider}'s change notice `{notice.id}`"
    elif notice is not None:
        origin = f"from the unsigned notice `{notice.id}` about {notice.provider}'s API"
    else:
        origin = f"with `tiltmeter fix {request.check_id}`"
    lines += [
        "",
        "---",
        f"This pull request was generated automatically by [Tiltmeter]({PROJECT_URL}) {origin}. "
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

    @property
    def scratch(self) -> Path:
        """A folder of the session's own, outside the worktree, removed with it."""
        return self._workspace

    def review(
        self, report: FixReport, *, ignore: Sequence[str] = (), tests: TestRun | None = None
    ) -> FixPreview:
        """Stage everything the agent changed and describe it, with the changed files that
        nothing seems to use."""
        self.worktree.stage_all()
        files = self.worktree.staged_files()
        additions, deletions = self.worktree.staged_counts() if files else (0, 0)
        unused = unused_files(self.worktree, files, ignore=ignore) if files else []
        return FixPreview(
            branch=self.branch,
            base=self.base,
            files=files,
            additions=additions,
            deletions=deletions,
            diff=self.worktree.staged_diff() if files else "",
            report=report,
            ignored=[name for name in files if is_ignored(name, ignore)],
            tests=tests,
            unused=unused,
            likely_dead=likely_dead(files, unused),
        )

    def outcome(self, status: FixStatus, preview: FixPreview | None, **fields: Any) -> FixOutcome:
        notice = self.request.notice
        return FixOutcome(
            status=status,
            check_id=self.request.check_id,
            endpoint_id=self.request.apis[0].endpoint_id if self.request.apis else None,
            notice_id=notice.id if notice else None,
            provider=notice.provider if notice else None,
            branch=self.branch if self._committed else None,
            base=self.base,
            preview=preview,
            **fields,
        )

    def deliver(
        self,
        preview: FixPreview,
        *,
        push: bool,
        github: GitHub | None,
        model_name: str,
        allow_unused: bool = False,
    ) -> FixOutcome:
        """Commit the reviewed edits, then push and open a pull request when asked to.

        Edits that touch only code that looks unused and old stay in the local commit,
        unless ``allow_unused``. Every step after the commit degrades instead of raising:
        the outcome says how far it got, why it stopped and what is left to run by hand.
        """
        if preview.empty:
            raise FixError("there is nothing to commit")
        if preview.ignored:
            raise FixError(f"refusing to commit ignored files: {', '.join(preview.ignored)}")
        title = commit_title(self.request, preview.report)
        self.worktree.commit(title, commit_body(self.request, preview.report))
        self._committed = True
        remote = self.repo.remote()
        open_pr = f"gh pr create --head {self.branch} --fill" + (
            f" --base {self.base}" if self.base else ""
        )
        push_branch = f"git push --set-upstream {remote or 'origin'} {self.branch}"
        if not push:
            return self.outcome("committed", preview, commands=[push_branch, open_pr])
        if preview.likely_dead and not allow_unused:
            return self.outcome(
                "committed",
                preview,
                commands=[push_branch, open_pr],
                notes=[HELD_BACK],
                held_back=True,
            )
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
                title=title,
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
