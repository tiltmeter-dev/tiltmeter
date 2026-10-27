from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from tests.notices.kit import (
    FEED_URL,
    KEY_FILE,
    KEY_URL,
    NOTICE_ID,
    NOTICE_URL,
    OTHER_KEY,
    PROVIDER,
    feed,
    notice,
    serve,
    signed,
)
from tiltmeter.config import SubscriptionConfig
from tiltmeter.core.models import NoticeRecord
from tiltmeter.errors import NoticeError
from tiltmeter.notices import reader as reader_module
from tiltmeter.notices.reader import NoticeReader, follow


@pytest.fixture
async def reader() -> AsyncIterator[NoticeReader]:
    async with httpx.AsyncClient() as http:
        yield NoticeReader(http)


@respx.mock
async def test_a_signed_notice_from_a_url_is_verified(reader: NoticeReader) -> None:
    respx.get(NOTICE_URL).respond(200, json=signed())
    key = respx.get(KEY_URL).respond(200, text=KEY_FILE)
    loaded = await reader.load(NOTICE_URL)
    assert loaded.verified
    assert loaded.notice.id == NOTICE_ID
    assert loaded.location == NOTICE_URL
    await reader.load(NOTICE_URL)
    assert key.call_count == 1  # keys are fetched once per run


@respx.mock
async def test_unsigned_or_tampered_notices_are_refused(reader: NoticeReader) -> None:
    respx.get(NOTICE_URL).respond(200, json=notice())
    with pytest.raises(NoticeError, match="only signed notices are read from URLs"):
        await reader.load(NOTICE_URL)
    tampered = {**signed(), "summary": "Nothing to see here."}
    respx.get(KEY_URL).respond(200, text=KEY_FILE)
    with pytest.raises(NoticeError, match="signature does not match"):
        await reader.check(tampered, "feed")
    with pytest.raises(NoticeError, match="signature does not match"):
        await reader.check(signed(key=OTHER_KEY), "feed")


async def test_files_need_a_signature_unless_allowed(tmp_path: Path, reader: NoticeReader) -> None:
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice()), encoding="utf-8")
    with pytest.raises(NoticeError, match="--allow-unsigned"):
        await reader.load(str(path))
    loaded = await reader.load(str(path), allow_unsigned=True)
    assert not loaded.verified
    with pytest.raises(NoticeError, match="cannot read"):
        await reader.load(str(tmp_path / "missing.json"))
    with pytest.raises(NoticeError, match="file or an https URL"):
        await reader.load("http://api.billing.test/notice.json")
    path.write_text('{"id": 1, "id": 2}', encoding="utf-8")
    with pytest.raises(NoticeError, match="duplicate key"):
        await reader.load(str(path))
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(NoticeError, match="JSON object"):
        await reader.load(str(path))


@respx.mock
async def test_a_notice_can_be_an_entry_of_a_feed_or_the_catalog(
    tmp_path: Path, reader: NoticeReader
) -> None:
    path = tmp_path / "feed.json"
    path.write_text(json.dumps(feed(notice(id="other"), notice())), encoding="utf-8")
    loaded = await reader.load(f"{path}#{NOTICE_ID}", allow_unsigned=True)
    assert (loaded.notice.id, loaded.location) == (NOTICE_ID, f"{path}#{NOTICE_ID}")
    with pytest.raises(NoticeError, match="lists no notice nope"):
        await reader.load(f"{path}#nope", allow_unsigned=True)

    respx.get(FEED_URL).respond(200, json=feed(notice()))
    with pytest.raises(NoticeError, match="only signed notices are read from URLs"):
        await reader.load(f"{FEED_URL}#{NOTICE_ID}", allow_unsigned=True)

    bundled = await reader.load("slack-files-upload-sunset", allow_unsigned=True)
    assert bundled.notice.provider == "slack.com"
    assert bundled.location == "tiltmeter catalog#slack-files-upload-sunset"
    with pytest.raises(NoticeError, match="--allow-unsigned"):
        await reader.load("slack-files-upload-sunset")
    # A file of that name wins over the catalog entry.
    (tmp_path / "slack-files-upload-sunset").write_text(json.dumps(notice()), encoding="utf-8")
    here = await reader.load(str(tmp_path / "slack-files-upload-sunset"), allow_unsigned=True)
    assert here.notice.id == NOTICE_ID


