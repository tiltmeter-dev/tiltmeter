from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import respx

from tests.notices.kit import PROVIDER, SPEC_URL, billing_spec
from tiltmeter.config import SubscriptionConfig
from tiltmeter.core.store import Store
from tiltmeter.errors import NoticeError
from tiltmeter.notices.impact import Candidate
from tiltmeter.notices.models import MAX_CHANGES, MAX_ENDPOINTS, SdkName
from tiltmeter.notices.reader import NoticeReader
from tiltmeter.notices.spec import (
    SpecSnapshot,
    build_entry,
    candidates,
    compare,
    follow_spec,
    notice_id,
    parse_spec,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
BASE = f"https://{PROVIDER}"


def every(candidates: Any, hosts: Any, sdks: Any) -> list[int]:
    return list(range(len(candidates)))


def subscription(**fields: Any) -> SubscriptionConfig:
    return SubscriptionConfig.model_validate({"openapi": SPEC_URL, **fields})


def test_specs_are_json_or_yaml() -> None:
    assert parse_spec(b'{"openapi": "3.1.0"}') == {"openapi": "3.1.0"}
    assert parse_spec("﻿openapi: 3.0.0\npaths: {}\n".encode()) == {
        "openapi": "3.0.0",
        "paths": {},
    }
    assert parse_spec(b"a: &x [1, 2]\nb: *x\n") == {"a": [1, 2], "b": [1, 2]}
    laughs = "a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]\n" for i in range(1, 9)
    )
    with pytest.raises(NoticeError, match="aliases expand to more than 1000000 values"):
        parse_spec(laughs.encode())
    with pytest.raises(NoticeError, match="not an OpenAPI document"):
        parse_spec(b"- a list\n")
    with pytest.raises(NoticeError, match="neither JSON nor YAML"):
        parse_spec(b"a: [unclosed\n")


def test_equal_specs_have_equal_digests() -> None:
    one = SpecSnapshot.of(SPEC_URL, json.dumps({"b": 1, "a": 2}).encode(), NOW)
    two = SpecSnapshot.of(SPEC_URL, b'{"a": 2,\n "b": 1}', NOW)
    assert one.digest == two.digest
    assert one.body == '{"a":2,"b":1}'
    assert one.fetched_at == "2026-11-03T12:00:00Z"
    assert SpecSnapshot.of(SPEC_URL, json.dumps(billing_spec("1.2")).encode(), NOW).version == "1.2"


def test_compare_lists_changed_endpoints_with_announceable_changes() -> None:
    change = compare(billing_spec(), billing_spec(renamed=True), base=BASE)
    assert [(method, url) for method, url, _ in change.endpoints] == [
        ("GET", f"{BASE}/v1/customers/{{id}}"),
        ("GET", f"{BASE}/v1/invoices/{{id}}"),
    ]
    assert [c["path"] for c in change.endpoints[0][2]] == ["$.balance", "$.phone"]
    assert change.endpoints[0][2][1]["renamed_to"] == "$.phone_number"
    assert change.omitted_changes == 0
    [first, _] = candidates(change.endpoints)
    assert first == Candidate(f"{BASE}/v1/customers/{{id}}", ("balance", "phone"), 2, "GET")


def test_names_a_notice_cannot_hold_are_left_out_and_counted() -> None:
    old = billing_spec(**{"bad\u001bname": {"type": "string"}, "`tick`": {"type": "string"}})
    old["components"]["schemas"]["Customer"]["required"] += ["bad\u001bname", "`tick`"]
    change = compare(old, billing_spec(), base=BASE)
    assert change.omitted_changes == 2  # the backtick name, on two endpoints
    paths = [c["path"] for _, _, changes in change.endpoints for c in changes]
    assert '$["bad\\u001bname"]' in paths  # escaped in the path: inert text, kept
    assert all("`" not in path for path in paths)


