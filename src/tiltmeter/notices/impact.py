"""Does a repository use what a notice changes? A plain text search, before any agent runs.

Running an agent in every repository for every notice is slow, costs money, and opens
pull requests nobody needs. So Tiltmeter looks first, without an LLM:

1. Anchors: tracked files that mention one of the notice's hosts or endpoint paths, and
   dependency manifests that list one of its SDKs. A path may also be quoted without its
   leading slash ("rest/api/3/search"), as code that joins it to a base URL writes it.
   Paths alone are not enough, since another vendor's API may use the same ones: the
   host, the provider's domain or an SDK must appear somewhere, unless the path is long
   enough (four segments) to be no one else's.
2. Fields: the old names of the changed fields (also in camelCase and PascalCase), in an
   anchor file or in a file under the folder of one.

A retired value, such as a model id, is its own anchor: spelled in code, it says which
API the code calls. A removed endpoint counts only where code spells its whole path (or
the repository depends on an SDK release that follows the change), so code that already
calls its replacement, which often starts the same way, is not affected.

No anchor, or anchors but none of the fields near them, means not affected. When it is
affected, the matching files are where the agent starts. Paths in ``fix.ignore`` and
symbolic links are never read, and neither are binaries, large files, docs, lock files or
change notices and feeds, which describe APIs without calling them.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Final, Literal
from urllib.parse import urlsplit

from tiltmeter.core.models import ChangeKind
from tiltmeter.fix.ignore import is_ignored
from tiltmeter.fix.request import field_name, is_target, reads_response
from tiltmeter.fix.tree import LOCK_FILES, MAX_FILES, is_ours, read_text
from tiltmeter.notices.models import (
    FEED_SPEC,
    NOTICE_SPEC,
    ApiChangeNotice,
    Impact,
    NoticeEndpoint,
    SdkName,
    https_host,
    on_domain,
)

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
_DOCS: Final = frozenset(
    {".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt", ".qmd", ".rmd", ".tex", ".bib", ".org"}
)
# Tables and pictures name models and endpoints as data, such as benchmark results.
_DATA: Final = frozenset({".csv", ".tsv", ".svg"})
# Tiltmeter's own config names the APIs it follows, which is no use of them.
_OWN_FILES: Final = frozenset({"tiltmeter.yaml", "tiltmeter.yml"})
_VERSION: Final = re.compile(r"v\d+(?:\.\d+)*", re.IGNORECASE)
_DISTINCTIVE_SEGMENTS: Final = 4


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


def versionless_anchors(url: str) -> list[str]:
    """The anchors of :func:`path_anchors` that leave the version out, like ``/customers``.
    They are short enough to be an app's own routes, so they count only next to the API."""
    return path_anchors(url)[1:]


def import_pattern(sdk: SdkName) -> re.Pattern[str] | None:
    """How code imports the SDK, in the ecosystems whose imports name the package."""
    name = re.escape(sdk.name)
    match sdk.ecosystem:
        case "npm":
            return re.compile(
                rf"""(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*|\bimport\s+)['"]{name}(?:/[^'"]*)?['"]"""
            )
        case "pypi":
            module = re.escape(re.sub(r"[-.]", "_", sdk.name.lower()))
            return re.compile(
                rf"^\s*(?:import\s+{module}\b|from\s+{module}(?:\.|\s+import\b))", re.M
            )
        case "go":
            return re.compile(rf'"{name}(?:/[^"]*)?"')
        case "rubygems":
            return re.compile(rf"""\brequire\s*\(?\s*['"]{name}['"]""")
        case "cargo":
            crate = re.escape(sdk.name.replace("-", "_"))
            return re.compile(rf"\b(?:use|extern\s+crate)\s+{crate}\b")
        case _:
            return None


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


def _finds(pattern: re.Pattern[str], name: str, text: str) -> bool:
    """``field_pattern(name).search(text)``, after a quick look for its spellings: a pattern
    that opens with a lookbehind is tried at every position of a large file."""
    return any(variant in text for variant in name_variants(name)) and bool(pattern.search(text))


