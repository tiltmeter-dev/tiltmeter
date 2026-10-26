"""Follow a provider's OpenAPI spec: keep its last version, and turn what its responses
change into an entry handled like a notice, once.

This is how a repository follows an API whose provider publishes no notices. The first
run keeps the spec as a baseline. A later run compares the latest version with it, using
the same diff as ``tiltmeter notice create --from-openapi``, and picks the endpoints the
repository uses. The change is then frozen in the history with the baseline moved past
it, so a retry works on the same change even if the spec has moved on since.

The provider did not sign or announce any of it: a spec can run ahead of production or
lag behind it, and a rename is a guess. Prompts and pull requests say so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

import yaml

from tiltmeter.core.classify import DEFAULT_SEVERITY
from tiltmeter.core.models import ChangeKind
from tiltmeter.core.openapi import OpenAPIError, base_url, diff_operations
from tiltmeter.core.store import SpecBaseline
from tiltmeter.core.timeutil import to_iso
from tiltmeter.errors import NoticeError
from tiltmeter.fix.request import field_name, is_target, reads_response
from tiltmeter.notices.create import (
    Announced,
    announceable,
    build_notice,
    summarize,
    unresolved_note,
)
from tiltmeter.notices.impact import Candidate
from tiltmeter.notices.models import (
    MAX_CHANGES,
    MAX_ENDPOINTS,
    MAX_TEXT,
    FeedEntry,
    FeedReport,
    SdkName,
    SpecInfo,
    https_host,
    printable,
)
from tiltmeter.notices.reader import NoticeReader, RecordLookup, parse_notice
from tiltmeter.notices.signing import parse_json

if TYPE_CHECKING:
    from tiltmeter.config import SubscriptionConfig
    from tiltmeter.core.store import FrozenSpecChange

# Diff this many changes per operation at most, before the notice limits apply.
DIFF_CHANGES: Final = 4 * MAX_CHANGES

type Chooser = Callable[[Sequence[Candidate], Sequence[str], Sequence[SdkName]], list[int]]
"""Picks the endpoints a repository uses: candidates, the API's hosts and its SDKs."""


# YAML aliases are copied out, up to this many values or one per byte of the file,
# whichever is more. A spec without aliases has fewer values than bytes, so only a file
# built to expand exponentially (a "billion laughs") reaches the cap.
MIN_YAML_VALUES: Final = 1_000_000


