"""Is the code the agent changed still used? A plain text search, before anything is committed.

The agent adapts every call it finds, including calls in files nothing imports any more.
A pull request that updates dead code wastes the maintainer's time: ospfranco/sol closed
one that moved ``src/lib/weather.ts`` to a new API version, a file unused since 2024. So
once the agent is done, Tiltmeter looks for each changed source file in the rest of the
tree, the way code reaches it:

- its name, with an extension of its language (``weather.ts``, ``weather.js``), as
  ``package.json``, a Dockerfile, a workflow or a Makefile spells it;
- a path that ends in it (``'../lib/weather'``, ``src/weather.handler``), or a bare
  import (``from 'weather'``);
- in Python, ``import`` and ``from ... import`` statements, dotted paths in strings
  (``"app.weather:main"``) and ``python -m``; in Rust, ``mod weather;`` and
  ``crate::weather``;
- in Swift, whose files are compiled without anything naming them, the names the file
  declares at the top level (``struct OneCall``). A file that extends a type declared
  elsewhere counts as used, since its methods are called by their own names.

A word alone is no use: ``tags: ['weather']`` in an emoji table does not import anything.
Documentation does not count either, unless it tells people to run the file
(``python weather.py``).

Go, Java, Kotlin, C#, Ruby, PHP and the rest find files by folder, package or convention,
so their files are never flagged; nor are entry points (``index.ts``, ``__main__.py``,
``main.rs``, config, test and script files), files in folders that frameworks or loaders
list (``pages``, ``routes``, ``plugins``, ``commands``), files that share a prefix with a
sibling (``plugin_*.py``), and a language's files once its code imports modules by
computed names. A wrong "unused" would hide a real fix, so a file counts as used whenever
the search is unsure.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Final, Literal

from tiltmeter.fix.git import Git
from tiltmeter.fix.ignore import is_ignored
from tiltmeter.fix.models import UnusedFile
from tiltmeter.fix.tree import LOCK_FILES, MAX_FILES, NOT_OURS, is_ours, read_text

STALE_DAYS: Final = 180

type Language = Literal["js", "python", "rust", "swift"]
type Kind = Language | Literal["docs", "other"]

_LANGUAGES: Final[Mapping[str, Language]] = {
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "js",
    ".tsx": "js",
    ".mts": "js",
    ".cts": "js",
    ".py": "python",
    ".rs": "rust",
    ".swift": "swift",
}
# The extensions a reference may give the file: ``./weather.js`` imports ``weather.ts``.
_EXTENSIONS: Final[Mapping[Language, str]] = {
    "js": r"[cm]?[jt]sx?",
    "python": r"py",
    "rust": r"rs",
    "swift": r"swift",
}
# Files whose imports read like JavaScript's, without being checked themselves.
_JS_LIKE: Final = frozenset({".vue", ".svelte", ".astro", ".mdx"})

_DOC_SUFFIXES: Final = frozenset({".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt", ".org"})
_DOC_NAMES: Final = (
    "CHANGELOG*",
    "CHANGES*",
    "HISTORY*",
    "LICENSE*",
    "LICENCE*",
    "COPYING*",
    "NOTICE*",
    "AUTHORS*",
    "CONTRIBUTORS*",
    "CODEOWNERS",
    ".*ignore",
)
_DOC_FOLDERS: Final = frozenset({"docs", "doc"})

# Installed, vendored and built code, besides what the notice search already leaves out.
_NOT_READ: Final = NOT_OURS | {
    "dist",
    "build",
    "out",
    "coverage",
    "htmlcov",
    "third_party",
    "pods",
    "carthage",
    "deriveddata",
}
_NOT_READ_NAMES: Final = ("*.min.js", "*.min.mjs", "*.bundle.js", "*.map")
_MORE_LOCK_FILES: Final = frozenset(
    {"bun.lock", "bun.lockb", "deno.lock", "Podfile.lock", "Package.resolved", "pubspec.lock"}
)

# Files that a runtime, a framework or a tool finds by their name.
_ENTRY_STEMS: Final[Mapping[Language, frozenset[str]]] = {
    "js": frozenset(
        {
            "index",
            "main",
            "app",
            "server",
            "cli",
            "worker",
            "sw",
            "service-worker",
            "preload",
            "middleware",
            "instrumentation",
            "root",
            "entry",
            "entry.client",
            "entry.server",
            "handler",
            "page",
            "layout",
            "route",
            "loading",
            "error",
            "not-found",
            "template",
            "default",
            "head",
            "_app",
            "_document",
            "_error",
        }
    ),
    "python": frozenset(
        {
            "__init__",
            "__main__",
            "main",
            "app",
            "setup",
            "manage",
            "conftest",
            "wsgi",
            "asgi",
            "settings",
            "urls",
            "models",
            "admin",
            "apps",
            "tasks",
            "signals",
            "celery",
            "fabfile",
            "noxfile",
            "dodo",
            "handler",
            "lambda_function",
            "function_app",
            "streamlit_app",
        }
    ),
    "rust": frozenset({"main", "lib", "mod", "build"}),
    "swift": frozenset({"main", "package", "appdelegate", "scenedelegate"}),
}
_ENTRY_NAMES: Final = (
    ".*",
    "*.config.*",
    "*.conf.*",
    "setup*",
    "gulpfile.*",
    "gruntfile.*",
    "*.d.ts",
    "*.d.mts",
    "*.d.cts",
    "+*",
    "[[]*",
    "test_*",
    "*_test.*",
    "tests.*",
    "*.test.*",
    "*.spec.*",
    "*.stories.*",
    "*.story.*",
    "*.e2e.*",
    "*.bench.*",
    "*tests.swift",
    "*test.swift",
    "*spec.swift",
)
# Folders of tests, scripts and examples, which a test runner or a person runs by path.
_RUN_FOLDERS: Final = frozenset(
    {
        "test",
        "tests",
        "spec",
        "fixtures",
        "benchmarks",
        "examples",
        "example",
        "scripts",
        "script",
        "bin",
        "tools",
        "plugins",
    }
)
# Folders that a loader lists to import what is in them, in languages that can.
_LOADED_FOLDERS: Final = _RUN_FOLDERS | {
    "extensions",
    "addons",
    "handlers",
    "commands",
    "providers",
    "jobs",
    "workers",
    "lambdas",
    "lambda",
    "api",
}
# Compiled languages only read the folders their build tool runs.
_LISTED_BY: Final[Mapping[Language, frozenset[str]]] = {
    "js": _LOADED_FOLDERS
    | {
        "pages",
        "app",
        "routes",
        "functions",
        "netlify",
        "server",
        "middleware",
        "public",
        "static",
        "stories",
        "__tests__",
        "__mocks__",
        "e2e",
        "cypress",
        "playwright",
    },
    "python": _LOADED_FOLDERS
    | {"management", "migrations", "dags", "spiders", "templatetags", "alembic"},
    "rust": _RUN_FOLDERS | {"benches", "fuzz_targets"},
    "swift": _RUN_FOLDERS | {"uitests", "preview content"},
}
# A file that runs as a program: a shebang, ``if __name__ == "__main__"``, ``@main``.
_RUNS_ITSELF: Final = re.compile(
    r"\A#!|__name__\s*==\s*['\"]__main__['\"]|\brequire\.main\s*===?\s*module\b"
    r"|\bimport\.meta\.main\b|^@(?:main|NSApplicationMain|UIApplicationMain)\b|^fn\s+main\s*\(",
    re.MULTILINE,
)
_RUNNERS: Final = (
    "python3?|py|pypy3?|node|deno|bunx?|tsx|ts-node|npx|pnpm|yarn|npm|uvx?|poetry|pipenv|pdm"
    "|hatch|streamlit|uvicorn|gunicorn|hypercorn|daphne|flask|fastapi|celery|cargo|sh|bash"
)

# Imports whose module is computed, and the literal start of its path when there is one.
_JS_GLOB: Final = re.compile(r"""\bimport\.meta\.glob(?:Eager)?\s*\(\s*\[?\s*['"`]([^'"`*{]*)""")
_JS_CONTEXT: Final = re.compile(r"""\brequire\.context\s*\(\s*['"`]([^'"`]*)""")
_JS_COMPUTED: Final = re.compile(
    r"""\b(?:import|require)\s*\(\s*(?:`([^`$]*)\$\{|['"]([^'"]*)['"]\s*\+)"""
)
_JS_BY_NAME: Final = re.compile(r"\b(?:import|require)\s*\(\s*[A-Za-z_$]")
_JS_LISTING: Final = re.compile(r"\b(?:readdirSync|readdir|opendir|globSync|glob|fastGlob)\s*\(")
_PY_COMPUTED: Final = re.compile(
    r"""\b(?:import_module|__import__)\s*\(\s*(?:f['"]([^'"{]*)\{|['"]([^'"]*)['"]\s*\+)"""
)
_PY_BY_NAME: Final = re.compile(
    r"\b(?:import_module|__import__|spec_from_file_location|load_source|run_path)\s*\(\s*[A-Za-z_]"
)
_PY_LISTING: Final = re.compile(
    r"\b(?:listdir|iterdir|scandir|glob|rglob|iter_modules|walk_packages|walk)\s*\("
)

_SWIFT_DECLARATION: Final = re.compile(
    r"^(?:@\w+(?:\([^)\n]*\))?\s+)*"
    r"(?:(?:public|private|fileprivate|internal|open|package|final|indirect|nonisolated)\s+)*"
    r"(class|struct|enum|protocol|actor|typealias|func|let|var|macro"
    r"|extension|operator|precedencegroup|prefix|postfix|infix)\b[ \t]*(\w*)",
    re.MULTILINE,
)
_SWIFT_OBJC_NAME: Final = re.compile(r"@objc\((\w+)\)")
# Declarations that add no name of their own, so their use cannot be told from names.
_SWIFT_NAMELESS: Final = frozenset(
    {"extension", "operator", "precedencegroup", "prefix", "postfix", "infix"}
)


def _folders(path: PurePosixPath) -> list[str]:
    return [part.lower() for part in path.parts[:-1]]


def _documentation(path: PurePosixPath) -> bool:
    return (
        path.suffix.lower() in _DOC_SUFFIXES
        or any(fnmatchcase(path.name, pattern) for pattern in _DOC_NAMES)
        or not _DOC_FOLDERS.isdisjoint(_folders(path))
    )


def _kind(path: PurePosixPath) -> Kind:
    if _documentation(path):
        return "docs"
    suffix = path.suffix.lower()
    if suffix in _JS_LIKE:
        return "js"
    if suffix == ".pyi":
        return "python"
    return _LANGUAGES.get(suffix, "other")


def _read(path: PurePosixPath) -> bool:
    """Whether a tracked file is the repository's own text, worth searching."""
    name = path.name
    if name in LOCK_FILES or name in _MORE_LOCK_FILES:
        return False
    if any(fnmatchcase(name.lower(), pattern) for pattern in _NOT_READ_NAMES):
        return False
    return is_ours(path) and _NOT_READ.isdisjoint(_folders(path))


def _found_by_name(path: PurePosixPath, language: Language) -> bool:
    """Entry points, and files in folders a framework, a tool or a loader lists."""
    name, stem = path.name.lower(), path.stem.lower()
    if stem in _ENTRY_STEMS[language] or any(fnmatchcase(name, p) for p in _ENTRY_NAMES):
        return True
    for folder in _folders(path):
        if folder.startswith(".") or folder in _LISTED_BY[language] or folder in _NOT_READ:
            return True
        if language == "swift" and folder.endswith("tests"):
            return True
    return False


def _in_a_series(path: PurePosixPath, siblings: Sequence[PurePosixPath]) -> bool:
    """Whether a sibling of the same type shares the file's prefix or role, as
    ``plugin_time.py`` does with ``plugin_weather.py``, and ``user.entity.ts`` with
    ``weather.entity.ts``: loaders find such files by a pattern."""
    stem = path.stem.lower()
    head = re.match(r"[^_\-.]+[_\-.]", stem)
    role = stem.rpartition(".")[2] if "." in stem else None
    for sibling in siblings:
        other = sibling.stem.lower()
        if sibling == path or sibling.suffix.lower() != path.suffix.lower():
            continue
        if head is not None and other.startswith(head.group()):
            return True
        if role is not None and other.endswith(f".{role}"):
            return True
    return False


def _swift_names(text: str) -> list[str] | None:
    """The names a Swift file declares at the top level; None when it cannot tell, as for
    a file that extends a type declared elsewhere, whose use the type's name does not show."""
    declarations = _SWIFT_DECLARATION.findall(text)
    names = [name for keyword, name in declarations if keyword not in _SWIFT_NAMELESS]
    for keyword, name in declarations:
        extends_its_own = keyword == "extension" and name in names
        if not name or (keyword in _SWIFT_NAMELESS and not extends_its_own):
            return None
    names += _SWIFT_OBJC_NAME.findall(text)
    return list(dict.fromkeys(names)) or None


def _alternatives(names: Sequence[str]) -> str:
    return "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))


def _patterns(path: PurePosixPath, language: Language) -> dict[Kind, re.Pattern[str]]:
    """What spells a use of the file, in each kind of file. Kinds left out never count."""
    stem = re.escape(path.stem)
    name = rf"(?<![\w\-.$@]){stem}\.(?:{_EXTENSIONS[language]})(?![\w\-])"
    after_slash = rf"(?<=[/\\]){stem}(?:\.\w+)*(?![\w\-/\\])"
    anywhere = [name, after_slash]
    run = [name, after_slash]
    own: list[str] = []
    other: list[str] = []
    if language == "js":
        own.append(
            rf"""(?:\bfrom|\bimport\s*\(?|\brequire\s*\(|\.mock\s*\()\s*['"`]{stem}(?:\.\w+)?['"`]"""
        )
    elif language == "python":
        module = rf"(?:\w+\.)*{stem}"
        spec = rf"(?<![\w.\-/]){module}:\w+"
        run_module = rf"-m[ \t]+{module}(?!\w)"
        anywhere += [
            rf"""['"](?:(?:\w+\.)+{stem}(?:[.:]\w[\w.:]*)?|{stem}[.:]\w[\w.:]*)['"]""",
            spec,
            run_module,
        ]
        run += [spec, run_module]
        own += [
            rf"^[ \t]*from[ \t]+\.*(?:\w+\.)*{stem}(?:\.\w+)*[ \t]+import\b",
            rf"^[ \t]*import[ \t]+[^\n#]*?(?<![\w.]){module}(?!\w)",
            rf"^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+"
            rf"(?:\([^)]*?|[^\n#(]*?)(?<![\w.]){stem}(?!\w)",
        ]
        other.append(rf"(?<![\w.\-/])(?:\w+\.)+{stem}(?:\.\w+)*(?![\w\-])")
    elif language == "rust":
        own += [
            rf"\bmod[ \t]+(?:r\#)?{stem}(?!\w)",
            rf"(?:\bcrate|\bsuper|\bself)(?:::\w+)*::{stem}(?!\w)",
        ]
    flags = re.IGNORECASE | re.MULTILINE
    shared = re.compile("|".join(anywhere), flags)
    patterns: dict[Kind, re.Pattern[str]] = dict.fromkeys(("js", "python", "rust", "swift"), shared)
    patterns[language] = re.compile("|".join(anywhere + own), flags)
    patterns["other"] = re.compile("|".join(anywhere + other), flags)
    patterns["docs"] = re.compile(
        rf"(?<![\w\-./])(?:{_RUNNERS})(?![\w\-.])[^\n]*?(?:{'|'.join(run)})", flags
    )
    return patterns


@dataclass(slots=True)
class _Candidate:
    """A changed source file, and what would show that something uses it."""

    path: str
    language: Language
    patterns: Mapping[Kind, re.Pattern[str]]

    def loaded_from(self, language: Language, folder: str | None) -> bool:
        """Whether code that imports ``language`` modules from ``folder`` (any folder when
        None) by computed names may load this file."""
        if language != self.language:
            return False
        return folder is None or folder in _folders(PurePosixPath(self.path))

    def used_by(self, name: str, kind: Kind, text: str) -> bool:
        if self.language == "swift" and any(
            part.lower().endswith((".xcodeproj", ".xcworkspace")) for part in name.split("/")
        ):
            return False  # Xcode projects list every file by name
        pattern = self.patterns.get(kind)
        return pattern is not None and pattern.search(text) is not None


def _candidate(path: PurePosixPath, text: str | None) -> _Candidate | None:
    """The file as something to look for, or None when it counts as used without a search."""
    language = _LANGUAGES.get(path.suffix.lower())
    if language is None or text is None or _found_by_name(path, language):
        return None
    if _RUNS_ITSELF.search(text):
        return None
    if language != "swift":
        return _Candidate(str(path), language, _patterns(path, language))
    names = _swift_names(text)
    if names is None:
        return None
    # Type names are case-sensitive; ``OneCall.swift`` in a project file is no use of it.
    uses = re.compile(rf"(?<![\w$])(?:{_alternatives(names)})(?![\w$]|\.swift\b)")
    patterns: dict[Kind, re.Pattern[str]] = dict.fromkeys(
        ("js", "python", "rust", "swift", "other"), uses
    )
    return _Candidate(str(path), language, patterns)


def _folder(prefix: str, separator: str) -> str | None:
    """The folder a computed import reads from: ``plugins`` for ``./plugins/plugin_`` or
    ``app.plugins.``; None when the prefix names none."""
    parts = prefix.split(separator)[:-1]
    named = [part.lower() for part in parts if part not in {"", ".", "..", "@", "~"}]
    return named[-1] if named else None


def _loaders(kind: Kind, text: str) -> Iterator[tuple[Language, str | None]]:
    """The folders this file imports modules from by computed names; None for any folder."""
    if kind == "js":
        for match in _JS_GLOB.finditer(text):
            yield "js", _folder(match.group(1), "/")
        for match in _JS_CONTEXT.finditer(text):
            yield "js", _folder(f"{match.group(1)}/", "/")
        for match in _JS_COMPUTED.finditer(text):
            yield "js", _folder(match.group(1) or match.group(2) or "", "/")
        if _JS_BY_NAME.search(text) and _JS_LISTING.search(text):
            yield "js", None
    elif kind == "python":
        for match in _PY_COMPUTED.finditer(text):
            yield "python", _folder(match.group(1) or match.group(2) or "", ".")
        if _PY_BY_NAME.search(text) and _PY_LISTING.search(text):
            yield "python", None


def unused_files(
    worktree: Git,
    changed: Sequence[str],
    *,
    ignore: Sequence[str] = (),
    now: datetime | None = None,
) -> list[UnusedFile]:
    """The changed source files that nothing else in the worktree imports or names, with
    when a commit last changed them. Files that are new, deleted or never read are left out."""
    root = worktree.root
    candidates: dict[str, _Candidate] = {}
    for name in changed:
        path = PurePosixPath(name)
        if path.suffix.lower() in _LANGUAGES and _read(path) and not is_ignored(name, ignore):
            candidate = _candidate(path, read_text(root / name))
            if candidate is not None:
                candidates[name] = candidate
    if not candidates:
        return []
    files = [
        PurePosixPath(name)
        for name in worktree.tracked_files()
        if _read(PurePosixPath(name)) and not is_ignored(name, ignore)
    ]
    if len(files) > MAX_FILES:
        return []  # too large to read all of it, so nothing is sure
    siblings: dict[PurePosixPath, list[PurePosixPath]] = defaultdict(list)
    for path in files:
        siblings[path.parent].append(path)
    pending = {
        name: candidate
        for name, candidate in candidates.items()
        if not _in_a_series(PurePosixPath(name), siblings[PurePosixPath(name).parent])
    }
    for path in files:
        if not pending:
            break
        text = read_text(root / path)
        if text is None:
            continue
        name, kind = str(path), _kind(path)
        for other, candidate in list(pending.items()):
            if other != name and candidate.used_by(name, kind, text):
                del pending[other]
        for language, folder in _loaders(kind, text):
            for other, candidate in list(pending.items()):
                if candidate.loaded_from(language, folder):
                    del pending[other]
    moment = now or datetime.now(UTC)
    unused: list[UnusedFile] = []
    for name in pending:
        last = worktree.last_changed(name)
        if last is None:
            continue
        age = (moment - datetime.fromisoformat(last)).days
        unused.append(UnusedFile(path=name, last_changed=last, age_days=max(age, 0)))
    return unused


def likely_dead(changed: Sequence[str], unused: Sequence[UnusedFile]) -> bool:
    """Whether the edits touch nothing but unused code, each file unchanged for over
    :data:`STALE_DAYS` days, and documentation."""
    old = {item.path for item in unused if item.age_days > STALE_DAYS}
    return bool(old) and all(name in old or _documentation(PurePosixPath(name)) for name in changed)