async def test_a_feed_can_be_read_from_a_file(tmp_path: Path, reader: NoticeReader) -> None:
    path = tmp_path / "feed.json"
    path.write_text(json.dumps(feed(notice())), encoding="utf-8")
    assert [item["id"] for item in await reader.feed(str(path))] == [NOTICE_ID]
    with pytest.raises(NoticeError, match="a feed is read from a file or an https URL"):
        await reader.feed("http://api.billing.test/feed.json")


@respx.mock
async def test_keys_only_count_from_the_provider_domain(reader: NoticeReader) -> None:
    respx.get(KEY_URL).respond(302, headers={"Location": "https://keys.attacker.test/key"})
    respx.get("https://keys.attacker.test/key").respond(200, text=KEY_FILE)
    with pytest.raises(NoticeError, match=r"outside api\.billing\.test"):
        await reader.check(signed(), "feed")


@respx.mock
async def test_key_redirects_must_stay_on_https_and_the_well_known_path(
    reader: NoticeReader,
) -> None:
    www = "https://www.api.billing.test/.well-known/api-change-key"
    respx.get(KEY_URL).respond(301, headers={"Location": www})
    respx.get(www).respond(200, text=KEY_FILE)
    assert (await reader.check(signed(), "feed")).verified  # a host move on the domain is fine

    async with httpx.AsyncClient() as http:
        fresh = NoticeReader(http)
        plain = "http://api.billing.test/.well-known/api-change-key"
        respx.get(KEY_URL).respond(302, headers={"Location": plain})
        respx.get(plain).respond(302, headers={"Location": KEY_URL})
        with pytest.raises(NoticeError, match="which is not https"):
            await fresh.check(signed(), "feed")

    async with httpx.AsyncClient() as http:
        fresh = NoticeReader(http)
        echo = "https://api.billing.test/v1/search?q=key"
        respx.get(KEY_URL).respond(302, headers={"Location": echo})
        respx.get(echo).respond(200, text=KEY_FILE)
        with pytest.raises(NoticeError, match=r"not /\.well-known/api-change-key"):
            await fresh.check(signed(), "feed")


@respx.mock
async def test_compressed_bodies_are_refused(reader: NoticeReader) -> None:
    body = gzip.compress(json.dumps(feed()).encode())
    route = respx.get(FEED_URL).respond(200, content=body, headers={"Content-Encoding": "gzip"})
    with pytest.raises(NoticeError, match="gzip-compressed"):
        await reader.feed(FEED_URL)
    assert route.calls.last.request.headers["Accept-Encoding"] == "identity"


@respx.mock
async def test_slow_downloads_stop(reader: NoticeReader, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_module, "TOTAL_TIMEOUT", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=feed())

    respx.get(FEED_URL).mock(side_effect=slow)
    with pytest.raises(NoticeError, match=r"took longer than 0\.05 seconds"):
        await reader.feed(FEED_URL)


@respx.mock
async def test_missing_or_empty_keys_are_explained(reader: NoticeReader) -> None:
    route = respx.get(KEY_URL).respond(404)
    with pytest.raises(NoticeError, match="answered HTTP 404"):
        await reader.check(signed(), "feed")
    with pytest.raises(NoticeError, match="answered HTTP 404"):
        await reader.check(signed(), "feed")
    assert route.call_count == 1  # the failure is remembered too

    async with httpx.AsyncClient() as http:
        fresh = NoticeReader(http)
        respx.get(KEY_URL).respond(200, text="# nothing yet\n")
        with pytest.raises(NoticeError, match="holds no Ed25519 key"):
            await fresh.check(signed(), "feed")


@respx.mock
async def test_downloads_are_capped(reader: NoticeReader, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_module, "MAX_FEED_BYTES", 100)
    respx.get(FEED_URL).respond(200, json=feed(signed()))
    with pytest.raises(NoticeError, match="larger than 100 bytes"):
        await reader.feed(FEED_URL)
    respx.get("https://down.test/feed.json").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NoticeError, match="could not fetch"):
        await reader.feed("https://down.test/feed.json")


@respx.mock
async def test_feeds_must_follow_the_spec(reader: NoticeReader) -> None:
    respx.get(FEED_URL).respond(200, json={"notices": []})
    with pytest.raises(NoticeError, match="not an api-change-feed/v1"):
        await reader.feed(FEED_URL)
    respx.get(FEED_URL).respond(200, json={**feed(), "extra": 1})
    with pytest.raises(NoticeError, match="unknown members: extra"):
        await reader.feed(FEED_URL)


