"""The repository's own text files, as the notice search and the unused-code check read them.

Installed or generated code, agents' copies of the repository, lock files, symbolic links,
binaries and large files are left out.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Final

MAX_FILE_BYTES: Final = 1_000_000
MAX_FILES: Final = 20_000

# Folders of installed or generated code, and agents' copies of the repository. They are
# not the repository's own code, and can hold more files than the index reads.
NOT_OURS: Final = frozenset(
    {
        "node_modules",
        "vendor",
        "bower_components",
        ".venv",
        "venv",
        "site-packages",
        "__pycache__",
        ".next",
        ".nuxt",
        ".output",
        ".svelte-kit",
        ".turbo",
        ".vercel",
        ".tiltmeter",
    }
)
_AGENT_COPIES: Final = ((".claude", "worktrees"), (".codex", "worktrees"))
LOCK_FILES: Final = frozenset(
    {
        "Cargo.lock",
        "Gemfile.lock",
        "Pipfile.lock",
        "composer.lock",
        "go.sum",
        "package-lock.json",
        "packages.lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)


def is_ours(path: PurePosixPath) -> bool:
    """Whether a repository path is outside installed or generated code and agents' copies."""
    folders = path.parts[:-1]
    if NOT_OURS.intersection(folders):
        return False
    pairs = set(pairwise(folders))
    return not pairs.intersection(_AGENT_COPIES)


def read_text(path: Path) -> str | None:
    """A file's text; None for links, binaries, large files and what cannot be read."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")
