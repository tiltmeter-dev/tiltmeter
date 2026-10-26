"""Write notices for providers: from a check of their own API, or from two versions of a spec.

Either way the changes come from Tiltmeter's diff, so they use its paths and change kinds.
New fields are left out (a renamed field carries its new name in ``renamed_to``), and so
are latency regressions. The result is an unsigned notice for the provider to review,
edit (the summary and the notes are theirs to write), sign and publish.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, get_args
from urllib.parse import urlsplit

from pydantic import ValidationError

from tiltmeter.core.classify import DEFAULT_SEVERITY
from tiltmeter.core.diff import MOVED_TO, RENAMED_TO
from tiltmeter.core.models import Change, ChangeKind, CheckResult
from tiltmeter.core.openapi import OpenAPIError, OperationDiff, base_url, diff_operations
from tiltmeter.core.timeutil import to_iso
from tiltmeter.errors import NoticeError
from tiltmeter.notices.models import (
    FEED_SPEC,
    MAX_CHANGES,
    MAX_ENDPOINTS,
    MAX_TEXT,
    MAX_VALUE,
    NOTICE_SPEC,
    Ecosystem,
    NoticeChange,
    NoticeEndpoint,
    SdkRelease,
    https_host,
    printable,
)
from tiltmeter.notices.reader import parse_notice

if TYPE_CHECKING:
    from tiltmeter.config import EndpointConfig


class NoChanges(NoticeError):
    """Two specs whose responses are the same: there is nothing to announce."""


_LEFT_OUT: Final = frozenset({ChangeKind.FIELD_ADDED, ChangeKind.LATENCY_REGRESSION})
_ID_PART: Final = re.compile(r"[^A-Za-z0-9._\-]+")

type Announced = tuple[str, str, list[dict[str, Any]]]
"""Method, URL and the changes of one endpoint, as they go into a notice."""


def _clean(text: str, limit: int) -> str:
    return printable(text, limit)


def _last_name(path: str) -> str:
    """``$.data[*].phone`` → ``phone``; the body is "the response"."""
    match = re.search(r"(?:\.([^.\[\]]+)|\[\"([^\"]+)\"\])$", path)
    if match is None:
        return "the response"
    return match.group(1) or match.group(2)


def notice_changes(
    changes: Sequence[Change], renames: Mapping[str, str] | None = None, *, root: str = ""
) -> list[dict[str, Any]]:
    """The changes worth announcing, as notice entries. ``root`` is the base URL the path of
    an endpoint a removed one moved to goes after."""
    entries: list[dict[str, Any]] = []
    for change in changes:
        if change.kind in _LEFT_OUT:
            continue
        entry: dict[str, Any] = {"path": change.path, "kind": change.kind.value}
        if change.before:
            entry["before"] = _clean(change.before, MAX_VALUE)
        if change.after:
            entry["after"] = _clean(change.after, MAX_VALUE)
        if change.kind is ChangeKind.FIELD_REMOVED:
            hint = change.hint or ""
            new_path = (
                hint.removeprefix(RENAMED_TO)
                if hint.startswith(RENAMED_TO)
                else (renames or {}).get(change.path)
            )
            if new_path:
                entry["renamed_to"] = new_path
        if change.kind is ChangeKind.ENDPOINT_REMOVED and root:
            hint = change.hint or ""
            method, _, path = hint.removeprefix(MOVED_TO).partition(" ")
            if hint.startswith(MOVED_TO) and path:
                entry["moved_to"] = {"method": method, "url": f"{root.rstrip('/')}{path}"}
        entries.append(entry)
    return entries


def _phrase(entry: Mapping[str, Any]) -> str:
    name = _last_name(str(entry["path"]))
    moved = entry.get("moved_to")
    match ChangeKind(entry["kind"]):
        case ChangeKind.ENDPOINT_REMOVED if isinstance(moved, Mapping):
            path = urlsplit(str(moved.get("url"))).path
            return f"the endpoint moves to {moved.get('method')} {path}"
        case ChangeKind.ENDPOINT_REMOVED:
            return "the endpoint is removed"
        case ChangeKind.VALUE_RETIRED if entry.get("replaced_by"):
            return f"{entry.get('before')} is retired in favor of {entry.get('replaced_by')}"
        case ChangeKind.VALUE_RETIRED:
            return f"{entry.get('before')} is retired"
        case ChangeKind.FIELD_REMOVED if entry.get("renamed_to"):
            target = str(entry["renamed_to"])
            if _last_name(target) == name:  # the same name elsewhere: it moved
                return f"{name} moves to {target.removeprefix('$.')}"
            return f"{name} is renamed to {_last_name(target)}"
        case ChangeKind.FIELD_REMOVED:
            return f"{name} is removed"
        case ChangeKind.TYPE_CHANGED:
            return f"{name} changes from {entry.get('before')} to {entry.get('after')}"
        case ChangeKind.NULLABLE_INTRODUCED:
            return f"{name} can be null"
        case ChangeKind.FIELD_NOW_OPTIONAL:
            return f"{name} is no longer always present"
        case ChangeKind.ENUM_VALUE_ADDED:
            return f"{name} has new values ({entry.get('after')})"
        case ChangeKind.STATUS_CHANGED:
            return f"the status changes from {entry.get('before')} to {entry.get('after')}"
        case _:
            return "the response is no longer JSON"


def summarize(endpoints: Sequence[Announced]) -> str:
    """A plain first draft of the summary: one sentence per set of changes."""
    grouped: dict[str, list[str]] = {}
    for method, url, changes in endpoints:
        # The same name at several depths ($.a.id, $.b.id) reads as one phrase.
        phrases = "; ".join(dict.fromkeys(_phrase(entry) for entry in changes))
        grouped.setdefault(phrases, []).append(f"{method} {urlsplit(url).path or '/'}")
    sentences = []
    for phrases, names in grouped.items():
        if len(names) == 1:
            subject = names[0]
        elif len(names) <= 3:
            subject = f"{', '.join(names[:-1])} and {names[-1]}"
        else:
            subject = f"{', '.join(names[:3])} and {len(names) - 3} more"
        sentences.append(f"{subject}: {phrases}.")
    return _clean(" ".join(sentences), MAX_TEXT)


def default_id(published_at: datetime, endpoints: Sequence[Announced]) -> str:
    """``<date>-<the last name in the first path>``, such as ``2026-11-03-customers``."""
    path = urlsplit(endpoints[0][1]).path if endpoints else ""
    names = [part for part in path.split("/") if part and "{" not in part]
    slug = _ID_PART.sub("-", names[-1] if names else "api").strip("-.").lower()[:60]
    return f"{published_at:%Y-%m-%d}-{slug or 'api'}"


def parse_sdk(text: str) -> SdkRelease:
    """``ecosystem:name@version``, such as ``pypi:partner-sdk@3.0.0``."""
    ecosystem, _, rest = text.partition(":")
    name, _, version = rest.rpartition("@")
    known = get_args(Ecosystem.__value__)
    if ecosystem not in known or not name or not version:
        raise NoticeError(
            f"expected ECOSYSTEM:NAME@VERSION with an ecosystem among {', '.join(known)}, "
            f"got {text!r}"
        )
    return SdkRelease.model_validate({"ecosystem": ecosystem, "name": name, "fixed_in": version})


def build_notice(
    endpoints: Sequence[Announced],
    *,
    provider: str | None,
    notice_id: str | None,
    summary: str | None,
    published_at: datetime,
    effective_at: datetime | None = None,
    sdks: Sequence[SdkRelease] = (),
) -> dict[str, Any]:
    """A valid, unsigned notice. The provider defaults to the host of the endpoints."""
    if not endpoints:
        raise NoticeError("there are no changes to announce")
    hosts = list(dict.fromkeys(https_host(url) or "" for _, url, _ in endpoints))
    if provider is None:
        if len(hosts) != 1 or not hosts[0]:
            raise NoticeError("the endpoints are on several hosts; pass --provider")
        provider = hosts[0]
    raw: dict[str, Any] = {
        "spec": NOTICE_SPEC,
        "id": notice_id or default_id(published_at, endpoints),
        "provider": provider,
        "published_at": to_iso(published_at),
    }
    if effective_at is not None:
        raw["effective_at"] = to_iso(effective_at)
    raw["summary"] = summary or summarize(endpoints)
    raw["endpoints"] = [
        {"method": method, "url": url, "changes": changes} for method, url, changes in endpoints
    ]
    if sdks:
        raw["sdks"] = [sdk.model_dump(mode="json") for sdk in sdks]
    parse_notice(raw)
    return raw


def from_check(
    check: CheckResult, endpoint: EndpointConfig, *, url: str | None = None
) -> Announced:
    """The endpoint and changes a stored check found, as the API's customers see them."""
    target = url or endpoint.display_url()
    if "${" in target:
        raise NoticeError(
            f"the URL of {endpoint.id} holds a placeholder; pass --url with the URL "
            "your customers call"
        )
    if https_host(target) is None:
        raise NoticeError(f"{target} is not an https URL; pass --url with the public one")
    renames = {}
    if check.explanation is not None:
        renames = {r.before_path: r.after_path for r in check.explanation.probable_renames}
    changes = notice_changes(check.changes, renames)
    if not changes:
        raise NoticeError(f"check #{check.check_id} found no changes worth announcing")
    return endpoint.method, target, changes


