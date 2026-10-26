"""Does a repository use what a notice changes? A plain text search, before any agent runs.

Running an agent in every repository for every notice is slow, costs money, and opens
pull requests nobody needs. So Tiltmeter looks first, without an LLM:

1. Anchors: tracked files that mention one of the notice's hosts or endpoint paths, and
   dependency manifests that list one of its SDKs. Paths alone are not enough, since
   another vendor's API may use the same ones: the host or an SDK must appear somewhere.
2. Fields: the old names of the changed fields (also in camelCase and PascalCase), in an
   anchor file or in a file under the folder of one.

No anchor, or anchors but none of the fields near them, means not affected. When it is
affected, the matching files are where the agent starts. Paths in ``fix.ignore`` and
symbolic links are never read, and neither are binaries, large files, docs or lock files.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from tiltmeter.fix.ignore import is_ignored
from tiltmeter.fix.request import field_name, is_target
from tiltmeter.notices.models import ApiChangeNotice, Impact, SdkName

MAX_FILE_BYTES: Final = 1_000_000
MAX_FILES: Final = 20_000
MAX_LISTED: Final = 20

MANIFESTS: Final[Mapping[str, tuple[str, ...]]] = {
    "pypi": ("*requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"),
    "npm": ("package.json",),
    "go": ("go.mod",),
    "maven": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "nuget": ("*.csproj", "*.fsproj", "*.vbproj", "packages.config", "Directory.Packages.props"),
    "rubygems": ("Gemfile", "*.gemspec"),
    "cargo": ("Cargo.toml",),
    "packagist": ("composer.json",),
}
_DOCS: Final = frozenset({".md", ".markdown", ".rst", ".adoc", ".txt"})
# Tiltmeter's own config names the APIs it follows, which is no use of them.
_OWN_FILES: Final = frozenset({"tiltmeter.yaml", "tiltmeter.yml"})
_LOCK_FILES: Final = frozenset(
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
_VERSION: Final = re.compile(r"v\d+(?:\.\d+)*", re.IGNORECASE)


def path_anchors(url: str) -> list[str]:
    """The literal start of the endpoint's path, with and without a leading version."""
    literal = urlsplit(url).path.split("{", 1)[0]
    segments = [segment for segment in literal.split("/") if segment]
    if not segments or all(_VERSION.fullmatch(segment) for segment in segments):
        return []
    anchors = ["/" + "/".join(segments)]
    if _VERSION.fullmatch(segments[0]):
        anchors.append("/" + "/".join(segments[1:]))
    return anchors


def name_variants(name: str) -> list[str]:
    """A field name as JSON, camelCase and PascalCase code tend to spell it."""
    words = [word for word in re.split(r"[_\-\s]+", name) if word]
    variants = [name]
    if len(words) > 1:
        camel = words[0] + "".join(word[:1].upper() + word[1:] for word in words[1:])
        variants += [camel, camel[:1].upper() + camel[1:]]
    else:
        variants.append(name[:1].upper() + name[1:])
    return list(dict.fromkeys(variants))


def _alternatives(texts: Iterable[str]) -> str:
    return "|".join(re.escape(text) for text in sorted(set(texts), key=len, reverse=True))


def field_pattern(name: str) -> re.Pattern[str]:
    """A field name read as a key or an attribute, but not as part of a longer name."""
    return re.compile(
        rf"(?<![A-Za-z0-9_$])(?:{_alternatives(name_variants(name))})(?![A-Za-z0-9_$])"
    )


def sdk_pattern(sdk: SdkName) -> re.Pattern[str]:
    """How the SDK appears in its ecosystem's manifests."""
    name = sdk.name
    if sdk.ecosystem == "maven":
        name = name.rsplit(":", 1)[-1]
    if sdk.ecosystem == "pypi":
        # Package names treat -, _ and . alike, in any case.
        body = "[-_.]".join(re.escape(part) for part in re.split(r"[-_.]+", name) if part)
        return re.compile(rf"(?<![A-Za-z0-9_.\-]){body}(?![A-Za-z0-9_.\-])", re.IGNORECASE)
    flags = re.IGNORECASE if sdk.ecosystem == "nuget" else 0
    return re.compile(rf"(?<![A-Za-z0-9_@/.\-]){re.escape(name)}(?![A-Za-z0-9_/\-])", flags)


