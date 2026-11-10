"""Which release of an SDK, and which version of an API, a repository asks for.

A notice names the SDK releases that already follow its change (``fixed_in``) and may say
the change is only for callers on some API version (``applies_to``). Whether that is this
repository is written in its manifests, its lock files and its code, so Tiltmeter reads it:

- the release of each of the notice's SDKs that a manifest allows or a lock file installs,
  and whether it is older than ``fixed_in``, at or past it, or cannot be told;
- the dated API versions the code pins next to the API, such as ``apiVersion: "2024-06-20"``
  or a ``Stripe-Version`` header.

These are facts for the person and the agent to weigh. The search decides one thing with
them: a repository whose SDK already follows a removed endpoint, and that never calls the
endpoint itself, is not affected (:func:`tiltmeter.notices.impact.assess`).

Requirements are read loosely, as bounds: ``^17.2`` is 17.2 up to 18, ``>=11,<12`` is 11
up to 12. What cannot be read as bounds (a URL, a tag, an "or") says nothing either way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from fnmatch import fnmatchcase
from itertools import islice
from pathlib import PurePosixPath
from typing import Final

from tiltmeter.notices.models import ApiVersionPin, Ecosystem, SdkName, SdkPin, SdkRelease

type Version = tuple[int, ...]
type Bounds = tuple[Version | None, Version | None]  # the lowest allowed, the first not

MAX_PINS: Final = 8
MAX_API_VERSIONS: Final = 5
MAX_PER_FILE: Final = 20  # the places in one file that name the SDK and are read
_NUMBERS: Final = re.compile(r"v?(\d+(?:\.\d+){0,3})")
_CLAUSE: Final = re.compile(r"(\^|~>|~=|~|===|==|>=|<=|!=|=|>|<)?\s*v?(\d+(?:\.(?:\d+|[xX*]))*)")
_UNREADABLE: Final = ("||", "://", "git+", "file:", "workspace:", "link:", "npm:", "path")
_WRITTEN: Final = re.compile(r"[A-Za-z0-9^~<>=!*][A-Za-z0-9.^~<>=!*,+_ \-]{0,63}")


def parse_version(text: str) -> Version | None:
    """The leading numbers of a version: 12.0.0, v81.2, or 2.0.0 from 2.0.0-beta.3."""
    found = _NUMBERS.match(text.strip())
    return tuple(int(part) for part in found[1].split(".")) if found else None


def _pad(a: Version, b: Version) -> tuple[Version, Version]:
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)), b + (0,) * (size - len(b))


def at_least(a: Version, b: Version) -> bool:
    left, right = _pad(a, b)
    return left >= right


def _bump(version: Version, at: int) -> Version:
    """The first version past ``version`` at position ``at``: 1.2.3 at 0 is 2."""
    kept = version[: at + 1] + (0,) * max(0, at + 1 - len(version))
    return (*kept[:at], kept[at] + 1)


def _caret(version: Version) -> Version:
    """Where ^version ends: the next major, or the next minor or patch below 1.0."""
    for at, number in enumerate(version):
        if number != 0:
            return _bump(version, at)
    return _bump(version, len(version) - 1)


def _clause(operator: str, text: str, bare: str) -> Bounds:
    wild = [part in {"x", "X", "*"} for part in text.split(".")]
    version: Version = tuple(
        int(part) for part, w in zip(text.split("."), wild, strict=True) if not w
    )
    if any(wild):
        return (version or None, _bump(version, len(version) - 1) if version else None)
    last = len(version) - 1
    match operator or bare:
        case "^":
            return version, _caret(version)
        case "~" | "~>" | "~=":
            # ~1.2.3 stays in 1.2; ~1.2 stays in 1 for Ruby and Python, in 1.2 for npm.
            at = max(last - 1, 0) if operator != "~" or last >= 2 else last
            return version, _bump(version, at)
        case ">=" | ">":
            return version, None
        case "<":
            return None, version
        case "<=":
            return None, _bump(version, last)
        case "!=":
            return None, None
        case _:  # ==, =, ===, or a bare version read as exact
            return version, _bump(version, last)


def allowed(spec: str, ecosystem: Ecosystem) -> Bounds | None:
    """The versions a requirement allows, as bounds; None when it cannot be read as such."""
    text = spec.strip().strip("\"'")
    if not text or any(mark in text for mark in _UNREADABLE):
        return None
    if text in {"*", "latest", "x"}:
        return None, None
    bare = "^" if ecosystem == "cargo" else "=="
    low: Version | None = None
    high: Version | None = None
    found = False
    for operator, numbers in _CLAUSE.findall(text):
        found = True
        clause_low, clause_high = _clause(operator, numbers, bare)
        if clause_low is not None and (low is None or at_least(clause_low, low)):
            low = clause_low
        if clause_high is not None and (high is None or at_least(high, clause_high)):
            high = clause_high
    return (low, high) if found else None


def follows(bounds: Bounds | None, fixed_in: str) -> bool | None:
    """Whether every version in ``bounds`` is at or past ``fixed_in`` (True), every one is
    older (False), or it cannot be told (None)."""
    fixed = parse_version(fixed_in)
    if bounds is None or fixed is None:
        return None
    low, high = bounds
    if low is not None and at_least(low, fixed):
        return True
    if high is not None and at_least(fixed, high):
        return False
    return None


# -- where each ecosystem writes it ----------------------------------------------------


def _name(sdk: SdkName) -> str:
    """The SDK's name as a pattern. PyPI treats -, _ and . alike, in any case."""
    if sdk.ecosystem == "pypi":
        parts = [re.escape(part) for part in re.split(r"[-_.]+", sdk.name) if part]
        return "(?i:" + "[-_.]".join(parts) + ")"
    if sdk.ecosystem == "nuget":
        return f"(?i:{re.escape(sdk.name)})"
    return re.escape(sdk.name)