def value_pattern(value: str) -> re.Pattern[str]:
    """A retired value as code spells it, quoted or after a prefix such as ``models/`` or
    ``anthropic/``, but not inside a longer id (``anthropic.claude-3-opus-20240229-v1:0`` on
    Bedrock, ``claude-2.1`` for ``claude-2``) or a file name (``gpt-4.txt``). A short value
    of letters and digits only, such as ``o1``, is a word too often: it counts quoted whole."""
    if re.fullmatch(r"[A-Za-z0-9]{1,6}", value):
        return re.compile(rf"(?<=[\"'`/]){re.escape(value)}(?=[\"'`])")
    return re.compile(rf"(?<![A-Za-z0-9_.\-]){re.escape(value)}(?![A-Za-z0-9_\-@]|\.\w)")


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


def _listing(items: Sequence[str], word: str = "or") -> str:
    shown = list(items[:4])
    if len(items) > 4:
        shown.append(f"{len(items) - 4} more")
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} {word} {shown[-1]}"


def _literal_segments(url: str) -> int:
    """How many segments of the URL's path are neither parameters nor the version."""
    segments = [part for part in urlsplit(url).path.split("/") if part]
    return sum(1 for part in segments if "{" not in part and not _VERSION.fullmatch(part))


def _spelled(term: str, lowered: str) -> bool:
    """Whether lowercased text spells a host or a path. A path of two segments or more
    may also come without its leading slash, quoted ("rest/api/3/search") or right after
    an interpolation that holds the slash ({separator}rest/api/3/search)."""
    term = term.lower()
    if term in lowered:
        return True
    rest = term[1:]
    return term.startswith("/") and "/" in rest and any(f"{q}{rest}" in lowered for q in "\"'`}")


def _under(name: str, folders: set[PurePosixPath]) -> bool:
    path = PurePosixPath(name)
    return any(parent in folders for parent in path.parents)


def _documentation(name: str) -> bool:
    """Docs, data tables, and files such as ``.dockerignore`` that list paths rather than
    call them."""
    base = PurePosixPath(name).name
    if PurePosixPath(base).suffix.lower() in _DOCS | _DATA or fnmatchcase(base, ".*ignore"):
        return True
    return base.upper().startswith(("CHANGELOG", "LICENSE"))