def _read_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _listing(items: Sequence[str], word: str = "or") -> str:
    shown = list(items[:4])
    if len(items) > 4:
        shown.append(f"{len(items) - 4} more")
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} {word} {shown[-1]}"


def _under(name: str, folders: set[PurePosixPath]) -> bool:
    path = PurePosixPath(name)
    return any(parent in folders for parent in path.parents)


def _documentation(name: str) -> bool:
    base = PurePosixPath(name).name
    return PurePosixPath(base).suffix.lower() in _DOCS or base.upper().startswith(
        ("CHANGELOG", "LICENSE")
    )


@dataclass(frozen=True, slots=True)
class RepoIndex:
    """A repository's tracked text files, read once for every notice.

    Lock files, Tiltmeter's own config and state, binaries, large files, links and
    ``fix.ignore`` paths are left out. Docs
    are kept apart: they only count when they are dependency manifests.
    """

    texts: Mapping[str, str]
    docs: frozenset[str] = frozenset()
    _paths: list[str] = field(default_factory=list, compare=False)
    _calls: frozenset[tuple[str, str]] = field(default_factory=frozenset, compare=False)

    @classmethod
    def read(cls, root: Path, files: Sequence[str], *, ignore: Sequence[str] = ()) -> RepoIndex:
        texts: dict[str, str] = {}
        docs: set[str] = set()
        for name in files[:MAX_FILES]:
            path = PurePosixPath(name)
            skipped = path.name in _LOCK_FILES or path.name in _OWN_FILES
            if skipped or ".tiltmeter" in path.parts or is_ignored(name, ignore):
                continue
            text = _read_text(root / name)
            if text is None:
                continue
            texts[name] = text
            if _documentation(name):
                docs.add(name)
        code = [text for name, text in texts.items() if name not in docs]
        paths = sorted({match for text in code for match in _PATH_TOKEN.findall(text)})
        calls = frozenset(
            (_resource(name), verb.lower())
            for text in code
            for name, verb in _SDK_CALL.findall(text)
        )
        return cls(texts, frozenset(docs), paths, calls)

    def code(self) -> Iterator[tuple[str, str]]:
        """Every file that is not documentation, with its text."""
        return ((name, text) for name, text in self.texts.items() if name not in self.docs)

    def mentions(self, text: str) -> bool:
        lowered = text.lower()
        return any(lowered in content.lower() for _, content in self.code())

    def uses_path(self, url: str, method: str | None = None) -> int:
        """2 when code spells the endpoint's path or calls it through an SDK, 1 when it
        spells only the path's start, 0 otherwise."""
        return self.uses([Candidate(url, (), method=method)])[0]

    def uses(self, candidates: Sequence[Candidate]) -> list[int]:
        """:meth:`uses_path` for several endpoints at once. A path in code that spells one
        of them is not also the start of the others: ``/repos/{owner}/{repo}`` names that
        endpoint, not every endpoint below it."""
        patterns = [_template_pattern(candidate.url) for candidate in candidates]
        spelled = {
            path
            for path in self._paths
            if any(pattern is not None and pattern.search(path) for pattern in patterns)
        }
        loose = [path for path in self._paths if path not in spelled]
        found = []
        for candidate, pattern in zip(candidates, patterns, strict=True):
            named = pattern is not None and any(pattern.search(path) for path in spelled)
            if named or self._calls & _call_shapes(candidate):
                found.append(2)
            elif any(anchor in path for anchor in path_anchors(candidate.url) for path in loose):
                found.append(1)
            else:
                found.append(0)
        return found


# Path-like strings in code: "/v1/customers/" + id, f"/v1/customers/{customer_id}", ...
_PATH_TOKEN: Final = re.compile(r"(?:/[A-Za-z0-9_\-.{}$:]+)+/?")


