"""``tiltmeter scan``: does this code still use API changes that already shipped?

A feed brings news, so someone who subscribes today never learns that Groq shut its models
down in August. Tiltmeter bundles a catalog of known changes instead (retired models,
removed endpoints, moved fields), and the scan runs every entry through the search that
``notices apply`` uses (:mod:`tiltmeter.notices.impact`), whatever its dates, then points
at the lines that matched. It needs no configuration, records nothing and runs no agent;
the bundled catalog is read from the package, so nothing is fetched or sent.

A folder is read as git sees it, tracked files and new ones it does not ignore, or file by
file outside git, without installed or generated code. A line in a file that nothing else
imports or names (:func:`tiltmeter.fix.usage.unreferenced`) is marked, so nobody chases
dead code.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, Field

from tiltmeter.catalog import LOCATION as CATALOG_LOCATION
from tiltmeter.core.models import ChangeKind
from tiltmeter.errors import FixError, NoticeError
from tiltmeter.fix.git import Git
from tiltmeter.fix.request import field_name
from tiltmeter.fix.tree import MAX_FILES, NOT_OURS, is_ours
from tiltmeter.fix.usage import unreferenced
from tiltmeter.notices.impact import (
    Place,
    RepoIndex,
    assess,
    locate,
    mentions_in_docs,
)
from tiltmeter.notices.models import ApiChangeNotice, Impact, NoticeChange, printable
from tiltmeter.notices.reader import LoadedNotice, NoticeReader, _label, bundled_feed

MAX_MATCHES: Final = 50
MAX_DOC_MENTIONS: Final = 5
_LINE_CHARS: Final = 160
_HEADLINE_CHARS: Final = 120
_SENTENCE_END: Final = re.compile(r"(?<=[a-z0-9)\]][.!?])\s+(?=[A-Z])")

# Outside git, these folders hold tools' state, installed or built code, not the project's.
_NOT_WALKED: Final = NOT_OURS | {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "coverage",
    "htmlcov",
}

# Tests and what they read, by folder and by name, in the usual conventions.
_TEST_FOLDERS: Final = frozenset(
    {
        "test",
        "tests",
        "spec",
        "specs",
        "__tests__",
        "__mocks__",
        "__fixtures__",
        "__data__",
        "__snapshots__",
        "fixtures",
        "testdata",
        "test_data",
        "testing",
        "e2e",
        "cypress",
        "playwright",
    }
)
_TEST_FOLDER_ENDINGS: Final = (".tests", "_tests", "-tests", ".test", "_test", "-test")
_TEST_NAMES: Final = (
    "test_*",
    "*_test.*",
    "*.test.*",
    "*.spec.*",
    "*_spec.*",
    "tests.*",
    "conftest.py",
)
# Test classes, in the languages that name them so: ABTest.tsx is a component.
_TEST_CLASS_NAMES: Final = tuple(
    f"*{suffix}.{extension}"
    for suffix in ("Test", "Tests", "IT", "Spec")
    for extension in ("java", "kt", "scala", "groovy", "cs", "swift", "php")
)

type MatchKind = Literal["value", "endpoint", "field", "api"]


class ScanMatch(BaseModel):
    """A line that uses what a change retires, removes or renames."""

    file: str
    line: int
    text: str = Field(description="The line, trimmed, as plain text.")
    kind: MatchKind = Field(
        description=(
            "value: spells a retired value; endpoint: calls a removed endpoint; field: reads a "
            "changed field near the API; api: names the API, when none of those was found."
        )
    )
    term: str = Field(description="What it spells: the value, endpoint, field, host or SDK.")
    detail: str = Field(description="What the change does to it.")
    possibly_unused: bool = Field(
        default=False, description="Nothing else in the folder imports or names the file."
    )


class ScanFinding(BaseModel):
    """A catalog entry that the folder uses, and where."""

    id: str
    provider: str
    headline: str = Field(description="The summary's first sentence.")
    summary: str
    published_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    signed: bool = Field(description="Signed by the provider, with the signature checked.")
    drafted: bool = Field(
        description="Drafted by Tiltmeter from the provider's public documentation."
    )
    reason: str
    matches: list[ScanMatch] = Field(description=f"The surest first, at most {MAX_MATCHES}.")
    count: int = Field(description="Every matching line.")
    files: int = Field(description="Files with a matching line.")
    tests: list[ScanMatch] = Field(
        default_factory=list,
        description=f"Places in tests, which are listed apart; at most {MAX_MATCHES}.",
    )
    fix_command: str | None = Field(
        default=None, description="The command that fixes it, when one can."
    )


class ScanMiss(BaseModel):
    """A catalog entry that does not affect the folder, and why."""

    id: str
    provider: str
    headline: str
    reason: str
    tests: list[ScanMatch] = Field(
        default_factory=list,
        description=f"Places in tests, which never count on their own; at most {MAX_MATCHES}.",
    )
    docs: list[ScanMatch] = Field(
        default_factory=list,
        description=f"Mentions in documentation, which never count; at most {MAX_DOC_MENTIONS}.",
    )


class ScanProblem(BaseModel):
    """A catalog entry that could not be used."""

    id: str | None = None
    provider: str | None = None
    problem: str


class ScanReport(BaseModel):
    """What ``tiltmeter scan`` found in one folder."""

    path: str
    git: bool = Field(description="The folder is in a git checkout.")
    catalog: str = Field(description="The catalog read: the bundled one, a file or a URL.")
    bundled: bool
    files: int = Field(default=0, description="Files searched.")
    entries: int = Field(default=0, description="Valid entries in the catalog.")
    error: str | None = Field(default=None, description="Why the catalog could not be read.")
    affected: list[ScanFinding] = Field(default_factory=list)
    unaffected: list[ScanMiss] = Field(default_factory=list)
    rejected: list[ScanProblem] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Catalog:
    """The valid entries of a catalog, and the ones that were not."""

    source: str
    bundled: bool
    entries: Sequence[LoadedNotice]
    rejected: Sequence[ScanProblem]


@dataclass(frozen=True, slots=True)
class Folder:
    """The files to scan, relative to ``root``, and whether git lists them."""

    root: Path
    files: Sequence[str]
    git: bool


def headline(summary: str) -> str:
    """A summary's first sentence, on one line."""
    return printable(_SENTENCE_END.split(summary, maxsplit=1)[0], _HEADLINE_CHARS)


