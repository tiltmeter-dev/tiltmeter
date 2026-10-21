"""Files the fix agent must never read or change (``fix.ignore``).

A pattern without a slash matches a file or folder name at any depth, so ``.env*``
covers ``.env`` and ``config/.env.local``. A pattern with a slash matches a path
from the repository root. The same rules feed the agent's filesystem permissions
and the check of the final diff.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

ALWAYS_BLOCKED = (".git",)


def is_ignored(path: str, patterns: Iterable[str]) -> bool:
    """Whether a repository-relative POSIX path matches any pattern."""
    parts = PurePosixPath(path).parts
    for pattern in (*ALWAYS_BLOCKED, *patterns):
        anchored = pattern.strip("/")
        if "/" not in anchored:
            if any(fnmatchcase(part, anchored) for part in parts):
                return True
        elif fnmatchcase(path, anchored) or fnmatchcase(path, f"{anchored}/*"):
            return True
    return False


def deny_globs(patterns: Iterable[str]) -> list[str]:
    """The patterns as absolute globs for the agent's virtual filesystem, rooted at ``/``."""
    globs: list[str] = []
    for pattern in (*ALWAYS_BLOCKED, *patterns):
        anchored = pattern.strip("/")
        prefix = "/**/" if "/" not in anchored else "/"
        globs += [f"{prefix}{anchored}", f"{prefix}{anchored}/**"]
    return list(dict.fromkeys(globs))