@dataclass(frozen=True, slots=True)
class RepoIndex:
    """A repository's tracked text files, read once for every notice.

    Lock files, Tiltmeter's own config and state, change notices and feeds (such as
    the catalog ``tiltmeter scan`` reads), installed or generated code (``node_modules``,
    ``.venv``, ``.next`` and the like), agents' worktree copies, binaries, large files,
    links and ``fix.ignore`` paths are left out. Docs are kept apart: they only count when
    they are dependency manifests.
    """

    texts: Mapping[str, str]
    docs: frozenset[str] = frozenset()
    _paths: Mapping[str, frozenset[str]] = field(default_factory=dict, compare=False)
    _calls: Mapping[str, frozenset[_Call]] = field(default_factory=dict, compare=False)
    _spoken: Mapping[str, str] = field(default_factory=dict, compare=False)

    @classmethod
    def read(cls, root: Path, files: Sequence[str], *, ignore: Sequence[str] = ()) -> RepoIndex:
        texts: dict[str, str] = {}
        docs: set[str] = set()
        for name in [name for name in files if is_ours(PurePosixPath(name))][:MAX_FILES]:
            path = PurePosixPath(name)
            skipped = path.name in LOCK_FILES or path.name in _OWN_FILES
            if skipped or not is_ours(path) or is_ignored(name, ignore):
                continue
            text = read_text(root / name)
            if text is None or _notice_document(name, text):
                continue
            texts[name] = text
            if _documentation(name):
                docs.add(name)
        spoken = {name: code_only(name, text) for name, text in texts.items() if name not in docs}
        paths = {name: _path_tokens(text) for name, text in spoken.items()}
        calls = {name: frozenset(_calls_in(text)) for name, text in spoken.items()}
        return cls(texts, frozenset(docs), paths, calls, spoken)

    def code(self) -> Iterator[tuple[str, str]]:
        """Every file that is not documentation, with its text."""
        return ((name, text) for name, text in self.texts.items() if name not in self.docs)

    def spoken(self, name: str) -> str:
        """A code file's text without its comment lines and docstrings (see
        :func:`code_only`): what the code does, not what it says about itself."""
        return self._spoken.get(name, self.texts.get(name, ""))

    def mentions(self, text: str) -> bool:
        lowered = text.lower()
        return any(lowered in content.lower() for _, content in self.code())

    def api_files(self, hosts: Sequence[str], sdks: Sequence[SdkName]) -> frozenset[str]:
        """The code files that mention one of the API's hosts or import one of its SDKs."""
        lowered = [host.lower() for host in hosts]
        imports = [pattern for sdk in sdks if (pattern := import_pattern(sdk)) is not None]
        return frozenset(
            name
            for name, text in self.code()
            if any(host in text.lower() for host in lowered)
            or any(pattern.search(text) for pattern in imports)
        )

    def calling(self, gone: Gone, api_files: Collection[str]) -> list[str]:
        """The code files that call a removed endpoint: where code spells its whole path,
        or, in ``api_files``, calls it through an SDK."""
        return [name for name in self._paths if self._calls_gone(gone, name, name in api_files)]

    def _calls_gone(self, gone: Gone, name: str, near: bool) -> bool:
        if gone.only_with_host:
            return gone.old_host in self.spoken(name).lower()
        # Code that keeps the host and version in its configuration spells the rest of the
        # path anywhere: "${baseURL}/artists/${id}/top-tracks". Two literal segments are no
        # app route of its own, the way /markets could be; /v1/markets, with its version,
        # is not either, but a bare /models/{model} is.
        if not (near or gone.segments >= 2 or gone.versioned):
            return False
        versionless = near or gone.segments >= 2
        if any(gone.spelled(path, versionless=versionless) for path in self._paths[name]):
            return True
        return near and gone.called(self._calls.get(name, frozenset()))

    def uses_path(self, url: str, method: str | None = None) -> int:
        """2 when code spells the endpoint's path or calls it through an SDK, 1 when it
        spells only the path's start, 0 otherwise."""
        return self.uses([Candidate(url, (), method=method)])[0]

    def uses(
        self, candidates: Sequence[Candidate], *, api_files: Collection[str] | None = None
    ) -> list[int]:
        """:meth:`uses_path` for several endpoints at once. A path in code that spells one
        of them is not also the start of the others: ``/repos/{owner}/{repo}`` names that
        endpoint, not every endpoint below it. A path without the API's version, such as
        ``/account`` for ``/v1/account``, counts only in ``api_files``, when they are given.
        """
        everywhere = frozenset().union(*self._paths.values())
        if api_files is None:
            near_api = everywhere
        else:
            near_api = frozenset().union(
                *(self._paths.get(name, frozenset()) for name in api_files)
            )
        strict = [_template_pattern(c.url, versionless=False) for c in candidates]
        loose = [_template_pattern(c.url, versionless=True) for c in candidates]

        def spells(i: int, path: str) -> bool:
            if (pattern := strict[i]) is not None and pattern.search(path):
                return True
            return (
                path in near_api
                and (pattern := loose[i]) is not None
                and bool(pattern.search(path))
            )

        spelled = {
            path for path in everywhere if any(spells(i, path) for i in range(len(candidates)))
        }
        rest, rest_near = everywhere - spelled, near_api - spelled
        found = []
        for i, candidate in enumerate(candidates):
            shape = _call_shape(candidate)
            called = shape is not None and any(
                _reaches(call, shape) for calls in self._calls.values() for call in calls
            )
            if called or any(spells(i, path) for path in spelled):
                found.append(2)
                continue
            anchors = path_anchors(candidate.url)[:1]
            short = versionless_anchors(candidate.url)
            started = any(anchor in path for anchor in anchors for path in rest) or any(
                anchor in path for anchor in short for path in rest_near
            )
            found.append(1 if started else 0)
        return found