async def read_catalog(reader: NoticeReader, source: str | None = None) -> Catalog:
    """Every valid entry of the bundled catalog or, with ``source``, of the feed in that file
    or at that https URL. Signed entries have their signature checked; an invalid entry is
    rejected on its own. Raises :class:`NoticeError` when the catalog cannot be read."""
    items = bundled_feed() if source is None else await reader.feed(source)
    location = CATALOG_LOCATION if source is None else source
    entries: list[LoadedNotice] = []
    rejected: list[ScanProblem] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        provider, notice_id = _label(item, "provider"), _label(item, "id")
        try:
            loaded = await reader.check(item, f"{location}#{notice_id}")
        except NoticeError as exc:
            problem = printable(str(exc))
            rejected.append(ScanProblem(id=notice_id, provider=provider, problem=problem))
            continue
        key = (loaded.notice.provider, loaded.notice.id)
        if key in seen:
            rejected.append(ScanProblem(id=notice_id, provider=provider, problem="listed twice"))
            continue
        seen.add(key)
        entries.append(loaded)
    return Catalog(location, source is None, entries, rejected)


def read_folder(path: Path) -> Folder:
    """The files under ``path``: what git tracks there plus new files it does not ignore, or,
    outside git, every file but installed or generated code and tools' state."""
    git = Git(path)
    try:
        inside = git.run("rev-parse", "--is-inside-work-tree").strip() == "true"
        listed = git.run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except FixError:  # not a git checkout, or no git at all
        return Folder(path, _walk(path), git=False)
    if not inside:
        return Folder(path, _walk(path), git=False)
    return Folder(path, sorted({name for name in listed.split("\0") if name}), git=True)


def _walk(root: Path) -> list[str]:
    found: list[str] = []
    for folder, dirs, names in os.walk(root):
        here = PurePosixPath(Path(folder).relative_to(root).as_posix())
        dirs[:] = sorted(d for d in dirs if d not in _NOT_WALKED and is_ours(here / d / "x"))
        for name in sorted(names):
            found.append(str(here / name))
            if len(found) > MAX_FILES:
                return found
    return found


def scan(folder: Folder, catalog: Catalog, *, label: str) -> ScanReport:
    """Search the folder for every entry of the catalog. ``label`` is the folder as the
    person running the scan named it, for the report and the fix commands.

    Code decides: tests, which feed code made-up data more often than they call the API,
    and docs are searched apart, and only listed."""
    index = RepoIndex.read(folder.root, [name for name in folder.files if not is_test(name)])
    tests = RepoIndex.read(folder.root, [name for name in folder.files if is_test(name)])
    affected: list[ScanFinding] = []
    unaffected: list[ScanMiss] = []
    for loaded in catalog.entries:
        notice = loaded.notice
        impact = assess(index, notice)
        in_tests = _surest(tests, notice)[:MAX_MATCHES]
        if not impact.affected:
            docs = mentions_in_docs(index, notice)[:MAX_DOC_MENTIONS]
            unaffected.append(
                ScanMiss(
                    id=notice.id,
                    provider=notice.provider,
                    headline=headline(notice.summary),
                    reason=impact.reason,
                    tests=[_match(place, notice) for place in in_tests],
                    docs=[_match(place, notice) for place in docs],
                )
            )
            continue
        places = _surest(index, notice, impact)
        affected.append(
            ScanFinding(
                id=notice.id,
                provider=notice.provider,
                headline=headline(notice.summary),
                summary=notice.summary,
                published_at=notice.published_at,
                effective_at=notice.effective_at,
                signed=loaded.verified,
                drafted=catalog.bundled,
                reason=impact.reason,
                matches=[_match(place, notice) for place in places[:MAX_MATCHES]],
                count=len(places),
                files=len({place.file for place in places}),
                tests=[_match(place, notice) for place in in_tests],
                fix_command=fix_command(loaded, catalog, label),
            )
        )
    matched = sorted({match.file for finding in affected for match in finding.matches})
    unused = set(unreferenced(folder.root, folder.files, matched))
    for finding in affected:
        for match in finding.matches:
            match.possibly_unused = match.file in unused
    return ScanReport(
        path=label,
        git=folder.git,
        catalog=catalog.source,
        bundled=catalog.bundled,
        files=len(index.texts) + len(tests.texts),
        entries=len(catalog.entries),
        affected=affected,
        unaffected=unaffected,
        rejected=list(catalog.rejected),
    )


