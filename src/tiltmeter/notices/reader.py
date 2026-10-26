"""Read notices and feeds, and check every signature against the provider's own key.

Notices come from third parties. Downloads are https only and capped in size, a key is
trusted only when it is served from the provider's domain (after redirects too), and an
invalid notice or signature is rejected, never repaired. Nothing here expands ``${VAR}``
placeholders or sends credentials: feeds and keys are public files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import ValidationError

from tiltmeter.core.fetch import USER_AGENT
from tiltmeter.core.models import NoticeRecord
from tiltmeter.errors import NoticeError
from tiltmeter.notices.models import (
    FEED_SPEC,
    KEY_PATH,
    MAX_FEED_NOTICES,
    ApiChangeNotice,
    FeedEntry,
    FeedReport,
    https_host,
    on_domain,
    printable,
)
from tiltmeter.notices.signing import MAX_JSON_BYTES, parse_json, parse_key_file, verify

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from tiltmeter.config import SubscriptionConfig

FETCH_TIMEOUT: Final = 15.0
TOTAL_TIMEOUT: Final = 60.0
MAX_REDIRECTS: Final = 5
MAX_FEED_BYTES: Final = 10_000_000
MAX_KEY_BYTES: Final = 16_384
_LABEL_CHARS: Final = 100

type RecordLookup = Callable[[str, str], NoticeRecord | None]
type FirstSeen = Callable[[str], str | None]

# On the first run of a feed, notices older than this that are already in effect are
# history, not news.
BACKLOG: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class LoadedNotice:
    """A valid notice, where it came from and whether its signature was checked."""

    notice: ApiChangeNotice
    raw: dict[str, Any]
    location: str
    verified: bool


def describe(exc: ValidationError, limit: int = 3) -> str:
    """The first few validation errors on one line."""
    parts = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    more = len(exc.errors()) - limit
    # Unknown keys show up in ``loc``, so the text is the sender's: keep it printable.
    return printable("; ".join(parts) + (f" (+{more} more)" if more > 0 else ""), 500)


def parse_notice(raw: Any) -> ApiChangeNotice:
    if not isinstance(raw, dict):
        raise NoticeError("a notice must be a JSON object")
    try:
        return ApiChangeNotice.model_validate(raw)
    except ValidationError as exc:
        raise NoticeError(f"not a valid api-change-notice/v1: {describe(exc)}") from exc


def read_json_file(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise NoticeError(f"{path} is larger than {MAX_JSON_BYTES} bytes")
        return parse_json(path.read_bytes())
    except OSError as exc:
        raise NoticeError(f"cannot read {path}: {exc.strerror}") from exc


def _label(item: Any, key: str) -> str | None:
    """A printable, bounded copy of a string field from an entry that may be invalid."""
    value = item.get(key) if isinstance(item, dict) else None
    if not isinstance(value, str):
        return None
    return printable(value, _LABEL_CHARS) or None


class NoticeReader:
    """Downloads notices, feeds and keys, and checks signatures. Keys are fetched once."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._keys: dict[tuple[str, str], list[Ed25519PublicKey] | str] = {}

    async def raw(self, source: str) -> Any:
        """The JSON in a file or behind an https URL, not validated yet."""
        if source.startswith("https://"):
            return parse_json(await self.download(source, limit=MAX_JSON_BYTES))
        if "://" in source:
            raise NoticeError("a notice is read from a file or an https URL")
        return read_json_file(Path(source))

    async def load(self, source: str, *, allow_unsigned: bool = False) -> LoadedNotice:
        """A notice from a file or an https URL. Over the network, only signed ones."""
        loaded = await self.check(await self.raw(source), source)
        if source.startswith("https://") and not loaded.verified:
            raise NoticeError(f"{source} is not signed: only signed notices are read from URLs")
        if not loaded.verified and not allow_unsigned:
            raise NoticeError(
                f"{source} is not signed; pass --allow-unsigned to use a notice you trust anyway"
            )
        return loaded

    async def check(
        self, raw: Any, location: str, *, keys: list[Ed25519PublicKey] | None = None
    ) -> LoadedNotice:
        """Validate a notice and, when it is signed, its signature (with ``keys`` or the
        provider's published ones)."""
        notice = parse_notice(raw)
        if notice.signature is None:
            return LoadedNotice(notice, raw, location, verified=False)
        if keys is None:
            keys = await self.keys(notice.signature.key, provider=notice.provider)
        if not verify(raw, notice.signature.value, keys):
            raise NoticeError(f"the signature does not match the key at {notice.signature.key}")
        return LoadedNotice(notice, raw, location, verified=True)

    async def keys(self, url: str, *, provider: str) -> list[Ed25519PublicKey]:
        cached = self._keys.get((url, provider))
        if cached is None:
            try:
                body = await self.download(url, limit=MAX_KEY_BYTES, domain=provider, path=KEY_PATH)
                found = parse_key_file(body.decode("utf-8", errors="replace"))
                cached = found or f"{url} holds no Ed25519 key"
            except NoticeError as exc:
                cached = str(exc)
            self._keys[(url, provider)] = cached
        if isinstance(cached, str):
            raise NoticeError(cached)
        return cached

    async def feed(self, url: str) -> list[Any]:
        """The raw notices a feed lists, each still to be validated on its own."""
        data = parse_json(await self.download(url, limit=MAX_FEED_BYTES))
        if not isinstance(data, dict) or data.get("spec") != FEED_SPEC:
            raise NoticeError(f"{url} is not an api-change-feed/v1 document")
        unknown = sorted(set(data) - {"spec", "notices"})
        if unknown:
            raise NoticeError(f"{url} has unknown members: {', '.join(unknown)}")
        notices = data.get("notices")
        if not isinstance(notices, list):
            raise NoticeError(f"{url} has no notices list")
        if len(notices) > MAX_FEED_NOTICES:
            raise NoticeError(f"{url} lists more than {MAX_FEED_NOTICES} notices")
        return notices

    async def download(
        self, url: str, *, limit: int, domain: str | None = None, path: str | None = None
    ) -> bytes:
        """GET an https URL, at most ``limit`` bytes; with ``domain``, only from that domain
        and with ``path``, only from that path, at every redirect."""
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                return await self._download(url, limit=limit, domain=domain, path=path)
        except TimeoutError as exc:
            raise NoticeError(f"{url} took longer than {TOTAL_TIMEOUT:g} seconds") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise NoticeError(f"could not fetch {url}: {exc}") from exc

    async def _download(
        self, url: str, *, limit: int, domain: str | None, path: str | None
    ) -> bytes:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = https_host(current)
            if host is None:
                raise NoticeError(f"{url} led to {current}, which is not https")
            if domain is not None and not on_domain(host, domain):
                raise NoticeError(f"{url} led to {current}, outside {domain}")
            if path is not None and urlsplit(current)[2:] != (path, "", ""):
                raise NoticeError(f"{url} led to {current}, not {path}")
            async with self._http.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=FETCH_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain",
                    # Compressed bodies could expand far past ``limit`` before it is checked.
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.is_redirect and "location" in response.headers:
                    current = urljoin(current, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise NoticeError(f"{current} answered HTTP {response.status_code}")
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise NoticeError(f"{current} sent a {encoding}-compressed body")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > limit:
                        raise NoticeError(f"{current} is larger than {limit} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise NoticeError(f"{url} redirected more than {MAX_REDIRECTS} times")


def _never_seen(_: str) -> str | None:
    return None


async def follow(
    reader: NoticeReader,
    subscriptions: Sequence[SubscriptionConfig],
    record_for: RecordLookup,
    *,
    first_seen: FirstSeen = _never_seen,
    now: datetime | None = None,
) -> list[FeedReport]:
    """Read every subscribed feed and sort its notices into new, done, skipped and rejected.

    Without a ``since``, a notice published well before the feed was first followed, and
    already in effect by then, is skipped: a first run brings news, not the whole history.
    """
    moment = now or datetime.now().astimezone()
    reports = list(
        await asyncio.gather(
            *(_read(reader, sub, record_for, first_seen, moment) for sub in subscriptions)
        )
    )
    seen: set[tuple[str, str]] = set()
    for report in reports:
        for index, entry in enumerate(report.entries):
            if entry.notice is None or entry.state not in ("new", "done"):
                continue
            key = (entry.notice.provider, entry.notice.id)
            if key in seen:
                report.entries[index] = entry.model_copy(
                    update={"state": "skipped", "problem": "listed more than once"}
                )
            seen.add(key)
    return reports


async def _read(
    reader: NoticeReader,
    subscription: SubscriptionConfig,
    record_for: RecordLookup,
    first_seen: FirstSeen,
    now: datetime,
) -> FeedReport:
    url = subscription.feed_url
    try:
        items = await reader.feed(url)
    except NoticeError as exc:
        return FeedReport(subscription=subscription.label, feed=url, error=printable(str(exc)))
    seen = first_seen(url)
    started = datetime.fromisoformat(seen) if seen else now
    entries = [await _entry(reader, subscription, url, item, record_for, started) for item in items]
    return FeedReport(subscription=subscription.label, feed=url, entries=entries)


async def _entry(
    reader: NoticeReader,
    subscription: SubscriptionConfig,
    url: str,
    item: Any,
    record_for: RecordLookup,
    started: datetime,
) -> FeedEntry:
    provider, notice_id = _label(item, "provider"), _label(item, "id")
    try:
        loaded = await reader.check(item, f"{url}#{notice_id}")
    except NoticeError as exc:
        return FeedEntry(
            feed=url,
            state="rejected",
            provider=provider,
            id=notice_id,
            problem=printable(str(exc)),
        )
    notice = loaded.notice
    entry = FeedEntry(feed=url, state="new", provider=notice.provider, id=notice.id, notice=notice)
    if subscription.provider is not None and notice.provider != subscription.provider:
        problem = (
            f"published by {notice.provider}, but this feed is followed for {subscription.provider}"
        )
        return entry.model_copy(update={"state": "rejected", "problem": problem})
    if not loaded.verified:
        problem = "not signed: only signed notices open pull requests"
        return entry.model_copy(update={"state": "rejected", "problem": problem})
    record = record_for(notice.provider, notice.id)
    if record is not None and record.done:
        return entry.model_copy(update={"state": "done", "record": record})
    if subscription.since is not None:
        if notice.published_at < subscription.since:
            problem = f"published before {subscription.since.date().isoformat()}"
            return entry.model_copy(update={"state": "skipped", "problem": problem})
    elif notice.published_at < started - BACKLOG and (
        notice.effective_at is None or notice.effective_at <= started
    ):
        problem = "published before you subscribed, and already in effect then"
        return entry.model_copy(update={"state": "skipped", "problem": problem})
    return entry.model_copy(update={"record": record})
