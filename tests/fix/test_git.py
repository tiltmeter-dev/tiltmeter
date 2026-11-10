from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.fix.kit import git, make_repo
from tiltmeter.errors import FixError
from tiltmeter.fix.git import GhCli, Git, failed_runs, parse_feedback


def test_open_finds_the_repository_root(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / "src").mkdir()
    opened = Git.open(repo / "src")
    assert opened.root.resolve() == repo.resolve()
    assert opened.current_branch() == "main"
    assert opened.remote() == "origin"
    assert len(opened.head()) >= 7
    assert not opened.has_uncommitted_changes()
    (repo / "client.py").write_text("changed\n", encoding="utf-8")
    assert opened.has_uncommitted_changes()


def test_open_rejects_folders_that_cannot_be_fixed(tmp_path: Path) -> None:
    with pytest.raises(FixError, match="not a directory"):
        Git.open(tmp_path / "missing")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(FixError, match="not inside a git repository"):
        Git.open(plain)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "--quiet")
    with pytest.raises(FixError, match="no commits yet"):
        Git.open(empty)


def test_worktree_stage_commit_and_push(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    main = Git.open(repo)
    worktree = main.add_worktree(tmp_path / "wt", "tiltmeter/fix-x-1")
    assert main.branch_exists("tiltmeter/fix-x-1")

    (worktree.root / "client.py").write_text("print('new')\n", encoding="utf-8")
    (worktree.root / "notes" / "new.txt").parent.mkdir()
    (worktree.root / "notes" / "new.txt").write_text("hello\n", encoding="utf-8")
    (worktree.root / "models.py").unlink()
    worktree.stage_all()
    assert sorted(worktree.staged_files()) == ["client.py", "models.py", "notes/new.txt"]
    additions, deletions = worktree.staged_counts()
    assert additions == 2
    assert deletions > 2
    assert "+print('new')" in worktree.staged_diff()

    worktree.commit("Adapt to the x API change", "Body line.")
    worktree.push("origin", "tiltmeter/fix-x-1")
    log = git(remote, "log", "-1", "--format=%an <%ae>%n%B", "tiltmeter/fix-x-1")
    assert log.startswith("Ada Lovelace <ada@example.com>\nAdapt to the x API change\n\nBody line.")
    assert "Co-Authored-By" not in log

    assert not main.has_uncommitted_changes()  # the user's checkout never moved
    assert main.current_branch() == "main"
    main.remove_worktree(worktree.root)
    assert not worktree.root.exists()
    main.delete_branch("tiltmeter/fix-x-1")
    assert not main.branch_exists("tiltmeter/fix-x-1")


HOOKS = ("post-checkout", "prepare-commit-msg", "post-commit", "pre-push", "reference-transaction")


def plant_hooks(folder: Path, marker: Path) -> None:
    """Hooks that add a line to ``marker`` whenever git runs them."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in HOOKS:
        hook = folder / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n", encoding="utf-8")
        hook.chmod(0o755)


def fix_and_push(repo: Path, tmp_path: Path, edits: dict[str, str]) -> None:
    """What a fix does with git: a worktree, the edits, a restore, a commit, a push, a cleanup."""
    main = Git.open(repo)
    worktree = main.add_worktree(tmp_path / "wt", "tiltmeter/fix-x-1")
    for name, text in edits.items():
        (worktree.root / name).write_text(text, encoding="utf-8")
    worktree.stage_all()
    worktree.run("checkout", "--", ".")
    worktree.commit("Adapt to the x API change", "Body line.")
    worktree.push("origin", "tiltmeter/fix-x-1")
    main.remove_worktree(worktree.root)
    main.delete_branch("tiltmeter/fix-x-1")


def test_no_git_hook_runs(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    marker = tmp_path / "hooks-ran.txt"
    plant_hooks(repo / ".git" / "hooks", marker)
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "Run the hooks once")
    assert "post-commit" in marker.read_text(encoding="utf-8")  # plain git does run them
    marker.unlink()

    fix_and_push(repo, tmp_path, {"client.py": "print('new')\n"})
    assert git(remote, "log", "-1", "--format=%s", "tiltmeter/fix-x-1").strip() == (
        "Adapt to the x API change"
    )
    assert not marker.exists()


def test_hooks_the_repository_keeps_do_not_run_either(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    marker = tmp_path / "hooks-ran.txt"
    plant_hooks(repo / ".githooks", marker)
    git(repo, "config", "core.hooksPath", ".githooks")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "Keep the hooks in the repository")
    assert "post-commit" in marker.read_text(encoding="utf-8")
    marker.unlink()

    # The agent can edit a hook the repository tracks: git must not run it on the commit.
    edited = f"#!/bin/sh\necho edited >> '{marker.as_posix()}'\n"
    fix_and_push(repo, tmp_path, {".githooks/post-commit": edited})
    assert not marker.exists()


def test_failures_carry_git_s_reason(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    with pytest.raises(FixError, match=r"`git push --quiet` failed: .*nowhere"):
        Git.open(repo).push("nowhere", "main")


def test_gh_problem_when_the_cli_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tiltmeter.fix.git.shutil.which", lambda _: None)
    assert GhCli().problem(tmp_path) == "the GitHub CLI (gh) is not installed"


@pytest.mark.skipif(sys.platform == "win32", reason="a script on PATH needs a POSIX shell")
def test_pull_requests_of_pushed_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = tmp_path / "calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['pr', 'list']:\n"
        "    head = sys.argv[sys.argv.index('--head') + 1]\n"
        "    found = [{'url': 'https://github.test/pr/3'}] if head == 'known' else []\n"
        "    print(json.dumps(found))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    gh = GhCli()
    assert gh.find_pull_request(cwd=tmp_path, head="known") == "https://github.test/pr/3"
    assert gh.find_pull_request(cwd=tmp_path, head="fresh") is None
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "pr list --head known --state all --json url",
        "pr list --head fresh --state all --json url",
    ]


def test_recent_titles_leave_out_merges(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    git(repo, "checkout", "--quiet", "-b", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    git(repo, "add", "side.txt")
    git(repo, "commit", "--quiet", "-m", "fix(side): add a side file")
    git(repo, "checkout", "--quiet", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    git(repo, "add", "main.txt")
    git(repo, "commit", "--quiet", "-m", "docs: note the main file")
    git(repo, "merge", "--quiet", "--no-ff", "-m", "Merge branch 'side'", "side")
    main = Git.open(repo)
    assert main.recent_titles() == [
        "docs: note the main file",
        "fix(side): add a side file",
        "Initial commit",
    ]
    assert main.recent_titles("side", count=1) == ["fix(side): add a side file"]
    assert main.recent_titles("no-such-branch") == []


# -- answering a review ----------------------------------------------------------------


def viewed(**overrides: object) -> dict[str, object]:
    """What `gh pr view --json` answers, in the shapes GitHub uses."""
    data: dict[str, object] = {
        "number": 7,
        "url": "https://github.com/pbek/QOwnNotes/pull/7",
        "title": "Replace the models api.groq.com retired",
        "state": "OPEN",
        "headRefName": "fix/groq-retired-models",
        "baseRefName": "main",
        "headRepository": {"name": "QOwnNotes", "nameWithOwner": ""},
        "headRepositoryOwner": {"login": "tiltmeter-dev"},
        "reviews": [
            {
                "author": {"login": "pbek"},
                "authorAssociation": "OWNER",
                "state": "CHANGES_REQUESTED",
                "body": "Please follow the commit message setting.",
            },
            {"author": {"login": "pbek"}, "authorAssociation": "OWNER", "state": "APPROVED"},
        ],
        "comments": [
            {
                "author": {"login": "torrresagus"},
                "authorAssociation": "CONTRIBUTOR",
                "body": "Thanks, pushed an update.",
                "viewerDidAuthor": True,
            },
            {
                "author": {"login": "spammer"},
                "authorAssociation": "NONE",
                "body": "hidden as spam",
                "isMinimized": True,
            },
            {
                "author": {"login": "review-bot[bot]"},
                "authorAssociation": "NONE",
                "body": "x" * 5000,
            },
        ],
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "build",
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/pbek/QOwnNotes/actions/runs/1/job/22",
            },
            {"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": "cla", "state": "ERROR", "targetUrl": ""},
        ],
    }
    data.update(overrides)
    return data


THREADS = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [
                        {
                            "isResolved": False,
                            "path": "src/ai.cpp",
                            "line": None,
                            "originalLine": 151,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": "willem-bd"},
                                        "authorAssociation": "CONTRIBUTOR",
                                        "body": "This default is not valid for the new model.",
                                    }
                                ]
                            },
                        },
                        {
                            "isResolved": True,
                            "path": "README.md",
                            "line": 3,
                            "comments": {
                                "nodes": [{"author": {"login": "pbek"}, "body": "settled"}]
                            },
                        },
                    ]
                }
            }
        }
    }
}


def test_feedback_keeps_what_others_said_and_which_checks_failed() -> None:
    log = "https://github.com/pbek/QOwnNotes/actions/runs/1/job/22"
    assert failed_runs(viewed()) == [log]
    feedback = parse_feedback(viewed(), THREADS, {log: "error: no member named 'model'"})
    assert (feedback.number, feedback.branch, feedback.base) == (
        7,
        "fix/groq-retired-models",
        "main",
    )
    assert feedback.repository == "tiltmeter-dev/QOwnNotes"  # where the branch lives: the fork
    said = [(n.author, n.role, n.kind, n.verdict, n.path, n.line) for n in feedback.notes]
    assert said == [
        ("pbek", "owner", "review", "changes requested", None, None),
        ("review-bot[bot]", "bot", "comment", None, None, None),
        ("willem-bd", "contributor", "line", None, "src/ai.cpp", 151),
    ]
    # Not there: an approval without a word, the viewer's own reply, a hidden comment and
    # a resolved thread. A long comment is cut.
    assert len(feedback.notes[1].body) == 2000
    assert [(c.name, c.log) for c in feedback.failed_checks] == [
        ("build", "error: no member named 'model'"),
        ("cla", ""),
    ]
    assert not feedback.empty
    quiet = parse_feedback(viewed(reviews=[], comments=[], statusCheckRollup=[]), {})
    assert quiet.empty


def test_feedback_is_bounded() -> None:
    many = [
        {"author": {"login": f"u{i}"}, "authorAssociation": "NONE", "body": f"note {i}"}
        for i in range(60)
    ]
    checks = [{"name": f"job {i}", "conclusion": "FAILURE"} for i in range(9)]
    feedback = parse_feedback(viewed(reviews=[], comments=many, statusCheckRollup=checks), None)
    assert [n.body for n in feedback.notes][:2] == ["note 20", "note 21"]  # the latest 40
    assert len(feedback.notes) == 40
    assert len(feedback.failed_checks) == 3


def test_a_remote_is_found_by_its_github_repository(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    main = Git.open(repo)
    assert main.remote_of("acme/app") is None  # origin is a folder here
    git(repo, "remote", "add", "fork", "git@github.com:Tiltmeter-Dev/App.git")
    git(repo, "remote", "add", "upstream", "https://github.com/acme/app")
    assert main.remote_of("tiltmeter-dev/app") == "fork"
    assert main.remote_of("acme/app") == "upstream"
    assert main.remote_of("acme/application") is None


def test_a_commit_is_pushed_onto_another_branch_but_never_forced(tmp_path: Path) -> None:
    repo, remote = make_repo(tmp_path)
    main = Git.open(repo)
    git(repo, "push", "--quiet", "origin", "main:refs/heads/pr")
    start = main.fetch("origin", "pr")
    worktree = main.add_worktree(tmp_path / "wt", "mine", start)
    (worktree.root / "client.py").write_text("print('answer')\n", encoding="utf-8")
    worktree.stage_all()
    worktree.commit("Address the review", "Body.")
    main.push_to("origin", "mine", "pr")
    assert git(remote, "log", "-1", "--format=%s", "pr").strip() == "Address the review"
    # Someone else pushed in the meantime: the remote says no, and nothing is overwritten.
    theirs = main.add_worktree(tmp_path / "theirs", "theirs", main.fetch("origin", "pr"))
    (theirs.root / "models.py").write_text("THEIRS = True\n", encoding="utf-8")
    theirs.stage_all()
    theirs.commit("A commit of the maintainer's", "Body.")
    main.push_to("origin", "theirs", "pr")
    (worktree.root / "client.py").write_text("print('again')\n", encoding="utf-8")
    worktree.stage_all()
    worktree.commit("Address the review again", "Body.")
    with pytest.raises(FixError, match="`git push --quiet` failed"):
        main.push_to("origin", "mine", "pr")
    assert git(remote, "log", "-1", "--format=%s", "pr").strip() == "A commit of the maintainer's"