def test_entries_pick_endpoints_and_respect_the_limits() -> None:
    old = {**billing_spec(), "paths": {}}
    new = {**billing_spec(renamed=True), "paths": {}}
    ok = billing_spec()["paths"]["/v1/customers/{id}"]
    for i in range(MAX_ENDPOINTS + 10):
        old["paths"][f"/v1/things{i}/{{id}}"] = ok
        new["paths"][f"/v1/things{i}/{{id}}"] = ok
    store = Store(":memory:")
    store.save_spec_baseline(
        SPEC_URL, digest="old", version="1.0", fetched_at="2026-10-01T00:00:00Z", body="{}"
    )
    baseline = store.spec_baseline(SPEC_URL)
    assert baseline is not None
    latest = SpecSnapshot.of(SPEC_URL, json.dumps(new).encode(), NOW)
    change = compare(old, new, base=BASE)
    picked: list[Any] = []

    def chooser(found: Any, hosts: Any, sdks: Any) -> list[int]:
        picked.append((hosts, sdks))
        return list(reversed(range(len(found))))

    sub = subscription(sdks=["pypi:billing-sdk"], versioned=True)
    entry = build_entry(sub, baseline, latest, change, base=BASE, chooser=chooser)
    assert picked == [([PROVIDER], [SdkName(ecosystem="pypi", name="billing-sdk")])]
    assert entry.source == "spec"
    assert entry.notice is not None
    assert len(entry.notice.endpoints) == MAX_ENDPOINTS
    assert entry.notice.endpoints[0].url == f"{BASE}/v1/things59/{{id}}"  # the chooser's order
    assert entry.spec is not None
    assert (entry.spec.endpoints_changed, entry.spec.omitted_endpoints) == (60, 10)
    assert (entry.spec.old_version, entry.spec.new_version) == ("1.0", "1.0")
    assert not entry.spec.new_api_version
    assert entry.notice.summary.startswith("The OpenAPI spec (1.0 to 1.0) changes the responses")
    assert entry.id == notice_id(SPEC_URL, "old", latest.digest, 1, latest.fetched_at)
    assert entry.id is not None
    assert entry.id.startswith("spec-20261103-")


def test_changes_past_the_limit_are_counted() -> None:
    many = {f"f{i}": {"type": "string"} for i in range(MAX_CHANGES + 5)}
    old = billing_spec(**many)
    old["components"]["schemas"]["Customer"]["required"] += list(many)
    store = Store(":memory:")
    store.save_spec_baseline(SPEC_URL, digest="d", version="1", fetched_at="t", body="{}")
    baseline = store.spec_baseline(SPEC_URL)
    assert baseline is not None
    latest = SpecSnapshot.of(SPEC_URL, json.dumps(billing_spec()).encode(), NOW)
    change = compare(old, billing_spec(), base=BASE)
    entry = build_entry(subscription(), baseline, latest, change, base=BASE, chooser=every)
    assert entry.notice is not None
    assert len(entry.notice.endpoints[0].changes) == MAX_CHANGES
    assert entry.spec is not None
    assert entry.spec.omitted_changes == 10


@respx.mock
async def test_following_a_spec_from_baseline_to_change() -> None:
    store = Store(":memory:")
    sub = subscription(provider=PROVIDER)
    route = respx.get(SPEC_URL).respond(200, json=billing_spec("1.0"))

    async def run() -> Any:
        import httpx

        async with httpx.AsyncClient() as http:
            return await follow_spec(
                NoticeReader(http),
                sub,
                baseline=store.spec_baseline(SPEC_URL),
                frozen=store.spec_changes(SPEC_URL),
                record_for=store.notice_record,
                chooser=every,
                now=NOW,
            )

    first = await run()
    assert first.report.kind == "openapi"
    assert first.report.note == "no baseline yet: `tiltmeter notices apply` records this version"
    assert first.latest is not None
    assert first.fresh is None
    store.save_spec_baseline(
        SPEC_URL,
        digest=first.latest.digest,
        version=first.latest.version,
        fetched_at=first.latest.fetched_at,
        body=first.latest.body,
    )

    unchanged = await run()
    assert (unchanged.latest, unchanged.report.note, unchanged.report.entries) == (None, None, [])

    route.respond(200, json={**billing_spec("1.0"), "info": {"title": "New", "version": "1.0"}})
    cosmetic = await run()
    assert cosmetic.report.note == "the spec changed, but none of its responses did"
    assert cosmetic.fresh is None

    route.respond(200, json=billing_spec("1.1", renamed=True))
    changed = await run()
    assert changed.fresh is not None
    assert changed.report.entries == [changed.fresh]
    assert changed.fresh.spec is not None
    assert changed.fresh.spec.new_version == "1.1"

    store.freeze_spec_change(
        SPEC_URL,
        provider=PROVIDER,
        notice_id=changed.fresh.id or "",
        entry=changed.fresh.model_dump_json(),
        digest=changed.latest.digest,
        version="1.1",
        fetched_at=changed.latest.fetched_at,
        body=changed.latest.body,
    )
    pending = await run()
    [kept] = pending.report.entries
    assert (kept.state, kept.id) == ("new", changed.fresh.id)
    store.record_notice(
        PROVIDER, changed.fresh.id or "", published_at="t", status="opened", detail=None, at="t"
    )
    [done] = (await run()).report.entries
    assert done.state == "done"

    route.respond(404)
    failed = await run()
    assert failed.report.error == f"{SPEC_URL} answered HTTP 404"