def rank(changes: Sequence[Mapping[str, Any]]) -> int:
    """How severe the worst of these changes is, by the default rules."""
    return max((DEFAULT_SEVERITY[ChangeKind(c["kind"])].rank for c in changes), default=0)


def announceable(operations: Sequence[OperationDiff], root: str) -> tuple[list[Announced], int]:
    """Each operation's changes as a notice can hold them, and how many were left out.

    Changes a notice cannot hold, such as a property name with a backtick, are left out
    and counted, and so is an operation whose URL a notice cannot hold.
    """
    endpoints: list[Announced] = []
    omitted = 0
    for operation in operations:
        omitted += operation.omitted
        url = f"{root.rstrip('/')}{operation.path}"
        changes = []
        for entry in notice_changes(operation.changes, root=root):
            try:
                NoticeChange.model_validate(entry)
            except ValidationError:
                omitted += 1
                continue
            changes.append(entry)
        if not changes:
            continue
        try:
            # The URL is what can fail here; the change limit applies when one is drafted.
            NoticeEndpoint.model_validate(
                {"method": operation.method, "url": url, "changes": changes[:MAX_CHANGES]}
            )
        except ValidationError:
            omitted += len(changes)
            continue
        endpoints.append((operation.method, url, changes))
    return endpoints, omitted