# Path-like strings in code: "/v1/customers/" + id, f"/v1/customers/{customer_id}", ...
_PATH_TOKEN: Final = re.compile(r"(?:/[A-Za-z0-9_\-.{}$:]+)+/?")
# The same without the leading slash, as code that joins it to a base URL writes it:
# quoted, "rest/api/3/search", or right after an interpolation that holds the slash,
# $"{endpoint}{separator}pipeline/feature-extraction/{model}". Two segments at least, so
# that a plain key is no path.
_QUOTED_PATH: Final = re.compile(
    r"""(?<=["'`}])[A-Za-z0-9_\-.]+(?:/[A-Za-z0-9_\-.{}$:]+)+(?=/?["'`?])"""
)


def _notice_document(name: str, text: str) -> bool:
    """Whether a file is a change notice or a feed: it names APIs, and calls none."""
    head = text[:2000]
    return name.lower().endswith(".json") and (NOTICE_SPEC in head or FEED_SPEC in head)


# A line that holds nothing but a comment: #, //, /* and the * of a block comment, -- in SQL
# and Lua. # followed by a preprocessor word or [ is code (#define, #[test]). Lines that open
# a markup comment are dropped in code_only.
_COMMENT_LINE: Final = re.compile(
    r"^[ \t]*(?:#(?!!|\[|(?:define|include|if|ifdef|ifndef|elif|else|endif|pragma|import"
    r"|region|endregion|undef|error|warning|line)\b)|//|/\*|\*(?=[ \t/]|$)|--[ \t]).*$",
    re.MULTILINE,
)
# A Python string that stands alone as a statement: a docstring, or code turned off.
_BARE_STRING: Final = re.compile(r"""^[ \t]*[rRuU]?("{3}|'{3})""")


def code_only(name: str, text: str) -> str:
    """The text without lines that are only comments and, in Python, without docstrings,
    line for line. A retired model named in a comment or a docstring is not sent."""
    text = _COMMENT_LINE.sub("", text)
    text = "\n".join(
        "" if line.lstrip(" \t").startswith("<!--") else line for line in text.split("\n")
    )
    if not name.endswith((".py", ".pyi")):
        return text
    lines = text.split("\n")
    start = 0
    while start < len(lines):
        opened = _BARE_STRING.match(lines[start])
        if opened is None:
            start += 1
            continue
        quote = opened.group(1)
        end = start
        if quote not in lines[start][opened.end() :]:
            end = next((i for i in range(start + 1, len(lines)) if quote in lines[i]), start)
        for i in range(start, end + 1):
            lines[i] = ""
        start = end + 1
    return "\n".join(lines)


type _Call = tuple[tuple[str, ...], str]


def _calls_in(text: str) -> Iterator[_Call]:
    """The SDK calls in code, as the resource names of the chain and the verb."""
    for chain, verb in _SDK_CALL.findall(text):
        parts = re.split(r"\s*\.\s*", chain)
        yield tuple(_resource(part.rstrip("() \t")) for part in parts), verb.lower()


def _path_tokens(text: str) -> frozenset[str]:
    return frozenset(_PATH_TOKEN.findall(text)) | {f"/{p}" for p in _QUOTED_PATH.findall(text)}


def _template_pattern(url: str, *, versionless: bool = True) -> re.Pattern[str] | None:
    """The endpoint's path as a regex: ``{id}`` matches any one segment. Only a host may
    come before it (the ``hosts`` group), so ``/repos/{owner}/{repo}/issues`` does not
    spell ``/issues``. With ``versionless``, a leading version such as ``/v1`` may be left
    out."""
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    literal = [segment for segment in segments if "{" not in segment]
    if not literal or all(_VERSION.fullmatch(segment) for segment in literal):
        return None
    parts = [r"[^/]+" if "{" in segment else re.escape(segment) for segment in segments]
    if versionless and _VERSION.fullmatch(segments[0]):
        body = rf"(?:/{parts[0]})?" + "".join(f"/{part}" for part in parts[1:])
    else:
        body = "".join(f"/{part}" for part in parts)
    return re.compile(rf"^(?P<hosts>(?:/[^/]*\.[^/]*)*){body}/?$")