def _jsonable(document: Any, limit: int) -> Any:
    """YAML as JSON: keys such as 200 become "200", dates become ISO text, and aliases
    become copies, ``limit`` values at most."""
    left = limit

    def plain(value: Any) -> Any:
        nonlocal left
        left -= 1
        if left < 0:
            raise NoticeError(f"the spec's YAML aliases expand to more than {limit} values")
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [plain(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, str | int | float | bool) or value is None:
            return value
        if isinstance(value, date | datetime):
            return value.isoformat()
        return str(value)

    return plain(document)


def parse_spec(data: bytes) -> dict[str, Any]:
    """A spec in JSON or YAML, as plain JSON values."""
    text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
    try:
        if text.lstrip().startswith("{"):
            document = parse_json(text)
        else:
            limit = max(len(data), MIN_YAML_VALUES)
            document = _jsonable(yaml.safe_load(text), limit)
    except yaml.YAMLError as exc:
        raise NoticeError(f"the spec is neither JSON nor YAML: {printable(str(exc))}") from exc
    except RecursionError as exc:
        raise NoticeError("the spec is nested too deeply to read") from exc
    if not isinstance(document, dict):
        raise NoticeError("the spec is not an OpenAPI document")
    return document


def canonical(document: Mapping[str, Any]) -> str:
    """The document as canonical JSON, so that equal specs have equal digests."""
    try:
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise NoticeError(f"the spec cannot be stored: {exc}") from exc


def _version(document: Mapping[str, Any]) -> str | None:
    info = document.get("info")
    version = info.get("version") if isinstance(info, Mapping) else None
    return printable(str(version), 64) if version is not None else None


@dataclass(frozen=True, slots=True)
class SpecSnapshot:
    """One download of a followed spec."""

    url: str
    document: dict[str, Any]
    body: str
    digest: str
    version: str | None
    fetched_at: str

    @classmethod
    def of(cls, url: str, data: bytes, fetched_at: datetime) -> SpecSnapshot:
        document = parse_spec(data)
        body = canonical(document)
        return cls(
            url=url,
            document=document,
            body=body,
            digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            version=_version(document),
            fetched_at=to_iso(fetched_at),
        )


@dataclass(frozen=True, slots=True)
class SpecChange:
    """What the responses changed between two versions, before picking endpoints."""

    endpoints: list[Announced]
    omitted_changes: int
    removed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def compare(old: Mapping[str, Any], new: Mapping[str, Any], *, base: str) -> SpecChange:
    """Every endpoint whose responses changed, with the changes worth announcing, and the
    operations that are gone."""
    try:
        diff = diff_operations(old, new, max_changes=DIFF_CHANGES)
    except OpenAPIError as exc:
        raise NoticeError(str(exc)) from exc
    endpoints, omitted = announceable(diff.operations, base)
    return SpecChange(endpoints, omitted, diff.removed, diff.unresolved)


def candidates(endpoints: Sequence[Announced]) -> list[Candidate]:
    """The endpoints as the selection sees them: their URL, field names and severity."""
    found = []
    for method, url, changes in endpoints:
        kinds = [ChangeKind(change["kind"]) for change in changes]
        names = tuple(
            name
            for change, kind in zip(changes, kinds, strict=True)
            if is_target(kind)
            and reads_response(kind)
            and (name := field_name(str(change["path"])))
        )
        rank = max((DEFAULT_SEVERITY[kind].rank for kind in kinds), default=0)
        found.append(Candidate(url, names, rank, method))
    return found


def notice_id(url: str, old_digest: str, new_digest: str, generation: int, at: str) -> str:
    """Stable for one change, so its branch and pull request are found again on a retry."""
    seed = f"{url}\0{old_digest}\0{new_digest}\0{generation}".encode()
    return f"spec-{at[:10].replace('-', '')}-{hashlib.sha256(seed).hexdigest()[:10]}"


def build_entry(
    subscription: SubscriptionConfig,
    baseline: SpecBaseline,
    latest: SpecSnapshot,
    change: SpecChange,
    *,
    base: str,
    chooser: Chooser,
) -> FeedEntry:
    """A notice for the endpoints the repository uses, with where the change came from."""
    host = https_host(base) or ""
    provider = subscription.provider or host
    chosen = chooser(candidates(change.endpoints), [host], subscription.sdks)[:MAX_ENDPOINTS]
    picked = [change.endpoints[i] for i in chosen]
    omitted_changes = change.omitted_changes + sum(
        max(len(changes) - MAX_CHANGES, 0) for _, _, changes in picked
    )
    endpoints = [(method, url, changes[:MAX_CHANGES]) for method, url, changes in picked]
    versions = f"{baseline.version or '?'} to {latest.version or '?'}"
    count = len(change.endpoints)
    summary = printable(
        f"The OpenAPI spec ({versions}) changes the responses of {count} endpoint"
        f"{'' if count == 1 else 's'}. {summarize(endpoints)}",
        MAX_TEXT,
    )
    identifier = notice_id(
        latest.url, baseline.digest, latest.digest, baseline.generation, latest.fetched_at
    )
    raw = build_notice(
        endpoints,
        provider=provider,
        notice_id=identifier,
        summary=summary,
        published_at=datetime.fromisoformat(latest.fetched_at),
    )
    info = SpecInfo(
        url=latest.url,
        baseline_url=baseline.url,
        old_version=baseline.version,
        new_version=latest.version,
        old_fetched_at=baseline.fetched_at,
        new_fetched_at=latest.fetched_at,
        old_digest=baseline.digest,
        new_digest=latest.digest,
        versioned=subscription.versioned,
        endpoints_changed=count,
        omitted_endpoints=count - len(endpoints),
        omitted_changes=omitted_changes,
    )
    return FeedEntry(
        feed=latest.url,
        state="new",
        provider=provider,
        id=identifier,
        source="spec",
        notice=parse_notice(raw),
        spec=info,
    )


@dataclass(slots=True)
class SpecFollow:
    """What one openapi subscription holds, and what ``notices apply`` should store."""

    report: FeedReport
    latest: SpecSnapshot | None = None
    fresh: FeedEntry | None = None


async def follow_spec(
    reader: NoticeReader,
    subscription: SubscriptionConfig,
    *,
    baseline: SpecBaseline | None,
    frozen: Sequence[FrozenSpecChange],
    record_for: RecordLookup,
    chooser: Chooser,
    now: datetime,
) -> SpecFollow:
    """Pending frozen changes, plus whatever the latest version changes."""
    url = subscription.feed_url
    report = FeedReport(subscription=subscription.label, feed=url, kind="openapi")
    for kept in frozen:
        entry = FeedEntry.model_validate_json(kept.entry)
        record = record_for(kept.provider, kept.notice_id)
        state = "done" if record is not None and record.done else "new"
        report.entries.append(entry.model_copy(update={"state": state, "record": record}))
    try:
        data = await reader.download(url, limit=subscription.max_bytes)
        latest = await asyncio.to_thread(SpecSnapshot.of, url, data, now)
        given = None
        if baseline is None and subscription.baseline is not None:
            given = await _given_baseline(reader, subscription, subscription.baseline, now)
            baseline = given
    except NoticeError as exc:
        report.error = printable(str(exc))
        return SpecFollow(report)
    if baseline is None:
        report.note = "no baseline yet: `tiltmeter notices apply` records this version"
        return SpecFollow(report, latest)
    if baseline.digest == latest.digest:
        # A baseline from the config that is already the latest version is stored as is.
        return SpecFollow(report, latest if given is not None else None)
    base = subscription.base_url or base_url(latest.document)
    if base is None:
        report.error = "the spec names no https server; set base_url in the subscription"
        return SpecFollow(report)
    try:
        old = json.loads(baseline.body)
        change = await asyncio.to_thread(compare, old, latest.document, base=base)
        notes = _notes(change)
        if given is not None:
            notes.insert(0, "compared with the baseline the subscription names")
        if not change.endpoints:
            report.note = "; ".join(["the spec changed, but none of its responses did", *notes])
            return SpecFollow(report, latest)
        fresh = build_entry(subscription, baseline, latest, change, base=base, chooser=chooser)
    except (NoticeError, RecursionError) as exc:
        report.error = printable(str(exc) or "the spec could not be compared")
        return SpecFollow(report)
    if notes:
        report.note = "; ".join(notes)
    report.entries.append(fresh)
    return SpecFollow(report, latest, fresh)


async def _given_baseline(
    reader: NoticeReader, subscription: SubscriptionConfig, url: str, now: datetime
) -> SpecBaseline:
    """The version the repository was written against, as the config names it."""
    try:
        data = await reader.download(url, limit=subscription.max_bytes)
        first = await asyncio.to_thread(SpecSnapshot.of, url, data, now)
    except NoticeError as exc:
        raise NoticeError(f"the baseline spec: {exc}") from exc
    return SpecBaseline(
        subscription=subscription.feed_url,
        url=url,
        digest=first.digest,
        version=first.version,
        fetched_at=first.fetched_at,
        generation=0,
        body=first.body,
    )


def _notes(change: SpecChange) -> list[str]:
    # Operations that are gone are in the entry already, as endpoint_removed changes.
    unread = unresolved_note(change.unresolved)
    return [unread] if unread else []