def subscription(**fields: object) -> SubscriptionConfig:
    return SubscriptionConfig.model_validate({"provider": PROVIDER, **fields})


@respx.mock
async def test_following_a_feed_sorts_every_notice(reader: NoticeReader) -> None:
    done = signed(notice(id="handled-before"))
    old = signed(notice(id="old", published_at="2026-01-01T00:00:00Z"))
    serve(
        signed(),
        done,
        old,
        notice(id="unsigned"),
        {**signed(notice(id="tampered")), "summary": "Changed after signing."},
        {"id": "broken\x1b[31m", "spec": "nope"},
        signed(),
    )
    record = NoticeRecord(
        provider=PROVIDER,
        notice_id="handled-before",
        published_at="2026-11-03T12:00:00Z",
        status="opened",
        detail="https://github.test/pr/1",
        updated_at="2026-11-03T13:00:00Z",
    )

    def lookup(provider: str, notice_id: str) -> NoticeRecord | None:
        return record if (provider, notice_id) == (PROVIDER, "handled-before") else None

    since = subscription(since=datetime(2026, 6, 1, tzinfo=UTC))
    [report] = await follow(reader, [since], lookup)
    assert report.error is None
    assert report.subscription == PROVIDER
    states = [(entry.id, entry.state) for entry in report.entries]
    assert states == [
        (NOTICE_ID, "new"),
        ("handled-before", "done"),
        ("old", "skipped"),
        ("unsigned", "rejected"),
        ("tampered", "rejected"),
        ("broken [31m", "rejected"),  # printable characters only
        (NOTICE_ID, "skipped"),
    ]
    problems = [entry.problem for entry in report.entries]
    assert problems[2] == "published before 2026-06-01"
    assert problems[3] == "not signed: only signed notices open pull requests"
    assert "signature does not match" in (problems[4] or "")
    assert "not a valid api-change-notice/v1" in (problems[5] or "")
    assert problems[6] == "listed more than once"
    assert report.entries[1].record == record


@respx.mock
async def test_a_feed_can_be_pinned_to_one_provider(reader: NoticeReader) -> None:
    aggregator = "https://changes.example.test/api-changes.json"
    respx.get(aggregator).respond(200, json=feed(signed()))
    respx.get(KEY_URL).respond(200, text=KEY_FILE)
    pinned = SubscriptionConfig(provider="other.test", feed=aggregator)
    anyone = SubscriptionConfig(feed=aggregator)
    first, second = await follow(reader, [pinned, anyone], lambda *_: None)
    assert first.entries[0].state == "rejected"
    assert first.entries[0].problem == (
        "published by api.billing.test, but this feed is followed for other.test"
    )
    assert second.subscription == "changes.example.test"
    assert second.entries[0].state == "new"


@respx.mock
async def test_an_unreadable_feed_is_reported_not_raised(reader: NoticeReader) -> None:
    respx.get(FEED_URL).respond(503)
    [report] = await follow(reader, [subscription()], lambda *_: None)
    assert report.error == f"{FEED_URL} answered HTTP 503"
    assert report.entries == []


@respx.mock
async def test_a_first_run_skips_old_history_but_not_pending_changes(
    reader: NoticeReader,
) -> None:
    old_and_done = signed(notice(id="old", published_at="2026-01-01T00:00:00Z", effective_at=None))
    old_but_pending = signed(
        notice(
            id="pending",
            published_at="2026-01-01T00:00:00Z",
            effective_at="2027-01-01T00:00:00Z",
        )
    )
    recent = signed(notice(id="recent", published_at="2026-10-20T00:00:00Z"))
    serve(old_and_done, old_but_pending, recent)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    [report] = await follow(reader, [subscription()], lambda *_: None, now=now)
    assert [(e.id, e.state) for e in report.entries] == [
        ("old", "skipped"),
        ("pending", "new"),
        ("recent", "new"),
    ]
    assert (
        report.entries[0].problem == "published before you subscribed, and already in effect then"
    )

    def subscribed_long_ago(feed: str) -> str | None:
        return "2025-12-01T00:00:00Z"

    [again] = await follow(
        reader, [subscription()], lambda *_: None, first_seen=subscribed_long_ago, now=now
    )
    assert [e.state for e in again.entries] == ["new", "new", "new"]