@dataclass(frozen=True, slots=True)
class Gone:
    """A removed endpoint, and how code that still calls it can be told from code that
    calls its replacement or another API with the same paths (``/v1/chat/completions``).

    A host spelled in front of the path must be on the provider's domain and not the host
    the endpoint moved to. When it moved to the same path on another host, only the old
    host tells the two apart, so code that names it calls the endpoint. An SDK call counts
    only when the replacement does not look the same to the SDK.
    """

    method: str
    path: str
    domain: str
    old_host: str
    new_host: str | None
    only_with_host: bool
    segments: int
    versioned: bool
    strict: re.Pattern[str] | None
    loose: re.Pattern[str] | None
    shape: _CallShape | None

    @classmethod
    def of(cls, endpoint: NoticeEndpoint, provider: str) -> Gone:
        moved = next((c.moved_to for c in endpoint.changes if c.moved_to is not None), None)
        old_host = (https_host(endpoint.url) or "").lower()
        new_host = (https_host(moved.url) or "").lower() if moved is not None else None
        strict = _template_pattern(endpoint.url, versionless=False)
        first = next((part for part in urlsplit(endpoint.url).path.split("/") if part), "")
        shape = _call_shape(Candidate(endpoint.url, (), method=endpoint.method))
        same_path = False
        if moved is not None:
            replacement = _template_pattern(moved.url, versionless=False)
            same_path = (
                strict is not None
                and replacement is not None
                and (strict.pattern == replacement.pattern)
            )
            if shape == _call_shape(Candidate(moved.url, (), method=moved.method)):
                shape = None
        return cls(
            method=endpoint.method,
            path=urlsplit(endpoint.url).path,
            domain=provider.lower(),
            old_host=old_host,
            new_host=new_host if new_host != old_host else None,
            only_with_host=same_path and new_host != old_host,
            segments=_literal_segments(endpoint.url),
            versioned=bool(_VERSION.fullmatch(first)),
            strict=strict,
            loose=_template_pattern(endpoint.url, versionless=True),
            shape=shape,
        )

    @property
    def term(self) -> str:
        """How a place names it: ``GET /rest/api/3/search``."""
        return f"{self.method} {self.path}"

    def spelled(self, path: str, *, versionless: bool) -> bool:
        """Whether a path in code is this endpoint's, with a host of its API if any."""
        for pattern in (self.strict, self.loose if versionless else None):
            match = pattern.search(path) if pattern is not None else None
            if match is None:
                continue
            hosts = [part.split(":")[0].lower() for part in match["hosts"].split("/") if part]
            if not hosts or (on_domain(hosts[-1], self.domain) and hosts[-1] != self.new_host):
                return True
        return False

    def called(self, calls: Iterable[_Call]) -> bool:
        """Whether one of these SDK calls reaches the endpoint."""
        return self.shape is not None and any(_reaches(call, self.shape) for call in calls)

    def in_line(self, line: str, *, near: bool) -> bool:
        """Whether a line of code calls the endpoint, by :meth:`RepoIndex.calling`'s rules."""
        if self.only_with_host:
            return self.old_host in line.lower()
        versionless = near or self.segments >= 2
        if any(self.spelled(path, versionless=versionless) for path in _path_tokens(line)):
            return True
        return near and self.called(_calls_in(line))


