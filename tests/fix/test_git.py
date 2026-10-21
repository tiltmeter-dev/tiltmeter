from __future__ import annotations

from pathlib import Path

import pytest

from tests.fix.kit import git, make_repo
from tiltmeter.errors import FixError
from tiltmeter.fix.git import GhCli, Git


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


def test_failures_carry_git_s_reason(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    with pytest.raises(FixError, match=r"`git push --quiet` failed: .*nowhere"):
        Git.open(repo).push("nowhere", "main")


def test_gh_problem_when_the_cli_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tiltmeter.fix.git.shutil.which", lambda _: None)
    assert GhCli().problem(tmp_path) == "the GitHub CLI (gh) is not installed"