def _template_pattern(url: str) -> re.Pattern[str] | None:
    """The endpoint's path as a regex: ``{id}`` matches any one segment. Only a host may
    come before it, so ``/repos/{owner}/{repo}/issues`` does not spell ``/issues``."""
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    literal = [segment for segment in segments if "{" not in segment]
    if not literal or all(_VERSION.fullmatch(segment) for segment in literal):
        return None
    parts = [r"[^/]+" if "{" in segment else re.escape(segment) for segment in segments]
    if _VERSION.fullmatch(segments[0]):
        body = rf"(?:/{parts[0]})?" + "".join(f"/{part}" for part in parts[1:])
    else:
        body = "".join(f"/{part}" for part in parts)
    return re.compile(rf"^(?:/[^/]*\.[^/]*)*{body}/?$")


# Calls through an SDK, as most SDKs name them: stripe.Subscription.retrieve(...),
# client.messages.create(...), octokit.rest.pulls.list(...).
_SDK_CALL: Final = re.compile(
    r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
    r"(retrieve|get|list|create|new|update|modify|delete|del)\s*\(",
    re.IGNORECASE,
)
_VERBS: Final[Mapping[str, tuple[frozenset[str], bool]]] = {
    # verb: the HTTP methods it sends, and whether the path ends with the object's id
    "retrieve": (frozenset({"GET"}), True),
    "get": (frozenset({"GET"}), True),
    "list": (frozenset({"GET"}), False),
    "create": (frozenset({"POST"}), False),
    "new": (frozenset({"POST"}), False),
    "update": (frozenset({"POST", "PATCH", "PUT"}), True),
    "modify": (frozenset({"POST", "PATCH", "PUT"}), True),
    "delete": (frozenset({"DELETE"}), True),
    "del": (frozenset({"DELETE"}), True),
}


def _resource(name: str) -> str:
    """A resource name in one spelling: Subscription, subscriptions and PaymentIntent,
    payment_intents meet."""
    word = name.lower().replace("_", "").replace("-", "")
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _call_shapes(candidate: Candidate) -> set[tuple[str, str]]:
    """The SDK calls that would reach this endpoint, as (resource, verb)."""
    segments = [segment for segment in urlsplit(candidate.url).path.split("/") if segment]
    literal = [s for s in segments if "{" not in s and not _VERSION.fullmatch(s)]
    if not literal:
        return set()
    resource = _resource(literal[-1])
    by_id = "{" in segments[-1]
    method = (candidate.method or "").upper()
    return {
        (resource, verb)
        for verb, (methods, needs_id) in _VERBS.items()
        if needs_id == by_id and (not method or method in methods)
    }


@dataclass(frozen=True, slots=True)
class Candidate:
    """An endpoint with changes, as the endpoint selection sees it."""

    url: str
    fields: tuple[str, ...]
    rank: int = 0
    method: str | None = None


def choose_endpoints(
    index: RepoIndex | None,
    candidates: Sequence[Candidate],
    *,
    near: Iterable[str] = (),
    limit: int | None = None,
) -> list[int]:
    """The candidates the repository most likely uses, as positions, best first.

    Paths spelled in code come first, then the start of a path. When no path shows up
    (code going through an SDK, or a base URL in the environment), the changed field
    names found near the API's uses decide, rarer names weighing more. Without any of
    that, the most severe changes come first.
    """
    order = sorted(range(len(candidates)), key=lambda i: (-candidates[i].rank, i))
    if index is None:
        return order[:limit]
    levels = index.uses(candidates)
    found = {i: levels[i] for i in order}
    chosen = sorted((i for i in order if found[i]), key=lambda i: -found[i])
    if not chosen:
        names = set(near)
        counts = Counter(name for c in candidates for name in set(c.fields))
        scores = {
            i: sum(1 / counts[n] for n in set(candidates[i].fields) if n in names) for i in order
        }
        chosen = sorted((i for i in order if scores[i] > 0), key=lambda i: -scores[i])
    return (chosen or order)[:limit]


def fields_near(
    index: RepoIndex, hosts: Sequence[str], sdks: Sequence[SdkName], fields: Iterable[str]
) -> set[str]:
    """Which field names appear near the API's hosts or SDKs anywhere in the repository."""
    impact = _search(index, hosts=hosts, paths=(), sdks=sdks, fields=list(dict.fromkeys(fields)))
    return set(impact.fields) if impact.affected else set()