def _requirements(sdk: SdkName, text: str) -> Iterator[str]:
    """What manifests ask for, by ecosystem: each requirement as written."""
    name = _name(sdk)
    match sdk.ecosystem:
        case "pypi":
            # requirements.txt lines and PEP 508 strings; then Poetry and Pipfile tables.
            # A lone = is Poetry's table, read below: PEP 508 has no such operator.
            # The repeats are bounded: a file may be someone else's, written to be slow.
            listed = (
                rf"(?m)(?:^[ \t]*|[\"']){name}(?:\[[^\]\n]{{0,200}}\])?[ \t]{{0,20}}"
                rf"((?:===?|~=|!=|[<>]=?)[ \t]{{0,20}}[\dv*][^\"'#;\n]{{0,80}})"
            )
            yield from (found.strip() for found in re.findall(listed, text))
            yield from re.findall(
                rf"(?m)^[ \t]*{name}[ \t]*=[ \t]*[\"']([^\"'\n]{{1,80}})[\"']", text
            )
            yield from re.findall(_table(name, "[\"']"), text)
        case "npm" | "packagist":
            yield from _json_requirements(sdk, text)
        case "go":
            yield from re.findall(rf"(?m)(?:^|\s){name}(?:/v\d+)?[ \t]+(v\d[^\s]*)", text)
        case "rubygems":
            call = r"(?:gem|add(?:_runtime|_development)?_dependency)[ \t]*(?:\([ \t]*)?"
            gem = (
                rf"{call}[\"']{name}[\"']"
                rf"((?:[ \t]{{0,20}},[ \t]{{0,20}}[\"'][^\"'\n]{{1,40}}[\"']){{1,4}})"
            )
            for found in re.findall(gem, text):
                yield ", ".join(re.findall(r"[\"']([^\"'\n]+)[\"']", found))
        case "cargo":
            yield from re.findall(rf"(?m)^[ \t]*{name}[ \t]*=[ \t]*\"([^\"\n]{{1,80}})\"", text)
            yield from re.findall(_table(name, '"'), text)
        case "maven":
            artifact = re.escape(sdk.name.rsplit(":", 1)[-1])
            xml = (
                rf"<artifactId>{artifact}</artifactId>\s{{0,200}}"
                r"<version>([^<$\s]{1,80})</version>"
            )
            yield from re.findall(xml, text)
            yield from re.findall(rf"[\"']{name}:([^\"'$\s]{{1,80}})[\"']", text)
        case "nuget":
            reference = rf"(?:Include|id)=\"{name}\"\s{{1,200}}[Vv]ersion=\"([^\"\n]{{1,80}})\""
            yield from re.findall(reference, text)


