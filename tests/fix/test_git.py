from __future__ import annotations

import os
import sys
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