def _surest(index: RepoIndex, notice: ApiChangeNotice, impact: Impact | None = None) -> list[Place]:
    """The places behind the impact (assessed here when not given), without the lines that
    only name the API when others say more."""
    impact = impact or assess(index, notice)
    if not impact.affected:
        return []
    places = locate(index, notice, impact)
    return [place for place in places if place.kind != "api"] or places


def is_test(name: str) -> bool:
    """Whether a file is a test, a test fixture or test data, by its folder or its name."""
    path = PurePosixPath(name)
    for folder in path.parts[:-1]:
        lowered = folder.lower()
        if lowered in _TEST_FOLDERS or lowered.endswith(_TEST_FOLDER_ENDINGS):
            return True
        if folder.endswith(("Tests", "Specs")):  # UnitTests, Connectors.OpenAI.UnitTests
            return True
    base = path.name
    return any(fnmatchcase(base.lower(), glob) for glob in _TEST_NAMES) or any(
        fnmatchcase(base, glob) for glob in _TEST_CLASS_NAMES
    )


def fix_command(loaded: LoadedNotice, catalog: Catalog, repo: str) -> str | None:
    """``tiltmeter fix`` for one entry. None for an unsigned entry of a catalog behind a
    URL, since only signed notices are read from URLs."""
    notice = loaded.notice
    if catalog.bundled:
        source = notice.id
    elif catalog.source.startswith("https://") and not loaded.verified:
        return None
    else:
        source = f"{catalog.source}#{notice.id}"
    command = ["tiltmeter", "fix", "--notice", source]
    if not loaded.verified:
        command.append("--allow-unsigned")
    return shell_join([*command, "--repo", repo])


def shell_join(args: Sequence[str]) -> str:
    """A command line for the shell it will be pasted in. POSIX quoting would wrap a Windows
    path in single quotes, which cmd and PowerShell don't read as quotes."""
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _match(place: Place, notice: ApiChangeNotice) -> ScanMatch:
    return ScanMatch(
        file=place.file,
        line=place.line,
        text=printable(place.text, _LINE_CHARS),
        kind=place.kind,
        term=place.term,
        detail=_detail(place, notice),
    )


def _detail(place: Place, notice: ApiChangeNotice) -> str:
    """What the change does to what the line spells, from the notice's own fields."""
    for endpoint in notice.endpoints:
        for change in endpoint.changes:
            if _about(change, place, endpoint.method, endpoint.url):
                return _what(change)
    return "names the API"


def _about(change: NoticeChange, place: Place, method: str, url: str) -> bool:
    match place.kind:
        case "value":
            return change.kind is ChangeKind.VALUE_RETIRED and change.before == place.term
        case "endpoint":
            return change.kind is ChangeKind.ENDPOINT_REMOVED and (
                place.term == f"{method} {urlsplit(url).path}"
            )
        case "field":
            return change.kind is not ChangeKind.VALUE_RETIRED and (
                field_name(change.path) == place.term
            )
        case _:
            return False


def _what(change: NoticeChange) -> str:
    match change.kind:
        case ChangeKind.VALUE_RETIRED:
            return f"retired; use {change.replaced_by}" if change.replaced_by else "retired"
        case ChangeKind.ENDPOINT_REMOVED if change.moved_to is not None:
            moved = change.moved_to
            return f"removed; moved to {moved.method} {urlsplit(moved.url).path}"
        case ChangeKind.ENDPOINT_REMOVED:
            return "removed"
        case ChangeKind.FIELD_REMOVED if change.renamed_to is not None:
            same = field_name(change.renamed_to) == field_name(change.path)
            return f"{'moved' if same else 'renamed'} to {change.renamed_to}"
        case ChangeKind.FIELD_REMOVED:
            return "no longer returned"
        case ChangeKind.TYPE_CHANGED:
            return f"now {change.after or 'another type'}, was {change.before or 'unknown'}"
        case ChangeKind.NULLABLE_INTRODUCED:
            return "can now be null"
        case ChangeKind.FIELD_NOW_OPTIONAL:
            return "now missing from some responses"
        case ChangeKind.ENUM_VALUE_ADDED:
            return f"new values: {change.after}" if change.after else "new values"
        case _:
            return change.kind.value


def dump(report: ScanReport, *, show_all: bool) -> dict[str, Any]:
    """The report as JSON, with the entries that do not affect the folder only with
    ``show_all``, as the terminal shows it."""
    return report.model_dump(mode="json", exclude=None if show_all else {"unaffected"})
