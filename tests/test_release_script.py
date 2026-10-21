from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

import tiltmeter

SCRIPT = Path(__file__).parent.parent / "scripts" / "release.py"

CHANGELOG = """# Changelog

## [Unreleased]

## [0.3.0rc1] - 2026-12-01

### Changed

- Release candidate.

## [0.2.0] - 2026-11-02

### Fixed

- Something.

## [0.1.0] - 2026-10-21

### Added

- First version.

## [0.0.9] - 2026-10-01

[0.2.0]: https://github.com/acme/tiltmeter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/acme/tiltmeter/releases/tag/v0.1.0
"""


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script()


def make_repo(tmp_path: Path, *, version: str = "0.2.0", owner: str = "acme") -> Path:
    package = tmp_path / "src" / "tiltmeter"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""Tiltmeter."""\n\n__version__ = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "tiltmeter"\n\n'
        f'[project.urls]\nSource = "https://github.com/{owner}/tiltmeter"\n'
    )
    return tmp_path


def test_a_matching_tag_is_ready(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert release.check("v0.2.0", repo) == {"version": "0.2.0", "prerelease": "false"}


def test_release_candidates_are_prereleases(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, version="0.3.0rc1")
    assert release.check("v0.3.0rc1", repo)["prerelease"] == "true"
    assert release.is_prerelease("1.0.0b2")
    assert not release.is_prerelease("1.0.0")


@pytest.mark.parametrize(
    ("tag", "version", "owner", "message"),
    [
        ("0.2.0", "0.2.0", "acme", "release tags look like"),
        ("v0.2", "0.2.0", "acme", "release tags look like"),
        ("v0.2.0.post1", "0.2.0", "acme", "release tags look like"),
        ("v0.2.1", "0.2.0", "acme", "does not match __version__"),
        ("v0.4.0", "0.4.0", "acme", "no '## [0.4.0] - YYYY-MM-DD' section"),
        ("v0.0.9", "0.0.9", "acme", "section for 0.0.9 is empty"),
        ("v0.2.0", "0.2.0", "OWNER", "github.com/OWNER/"),
    ],
)
def test_check_rejects_unready_releases(
    tmp_path: Path, tag: str, version: str, owner: str, message: str
) -> None:
    repo = make_repo(tmp_path, version=version, owner=owner)
    with pytest.raises(release.ReleaseError, match=message.replace("[", r"\[")):
        release.check(tag, repo)


def test_notes_are_the_changelog_section(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert release.changelog_section("0.2.0", repo) == "### Fixed\n\n- Something."
    assert release.changelog_section("0.1.0", repo) == "### Added\n\n- First version."


def test_main_writes_step_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    outputs = tmp_path / "outputs.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    assert release.main(["check", "v0.2.0"], root=repo) == 0
    assert outputs.read_text() == "version=0.2.0\nprerelease=false\n"
    assert release.main(["notes", "v0.2.0"], root=repo) == 0
    assert "- Something." in capsys.readouterr().out


def test_main_reports_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert release.main(["check", "v9.9.9"], root=repo) == 1
    assert capsys.readouterr().err.startswith("error: tag v9.9.9")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert release.main(["notes", "7.7.7"], root=repo) == 1
    assert capsys.readouterr().err.startswith("::error::")


def test_the_repository_version_is_readable() -> None:
    assert release.package_version() == tiltmeter.__version__