def _table(name: str, quote: str) -> str:
    """``name = { version = "1.2", ... }`` on one line, as Poetry and Cargo write it."""
    return (
        rf"(?m)^[ \t]*{name}[ \t]*=[ \t]*\{{[^}}\n]{{0,300}}?\bversion[ \t]*=[ \t]*"
        rf"{quote}([^\"'\n]{{1,80}})"
    )


def _json_requirements(sdk: SdkName, text: str) -> Iterator[str]:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser goes
        return
    if not isinstance(data, dict):
        return
    sections = (
        ("require", "require-dev")
        if sdk.ecosystem == "packagist"
        else ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
    )
    for section in sections:
        listed = data.get(section)
        if isinstance(listed, dict) and isinstance(listed.get(sdk.name), str):
            yield listed[sdk.name]


def _installed(sdk: SdkName, base: str, text: str) -> Iterator[str]:
    """The exact versions a lock file installs for the SDK."""
    name = _name(sdk)
    match base:
        case "uv.lock" | "poetry.lock" | "Cargo.lock":
            named = rf"name = \"{name}\"[ \t\r]*\nversion = \"([^\"\n]{{1,80}})\""
            yield from re.findall(named, text)
        case "Pipfile.lock":
            yield from _pipfile_installed(sdk, text)
        case "package-lock.json" | "composer.lock":
            yield from _json_installed(sdk, text)
        case "yarn.lock":
            yield from _yarn(name, text)
        case "pnpm-lock.yaml":
            yield from re.findall(rf"(?m)^[ \t]+/?'?{name}@(\d[^:'(\s]{{0,80}})'?:", text)
            entry = rf"(?m)^[ \t]+'?{name}'?:\n[ \t]+specifier:[^\n]*\n"
            block = entry + r"[ \t]+version:[ \t]+'?(\d[^'(\s]{0,80})"
            yield from re.findall(block, text)
        case "Gemfile.lock":
            yield from re.findall(rf"(?m)^ {{4}}{name} \(([^)\s]{{1,80}})\)$", text)


_YARN_VERSION: Final = re.compile(r"[ \t]+version:?[ \t]+\"?([^\"\s]{1,80})")


def _yarn(name: str, text: str) -> Iterator[str]:
    """yarn.lock, line by line: an entry opens with ``stripe@^17.4.0:`` at the start of a
    line, in quotes or not, and says ``version "17.7.0"`` (``version: 17.7.0`` since
    Yarn 2) in the lines indented under it."""
    opens = re.compile(rf"\"?{name}@")
    inside = False
    for line in text.split("\n"):
        if not line.startswith((" ", "\t")):
            inside = opens.match(line) is not None and line.rstrip().endswith(":")
        elif inside and (found := _YARN_VERSION.match(line)) is not None:
            inside = False
            yield found[1]