@respx.mock
async def test_a_configured_baseline_that_is_already_the_latest_is_kept() -> None:
    import httpx

    old_url = SPEC_URL.replace(".json", "-1.0.json")
    respx.get(old_url).respond(200, json=billing_spec("1.0"))
    respx.get(SPEC_URL).respond(200, json=billing_spec("1.0"))
    async with httpx.AsyncClient() as http:
        followed = await follow_spec(
            NoticeReader(http),
            subscription(baseline=old_url),
            baseline=None,
            frozen=[],
            record_for=lambda provider, notice_id: None,
            chooser=every,
            now=NOW,
        )
    assert followed.latest is not None  # so that `notices apply` stores it
    assert (followed.fresh, followed.report.note, followed.report.entries) == (None, None, [])


@respx.mock
async def test_notes_say_what_the_diff_could_not_follow() -> None:
    import httpx

    store = Store(":memory:")
    store.save_spec_baseline(
        SPEC_URL, digest="x", version="1", fetched_at="t", body=json.dumps(billing_spec())
    )
    broken = billing_spec("1.1", renamed=True, plan={"$ref": "plans.yaml#/Plan"})
    del broken["paths"]["/v1/invoices/{id}"]
    respx.get(SPEC_URL).respond(200, json=broken)
    async with httpx.AsyncClient() as http:
        followed = await follow_spec(
            NoticeReader(http),
            subscription(),
            baseline=store.spec_baseline(SPEC_URL),
            frozen=[],
            record_for=store.notice_record,
            chooser=every,
            now=NOW,
        )
    assert followed.fresh is not None
    assert followed.report.note == (
        "1 reference in the specs could not be followed, such as plans.yaml#/Plan; what it "
        "points at was not compared"
    )
    gone = [e for e in followed.fresh.notice.endpoints if e.url.endswith("/v1/invoices/{id}")]
    assert [(c.kind.value, c.path) for c in gone[0].changes] == [("endpoint_removed", "$")]


@respx.mock
async def test_a_spec_without_a_server_needs_a_base_url() -> None:
    import httpx

    store = Store(":memory:")
    store.save_spec_baseline(
        SPEC_URL, digest="x", version="1", fetched_at="t", body=json.dumps(billing_spec())
    )
    no_server = {
        key: value for key, value in billing_spec(renamed=True).items() if key != "servers"
    }
    respx.get(SPEC_URL).respond(200, json=no_server)
    async with httpx.AsyncClient() as http:
        followed = await follow_spec(
            NoticeReader(http),
            subscription(),
            baseline=store.spec_baseline(SPEC_URL),
            frozen=[],
            record_for=store.notice_record,
            chooser=every,
            now=NOW,
        )
    assert (
        followed.report.error == "the spec names no https server; set base_url in the subscription"
    )


def test_yaml_specs_become_plain_json() -> None:
    text = b"""openapi: 3.0.3
info: {title: Dated, version: 1}
paths:
  /x:
    get:
      responses:
        200:
          description: ok
          content:
            application/json:
              schema: {type: string, example: 2024-01-01}
        default: {description: error}
"""
    document = parse_spec(text)
    responses = document["paths"]["/x"]["get"]["responses"]
    assert set(responses) == {"200", "default"}
    assert responses["200"]["content"]["application/json"]["schema"]["example"] == "2024-01-01"
    assert SpecSnapshot.of(SPEC_URL, text, NOW).version == "1"


def test_deeply_nested_specs_are_refused_not_crashing() -> None:
    with pytest.raises(NoticeError, match=r"nested too deeply|not valid JSON"):
        parse_spec(b"a: " + b"[" * 100_000)
    with pytest.raises(NoticeError):
        parse_spec(b"{" + b'"a":{' * 100_000)


def test_removed_operations_are_announced_with_where_they_moved() -> None:
    old = billing_spec()
    new = billing_spec()
    del new["paths"]["/v1/invoices/{id}"]
    change = compare(old, new, base=BASE)
    assert change.endpoints == [
        ("GET", f"{BASE}/v1/invoices/{{id}}", [{"path": "$", "kind": "endpoint_removed"}])
    ]
    assert change.removed == ["GET /v1/invoices/{id}"]
    new["paths"]["/v1/invoices/{id}/details"] = old["paths"]["/v1/invoices/{id}"]
    [(_, url, changes)] = compare(old, new, base=BASE).endpoints
    assert url == f"{BASE}/v1/invoices/{{id}}"
    assert changes == [
        {
            "path": "$",
            "kind": "endpoint_removed",
            "moved_to": {"method": "GET", "url": f"{BASE}/v1/invoices/{{id}}/details"},
        }
    ]
