"""Release helpers used by .github/workflows/release.yml. Standard library only.

    python scripts/release.py check v0.1.0   # is the repository ready to release this tag?
    python scripts/release.py notes 0.1.0    # print that version's section of CHANGELOG.md

Inside GitHub Actions, `check` also writes `version` and `prerelease` to the step outputs.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "github.com/OWNER/"

_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_RELEASE = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
_PRERELEASE = re.compile(r"(?:a|b|rc)\d+$")
_NEXT_SECTION = re.compile(r"^## \[", re.MULTILINE)
_LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s+\S+[ \t]*$", re.MULTILINE)


class ReleaseError(Exception):
    """The repository is not ready to release this tag."""


def package_version(root: Path = ROOT) -> str:
    """``__version__`` from ``src/tiltmeter/__init__.py``."""
    text = (root / "src" / "tiltmeter" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION.search(text)
    if match is None:
        raise ReleaseError("could not find __version__ in src/tiltmeter/__init__.py")
    return match.group(1)


def is_prerelease(version: str) -> bool:
    """True for versions such as ``0.2.0rc1``, which go to TestPyPI."""
    return _PRERELEASE.search(version) is not None


def changelog_section(version: str, root: Path = ROOT) -> str:
    """The body of the ``## [version] - YYYY-MM-DD`` section of CHANGELOG.md."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.compile(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*$", re.MULTILINE
    )
    match = heading.search(text)
    if match is None:
        raise ReleaseError(
            f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section; "
            "move the [Unreleased] entries under one before tagging"
        )
    following = _NEXT_SECTION.search(text, match.end())
    body = text[match.end() : following.start() if following else len(text)]
    body = _LINK_DEFINITION.sub("", body).strip()
    if not body:
        raise ReleaseError(f"the CHANGELOG.md section for {version} is empty")
    return body


def check(tag: str, root: Path = ROOT) -> dict[str, str]:
    """Validate a release tag and return the workflow step outputs."""
    if not tag.startswith("v") or not _RELEASE.fullmatch(tag[1:]):
        raise ReleaseError(f"release tags look like v1.2.3 or v1.2.3rc1, got {tag!r}")
    version = tag[1:]
    current = package_version(root)
    if current != version:
        raise ReleaseError(
            f"tag {tag} does not match __version__ = {current!r} in src/tiltmeter/__init__.py"
        )
    changelog_section(version, root)
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    tomllib.loads(pyproject)
    if PLACEHOLDER in pyproject:
        raise ReleaseError(
            "pyproject.toml still points at github.com/OWNER/; "
            "set the repository owner first (see RELEASING.md)"
        )
    return {"version": version, "prerelease": "true" if is_prerelease(version) else "false"}


def _write_step_outputs(outputs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Release helpers for Tiltmeter.")
    commands = parser.add_subparsers(dest="command", required=True)
    check_command = commands.add_parser("check", help="validate a release tag")
    check_command.add_argument("tag")
    notes_command = commands.add_parser("notes", help="print a version's changelog section")
    notes_command.add_argument("version")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            outputs = check(args.tag, root)
            _write_step_outputs(outputs)
            print(
                f"{args.tag} is ready: version {outputs['version']}, "
                f"prerelease {outputs['prerelease']}"
            )
        else:
            print(changelog_section(args.version.removeprefix("v"), root))
    except ReleaseError as exc:
        prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
        print(f"{prefix}{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