def assess(index: RepoIndex, notice: ApiChangeNotice, *, sdks: Sequence[SdkName] = ()) -> Impact:
    """Whether the repository uses what the notice changes, and which of its endpoints."""
    changes = [change for endpoint in notice.endpoints for change in endpoint.changes]
    targets = [change for change in changes if is_target(change.kind)]
    if not targets:
        return Impact(affected=False, reason="the notice has no changes code has to follow")
    fields = list(dict.fromkeys(name for t in targets if (name := field_name(t.path))))
    paths = list(dict.fromkeys(a for e in notice.endpoints for a in path_anchors(e.url)))
    names = [*notice.sdks, *(sdk for sdk in sdks if sdk.name not in {s.name for s in notice.sdks})]
    impact = _search(index, hosts=notice.hosts, paths=paths, sdks=names, fields=fields)
    if not impact.affected:
        return impact
    candidates = [
        Candidate(
            endpoint.url,
            tuple(n for c in endpoint.changes if is_target(c.kind) and (n := field_name(c.path))),
            method=endpoint.method,
        )
        for endpoint in notice.endpoints
    ]
    chosen = choose_endpoints(index, candidates, near=impact.fields)
    return impact.model_copy(update={"endpoints": chosen})


def _search(
    index: RepoIndex,
    *,
    hosts: Sequence[str],
    paths: Sequence[str],
    sdks: Sequence[SdkName],
    fields: Sequence[str],
) -> Impact:
    field_patterns = {name: field_pattern(name) for name in fields}
    texts = list(dict.fromkeys([*hosts, *paths]))
    by_name = {sdk.name: (sdk, sdk_pattern(sdk)) for sdk in sdks}

    anchor_hits: dict[str, set[str]] = {}
    field_hits: dict[str, set[str]] = {}
    for name, text in index.texts.items():
        base = PurePosixPath(name).name
        manifest_of = [
            sdk.name
            for sdk, _ in by_name.values()
            if any(fnmatchcase(base, pattern) for pattern in MANIFESTS[sdk.ecosystem])
        ]
        if name in index.docs and not manifest_of:
            continue
        lowered = text.lower()
        found = {term for term in texts if term.lower() in lowered}
        found |= {sdk for sdk in manifest_of if by_name[sdk][1].search(text)}
        if found:
            anchor_hits[name] = found
        read = {field for field, pattern in field_patterns.items() if pattern.search(text)}
        if read:
            field_hits[name] = read

    searched = [*texts, *by_name]
    if not anchor_hits:
        return Impact(affected=False, reason=f"no tracked file mentions {_listing(searched)}")
    anchors = sorted({term for hits in anchor_hits.values() for term in hits}, key=searched.index)
    identities = [*hosts, *by_name]
    if not set(anchors) & set(identities):
        return Impact(
            affected=False,
            reason=(
                f"the repository mentions {_listing(anchors, 'and')}, but never "
                f"{_listing(identities)}, so those may belong to another API"
            ),
            anchors=anchors,
        )
    folders = {PurePosixPath(name).parent for name in anchor_hits}
    near = {
        name: read
        for name, read in field_hits.items()
        if name in anchor_hits or _under(name, folders)
    }
    if fields and not near:
        return Impact(
            affected=False,
            reason=(
                f"the repository mentions {_listing(anchors, 'and')}, but no file near "
                f"those mentions reads {_listing(fields)}"
            ),
            anchors=anchors,
        )
    found_fields = sorted({field for read in near.values() for field in read}, key=fields.index)
    ranked = sorted(
        set(anchor_hits) | set(near),
        key=lambda name: (not (name in anchor_hits and name in near), name not in near, name),
    )
    reads = f", and code near them reads {_listing(found_fields, 'and')}" if found_fields else ""
    return Impact(
        affected=True,
        reason=f"the repository mentions {_listing(anchors, 'and')}{reads}",
        files=ranked[:MAX_LISTED],
        anchors=anchors,
        fields=found_fields,
    )