# Calls through an SDK, as most SDKs name them: stripe.Subscription.retrieve(...),
# client.messages.create(...), octokit.rest.pulls.list(...).
# A step of the chain may be a call without arguments, as in getStripe().subscriptions.
_SDK_CALL: Final = re.compile(
    r"\b((?:[A-Za-z_]\w*(?:\(\s*\))?\s*\.\s*)*[A-Za-z_]\w*)\s*\.\s*"
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


@dataclass(frozen=True, slots=True)
class _CallShape:
    """What an SDK call that reaches an endpoint looks like."""

    resource: str
    namespace: str | None  # checkout in /v1/checkout/sessions, which SDKs mirror
    verbs: frozenset[str]


def _call_shape(candidate: Candidate) -> _CallShape | None:
    segments = [segment for segment in urlsplit(candidate.url).path.split("/") if segment]
    segments = [segment for segment in segments if not _VERSION.fullmatch(segment)]
    literal = [i for i, segment in enumerate(segments) if "{" not in segment]
    if not literal:
        return None
    last = literal[-1]
    namespace = segments[last - 1] if last > 0 and "{" not in segments[last - 1] else None
    by_id = "{" in segments[-1]
    method = (candidate.method or "").upper()
    verbs = frozenset(
        verb
        for verb, (methods, needs_id) in _VERBS.items()
        if needs_id == by_id and (not method or method in methods)
    )
    return _CallShape(_resource(segments[last]), _resource(namespace) if namespace else None, verbs)


def _reaches(call: tuple[tuple[str, ...], str], shape: _CallShape) -> bool:
    """Whether ``stripe.checkout.sessions.create`` reaches ``POST /v1/checkout/sessions``."""
    chain, verb = call
    if verb not in shape.verbs or chain[-1] != shape.resource:
        return False
    return shape.namespace is None or shape.namespace in chain[:-1]


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
    hosts: Sequence[str] = (),
    sdks: Sequence[SdkName] = (),
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
    api = index.api_files(hosts, sdks) if hosts or sdks else None
    levels = index.uses(candidates, api_files=api)
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
    fields = list(
        dict.fromkeys(
            name for t in targets if reads_response(t.kind) and (name := field_name(t.path))
        )
    )
    values = list(
        dict.fromkeys(t.before for t in targets if t.kind is ChangeKind.VALUE_RETIRED and t.before)
    )
    paths = list(dict.fromkeys(a for e in notice.endpoints for a in path_anchors(e.url)))
    short = {a for e in notice.endpoints for a in versionless_anchors(e.url)}
    names = [*notice.sdks, *(sdk for sdk in sdks if sdk.name not in {s.name for s in notice.sdks})]
    # The provider's domain names the API too, where each customer has a host of its own
    # (your-domain.atlassian.net).
    hosts = list(dict.fromkeys([*notice.hosts, notice.provider]))
    impact = _search(
        index, hosts=hosts, paths=paths, short=short, sdks=names, fields=fields, values=values
    )
    if not impact.affected:
        return impact
    candidates = [
        Candidate(
            endpoint.url,
            tuple(
                n
                for c in endpoint.changes
                if is_target(c.kind) and reads_response(c.kind) and (n := field_name(c.path))
            ),
            method=endpoint.method,
        )
        for endpoint in notice.endpoints
    ]
    chosen = choose_endpoints(index, candidates, near=impact.fields, hosts=hosts, sdks=names)
    removed = [
        i
        for i, endpoint in enumerate(notice.endpoints)
        if any(c.kind is ChangeKind.ENDPOINT_REMOVED for c in endpoint.changes)
    ]
    if removed and not set(impact.anchors) & {sdk.name for sdk in notice.sdks}:
        api = index.api_files(hosts, names)
        gone = {i: Gone.of(notice.endpoints[i], notice.provider) for i in removed}
        called = {i for i in removed if index.calling(gone[i], api)}
        # A call of an endpoint that is gone breaks for certain: those come first.
        chosen = [i for i in removed if i in called] + [i for i in chosen if i not in removed]
        if not chosen:
            terms = [gone[i].term for i in removed]
            return Impact(
                affected=False,
                reason=f"the repository never calls {_listing(terms)}, which the notice removes",
                anchors=impact.anchors,
            )
    return impact.model_copy(update={"endpoints": chosen})


def _search(
    index: RepoIndex,
    *,
    hosts: Sequence[str],
    paths: Sequence[str],
    sdks: Sequence[SdkName],
    fields: Sequence[str],
    short: Collection[str] = (),
    values: Sequence[str] = (),
) -> Impact:
    """``short`` are the paths without their version: they count only in a file that also
    mentions a host or imports an SDK. ``values`` are retired values, such as model ids:
    spelled in code, each is an anchor that names the API on its own."""
    field_patterns = {name: field_pattern(name) for name in fields}
    value_patterns = {value: value_pattern(value) for value in values}
    texts = list(dict.fromkeys([*hosts, *paths]))
    by_name = {sdk.name: (sdk, sdk_pattern(sdk)) for sdk in sdks}
    imports = {sdk.name: import_pattern(sdk) for sdk in sdks}
    manifests: set[str] = set()

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
        found = {term for term in texts if term not in short and _spelled(term, lowered)}
        found |= {sdk for sdk in manifest_of if by_name[sdk][1].search(text)}
        if manifest_of and found & set(manifest_of):
            manifests.add(name)
        elif name not in index.docs:
            found |= {sdk for sdk, pattern in imports.items() if pattern and pattern.search(text)}
        if name not in index.docs:
            spoken = index.spoken(name)
            found |= {
                v for v, pattern in value_patterns.items() if v in spoken and pattern.search(spoken)
            }
        if found & {*hosts, *by_name}:
            found |= {term for term in short if _spelled(term, lowered)}
        if found:
            anchor_hits[name] = found
        said = index.spoken(name)
        read = {f for f, pattern in field_patterns.items() if _finds(pattern, f, said)}
        if read:
            field_hits[name] = read

    searched = [*texts, *by_name, *values]
    if not anchor_hits:
        return Impact(affected=False, reason=f"no tracked file mentions {_listing(searched)}")
    anchors = sorted({term for hits in anchor_hits.values() for term in hits}, key=searched.index)
    # A literal path of four segments or more, such as /rest/api/3/search, is no app's own
    # route: it names its API as a host would, which matters when the host comes from the
    # configuration.
    distinctive = [path for path in paths if path.count("/") >= _DISTINCTIVE_SEGMENTS]
    identities = [*hosts, *by_name, *values, *distinctive]
    if not set(anchors) & set(identities):
        return Impact(
            affected=False,
            reason=(
                f"the repository mentions {_listing(anchors, 'and')}, but never "
                f"{_listing(identities)}, so those may belong to another API"
            ),
            anchors=anchors,
        )
    # A manifest at the root is evidence the API is used, not of where: when code that
    # names the API is found, nearness is measured from there.
    in_code = [name for name in anchor_hits if name not in manifests]
    folders = {PurePosixPath(name).parent for name in in_code or anchor_hits}
    near = {
        name: read
        for name, read in field_hits.items()
        if name in anchor_hits or _under(name, folders)
    }
    sent = [value for value in values if value in anchors]
    if values and not fields and not sent:
        return Impact(
            affected=False,
            reason=f"the repository mentions {_listing(anchors, 'and')}, but never "
            f"{_listing(values)}",
            anchors=anchors,
        )
    if fields and not near and not sent:
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


# -- where, line by line ------------------------------------------------------------

type PlaceKind = Literal["value", "endpoint", "field", "api"]

# The surest sign first: a retired value or a removed endpoint fails for certain.
_KIND_ORDER: Final[Mapping[PlaceKind, int]] = {"value": 0, "endpoint": 1, "field": 2, "api": 3}


@dataclass(frozen=True, slots=True)
class Place:
    """A line behind an impact: its text, and the term of the notice it spells."""

    file: str
    line: int
    text: str
    kind: PlaceKind
    term: str


@dataclass(frozen=True, slots=True)
class _Term:
    """What one file may spell, line by line: a pattern, an endpoint or a plain term."""

    kind: PlaceKind
    term: str
    pattern: re.Pattern[str] | None = None
    gone: Gone | None = None
    near: bool = False

    def on(self, line: str) -> bool:
        if self.gone is not None:
            return self.gone.in_line(line, near=self.near)
        if self.pattern is not None:
            return self.pattern.search(line) is not None
        return _spelled(self.term, line.lower())


def locate(index: RepoIndex, notice: ApiChangeNotice, impact: Impact) -> list[Place]:
    """The lines behind an impact, the surest first: lines that send a retired value, call
    a removed endpoint or read a changed field near the API, then lines that only name the
    API (its host, a path or an SDK). Each line counts once, for its surest kind. The rules
    are the search's, so docs and lock files never show up, except manifests for an SDK."""
    retired = _retired(notice)
    values = {value: retired[value] for value in impact.anchors if value in retired}
    fields = {name: field_pattern(name) for name in impact.fields}
    hosts = list(dict.fromkeys([*notice.hosts, notice.provider]))
    api = index.api_files(hosts, notice.sdks)
    removed = [
        Gone.of(notice.endpoints[i], notice.provider)
        for i in impact.endpoints
        if any(c.kind is ChangeKind.ENDPOINT_REMOVED for c in notice.endpoints[i].changes)
    ]
    calling = {gone.term: set(index.calling(gone, api)) for gone in removed}
    sdks = {sdk.name: sdk for sdk in notice.sdks}
    names = [term for term in impact.anchors if term not in retired and term not in sdks]
    imports = {name: import_pattern(sdk) for name, sdk in sdks.items()}
    listed = {name: sdk_pattern(sdk) for name, sdk in sdks.items() if name in impact.anchors}
    near = set(impact.files)
    found: dict[tuple[str, int], Place] = {}

    def add(name: str, number: int, line: str, kind: PlaceKind, term: str) -> None:
        current = found.get((name, number))
        if current is None or _KIND_ORDER[kind] < _KIND_ORDER[current.kind]:
            found[(name, number)] = Place(name, number, line.strip(), kind, term)

    for name, text in index.texts.items():
        lowered, spoken = text.lower(), index.spoken(name)
        terms: list[_Term] = []
        if name not in index.docs:
            terms += [
                _Term("value", v, pattern=p)
                for v, p in values.items()
                if v in spoken and p.search(spoken)
            ]
            terms += [
                _Term("endpoint", gone.term, gone=gone, near=name in api)
                for gone in removed
                if name in calling[gone.term]
            ]
            terms += [_Term("api", t) for t in names if _spelled(t, lowered)]
            terms += [
                _Term("api", n, pattern=p)
                for n, p in imports.items()
                if p is not None and p.search(text)
            ]
        if name in near:
            terms += [
                _Term("field", f, pattern=p) for f, p in fields.items() if _finds(p, f, spoken)
            ]
        base = PurePosixPath(name).name
        terms += [
            _Term("api", n, pattern=p)
            for n, p in listed.items()
            if any(fnmatchcase(base, glob) for glob in MANIFESTS[sdks[n].ecosystem])
            and p.search(text)
        ]
        # Only what names the API counts in comments and docstrings too.
        said = spoken.split("\n")
        for number, line in enumerate(text.split("\n"), start=1) if terms else ():
            for term in terms:
                code = term.kind != "api"
                if term.on(said[number - 1] if code and number <= len(said) else line):
                    add(name, number, line, term.kind, term.term)
    places = list(found.values())
    # A changed field name read in a file that names the API says more than the same name
    # in a file next to it, which may read another API's field of that name.
    named = api | {place.file for place in places if place.kind != "field"}
    if any(place.file in named for place in places if place.kind == "field"):
        places = [place for place in places if place.kind != "field" or place.file in named]
    ranked = {name: i for i, name in enumerate(impact.files)}
    return sorted(
        places,
        key=lambda p: (_KIND_ORDER[p.kind], ranked.get(p.file, len(ranked)), p.file, p.line),
    )


def mentions_in_docs(index: RepoIndex, notice: ApiChangeNotice) -> list[Place]:
    """Lines of documentation that spell a value the notice retires or one of its hosts.
    The search never counts them; they show why a repository that mentions an API in its
    docs is not affected by it."""
    values = _retired(notice)
    hosts = [host.lower() for host in notice.hosts]
    found: list[Place] = []
    for name in sorted(index.docs):
        text = index.texts[name]
        lowered = text.lower()
        hits = [
            value for value, pattern in values.items() if value in text and pattern.search(text)
        ]
        named = [host for host in hosts if host in lowered]
        for number, line in enumerate(text.splitlines(), start=1) if hits or named else ():
            low = line.lower()
            value = next((v for v in hits if values[v].search(line)), None)
            host = next((h for h in named if h in low), None)
            if value is not None:
                found.append(Place(name, number, line.strip(), "value", value))
            elif host is not None:
                found.append(Place(name, number, line.strip(), "api", host))
    return found


def _retired(notice: ApiChangeNotice) -> dict[str, re.Pattern[str]]:
    return {
        change.before: value_pattern(change.before)
        for endpoint in notice.endpoints
        for change in endpoint.changes
        if change.kind is ChangeKind.VALUE_RETIRED and change.before
    }