def _pipfile_installed(sdk: SdkName, text: str) -> Iterator[str]:
    """Pipfile.lock: ``{"default": {"stripe": {"version": "==12.0.0"}}, "develop": ...}``."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return
    wanted = re.compile(_name(sdk))
    for section in ("default", "develop"):
        listed = data.get(section) if isinstance(data, dict) else None
        for key, package in listed.items() if isinstance(listed, dict) else ():
            version = package.get("version") if isinstance(package, dict) else None
            if isinstance(version, str) and wanted.fullmatch(key):
                yield version.removeprefix("==")


def _json_installed(sdk: SdkName, text: str) -> Iterator[str]:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser goes
        return
    if not isinstance(data, dict):
        return
    packages = data.get("packages")
    if isinstance(packages, dict):  # package-lock.json, version 2 and 3
        found = packages.get(f"node_modules/{sdk.name}")
        if isinstance(found, dict) and isinstance(found.get("version"), str):
            yield found["version"]
    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):  # package-lock.json, version 1
        found = dependencies.get(sdk.name)
        if isinstance(found, dict) and isinstance(found.get("version"), str):
            yield found["version"]
    for section in ("packages", "packages-dev"):  # composer.lock
        listed = data.get(section)
        for package in listed if isinstance(listed, list) else []:
            named = isinstance(package, dict) and package.get("name") == sdk.name
            if named and isinstance(package.get("version"), str):
                yield package["version"]


_LOCKS_OF: Final[Mapping[Ecosystem, tuple[str, ...]]] = {
    "pypi": ("uv.lock", "poetry.lock", "Pipfile.lock"),
    "npm": ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "rubygems": ("Gemfile.lock",),
    "cargo": ("Cargo.lock",),
    "packagist": ("composer.lock",),
}


def sdk_pins(
    texts: Mapping[str, str],
    locks: Mapping[str, str],
    sdks: Sequence[SdkRelease],
    manifests: Mapping[str, tuple[str, ...]],
) -> list[SdkPin]:
    """The release of each SDK that the repository asks for, file by file. A lock file says
    what is installed, so where one names the SDK, the manifests next to it are left out."""
    pins: list[SdkPin] = []
    for sdk in sdks:
        found: list[SdkPin] = []
        for name, text in sorted(locks.items()):
            base = PurePosixPath(name).name
            if base in _LOCKS_OF.get(sdk.ecosystem, ()):
                found += [
                    pin
                    for version in islice(_installed(sdk, base, text), MAX_PER_FILE)
                    if (pin := _pin(sdk, name, version, locked=True))
                ]
        if not found:
            for name, text in sorted(texts.items()):
                base = PurePosixPath(name).name
                if any(fnmatchcase(base, glob) for glob in manifests.get(sdk.ecosystem, ())):
                    found += [
                        pin
                        for spec in islice(_requirements(sdk, text), MAX_PER_FILE)
                        if (pin := _pin(sdk, name, spec, locked=False))
                    ]
        pins += list({(pin.file, pin.version): pin for pin in found}.values())
    return pins[:MAX_PINS]


def _pin(sdk: SdkRelease, file: str, version: str, *, locked: bool) -> SdkPin | None:
    """The pin, unless what the file writes is not a version or a range: the text is shown
    to people and to the agent, so it is only ever the characters those are made of."""
    written = " ".join(version.split())
    if not _WRITTEN.fullmatch(written) or not (written == "*" or re.search(r"\d", written)):
        return None
    return SdkPin(
        ecosystem=sdk.ecosystem,
        name=sdk.name,
        version=written,
        file=file,
        locked=locked,
        fixed_in=sdk.fixed_in,
        follows=follows(allowed(written, sdk.ecosystem), sdk.fixed_in),
    )


def standing(pin: SdkPin) -> str:
    """Where a pin stands next to the release that follows the change, in words."""
    if pin.follows is None:
        return f"may be older than {pin.fixed_in}, the first release that follows the change"
    if pin.follows:
        return f"already follows the change ({pin.fixed_in} or later)"
    return f"is older than {pin.fixed_in}, the first release that follows the change"


def all_follow(pins: Sequence[SdkPin], names: Sequence[str]) -> bool:
    """Whether each of the named SDKs is pinned somewhere, and every pin of them is at or
    past the release that follows the change."""
    theirs = [pin for pin in pins if pin.name in names]
    return (
        bool(theirs)
        and {pin.name for pin in theirs} == set(names)
        and all(pin.follows for pin in theirs)
    )


# An API version written as a date, the way Stripe, GitHub, Anthropic, Notion, Shopify and
# Azure version theirs: apiVersion: "2024-06-20", "Stripe-Version": "2024-06-20",
# X-GitHub-Api-Version: 2022-11-28, api_version = "2024-10".
_API_VERSION: Final = re.compile(
    r"(?i)\b(?:api[-_ ]?version|[a-z]+-version)\b[\"']?\s*[:=,(]\s*[\"']?"
    r"(\d{4}-\d{2}(?:-\d{2})?(?:[.-][a-z0-9]+)?)\b"
)


def api_versions(texts: Mapping[str, str], files: Sequence[str]) -> list[ApiVersionPin]:
    """The dated API versions pinned in ``files``, the files that name the API, each
    version once, where it is first written."""
    seen: dict[str, ApiVersionPin] = {}
    for name in sorted(files):
        for number, line in enumerate(texts.get(name, "").split("\n"), start=1):
            if len(line) > 2_000:
                continue
            for version in _API_VERSION.findall(line):
                seen.setdefault(version, ApiVersionPin(version=version, file=name, line=number))
    return list(seen.values())[:MAX_API_VERSIONS]