def unresolved_note(references: Sequence[str]) -> str | None:
    """What to tell about references a spec diff could not follow, if any."""
    if not references:
        return None
    count = len(references)
    return printable(
        f"{count} reference{'' if count == 1 else 's'} in the specs could not be followed, "
        f"such as {references[0]}; what {'it points' if count == 1 else 'they point'} at "
        "was not compared",
        MAX_TEXT,
    )


def within_limits(endpoints: Sequence[Announced]) -> tuple[list[Announced], int]:
    """At most as many endpoints and changes as a notice holds, the most severe first."""
    by_severity = sorted(range(len(endpoints)), key=lambda i: (-rank(endpoints[i][2]), i))
    keep = set(by_severity[:MAX_ENDPOINTS])
    kept = [endpoint for i, endpoint in enumerate(endpoints) if i in keep]
    omitted = sum(len(endpoint[2]) for i, endpoint in enumerate(endpoints) if i not in keep)
    omitted += sum(max(len(changes) - MAX_CHANGES, 0) for _, _, changes in kept)
    return [(m, u, changes[:MAX_CHANGES]) for m, u, changes in kept], omitted


@dataclass(frozen=True, slots=True)
class Drafted:
    """What two specs give a notice: its endpoints, the operations gone, what is left out,
    and the references the diff could not follow."""

    endpoints: list[Announced]
    removed: list[str]
    omitted_endpoints: int
    omitted_changes: int
    unresolved: list[str] = field(default_factory=list)


def from_openapi(
    old: Mapping[str, Any], new: Mapping[str, Any], *, base: str | None = None
) -> Drafted:
    """The endpoints whose responses differ between two specs, within a notice's limits."""
    try:
        diff = diff_operations(old, new, max_changes=4 * MAX_CHANGES)
        root = base or base_url(new)
    except OpenAPIError as exc:
        raise NoticeError(str(exc)) from exc
    if root is None:
        raise NoticeError("the new spec names no https server; pass --base-url")
    every, dropped = announceable(diff.operations, root)
    if not every:
        raise NoChanges("the two specs describe the same responses")
    endpoints, left_out = within_limits(every)
    return Drafted(
        endpoints,
        diff.removed,
        len(every) - len(endpoints),
        dropped + left_out,
        diff.unresolved,
    )


# -- feeds ------------------------------------------------------------------------


def _published(notice: Mapping[str, Any]) -> datetime:
    try:
        moment = datetime.fromisoformat(str(notice.get("published_at")))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def add_to_feed(feed: Mapping[str, Any] | None, notice: Mapping[str, Any]) -> dict[str, Any]:
    """The feed with ``notice`` in it, replacing an older copy, newest first."""
    current = list(feed.get("notices", [])) if feed is not None else []
    kept = [
        item
        for item in current
        if not (
            isinstance(item, Mapping)
            and item.get("id") == notice.get("id")
            and item.get("provider") == notice.get("provider")
        )
    ]
    kept.append(dict(notice))
    kept.sort(key=_published, reverse=True)
    return {"spec": FEED_SPEC, "notices": kept}


def write_json(path: Path, data: Any) -> None:
    """Write pretty JSON atomically, so a half-written feed is never served."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    # Feeds are public files: keep the mode a file already had, or make it world-readable.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
