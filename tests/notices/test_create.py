from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fix.kit import billing_endpoint, rename_check
from tests.notices.kit import notice, signed
from tiltmeter.core.models import Change, ChangeKind, Severity
from tiltmeter.errors import NoticeError
from tiltmeter.notices.create import (
    add_to_feed,
    build_notice,
    from_check,
    from_openapi,
    notice_changes,
    parse_sdk,
    summarize,
    unresolved_note,
    write_json,
)
from tiltmeter.notices.reader import parse_notice

SPECS = Path(__file__).parent.parent / "fixtures" / "specs"
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def spec(case: str, name: str) -> Any:
    return yaml.safe_load((SPECS / case / name).read_text(encoding="utf-8"))


def test_announced_changes_leave_new_fields_and_latency_out() -> None:
    changes = [
        *rename_check().changes,
        Change(
            path="$", kind=ChangeKind.LATENCY_REGRESSION, severity=Severity.WARNING, after="9 s"
        ),
    ]
    assert notice_changes(changes) == [
        {
            "path": "$.phone",
            "kind": "field_removed",
            "before": "string",
            "renamed_to": "$.phone_number",
        },
        {"path": "$.balance", "kind": "type_changed", "before": "integer", "after": "string"},
    ]
    plain = Change(path="$.tel", kind=ChangeKind.FIELD_REMOVED, severity=Severity.BREAKING)
    assert notice_changes([plain], {"$.tel": "$.phone"})[0]["renamed_to"] == "$.phone"


def test_a_check_becomes_a_notice() -> None:
    endpoint = billing_endpoint()
    announced = from_check(rename_check(), endpoint)
    raw = build_notice([announced], provider=None, notice_id=None, summary=None, published_at=NOW)
    assert raw["provider"] == "api.billing.test"
    assert raw["id"] == "2026-11-03-customers"
    assert raw["endpoints"][0]["url"] == "https://api.billing.test/v1/customers"
    assert raw["summary"] == (
        "GET /v1/customers: phone is renamed to phone_number; "
        "balance changes from integer to string."
    )
    assert "BILLING_KEY" not in json.dumps(raw)
    public = from_check(rename_check(), endpoint, url="https://api.billing.test/v2/customers")
    assert public[1] == "https://api.billing.test/v2/customers"


def test_checks_against_hidden_or_plain_urls_need_the_public_one() -> None:
    with pytest.raises(NoticeError, match="--url"):
        from_check(rename_check(), billing_endpoint(url="${BILLING_URL}/v1/customers"))
    with pytest.raises(NoticeError, match="not an https URL"):
        from_check(rename_check(), billing_endpoint(url="http://localhost:8000/v1/customers"))
    empty = rename_check().model_copy(update={"changes": []})
    with pytest.raises(NoticeError, match="no changes worth announcing"):
        from_check(empty, billing_endpoint())


def test_two_specs_become_a_notice() -> None:
    old, new = spec("rename_and_retype", "old.yaml"), spec("rename_and_retype", "new.yaml")
    drafted = from_openapi(old, new)
    endpoints = drafted.endpoints
    assert drafted.removed == ["GET /v1/legacy"]
    raw = build_notice(
        endpoints,
        provider=None,
        notice_id="billing-2026-11-03",
        summary=None,
        published_at=NOW,
        effective_at=datetime(2026, 12, 1, tzinfo=UTC),
        sdks=[parse_sdk("pypi:partner-sdk@3.0.0")],
    )
    model = parse_notice(raw)
    assert model.provider == "api.partner.example"
    assert [e.url for e in model.endpoints] == [
        "https://api.partner.example/v1/customers",
        "https://api.partner.example/v1/customers/{id}",
    ]
    assert [(c.path, c.kind.value, c.renamed_to) for c in model.endpoints[0].changes] == [
        ("$.data[*].balance", "type_changed", None),
        ("$.data[*].nickname", "field_removed", None),
        ("$.data[*].phone", "field_removed", "$.data[*].phone_number"),
        ("$.data[*].status", "enum_value_added", None),
    ]
    assert raw["effective_at"] == "2026-12-01T00:00:00Z"
    assert raw["summary"].startswith("GET /v1/customers and GET /v1/customers/{id}: ")
    swagger = from_openapi(spec("swagger2", "old.yaml"), spec("swagger2", "new.yaml"))
    assert swagger.endpoints[0][:2] == ("POST", "https://legacy.example/api/users")


def test_references_a_diff_cannot_follow_are_noted() -> None:
    old, new = spec("unresolved_refs", "old.yaml"), spec("unresolved_refs", "new.yaml")
    drafted = from_openapi(old, new)
    assert [(c["path"], c["kind"]) for _, _, changes in drafted.endpoints for c in changes] == [
        ("$.total", "type_changed")
    ]
    assert len(drafted.unresolved) == 4
    assert unresolved_note(drafted.unresolved) == (
        "4 references in the specs could not be followed, such as "
        "#/components/responses/Refunds; what they point at was not compared"
    )
    assert unresolved_note(["#/x"]) == (
        "1 reference in the specs could not be followed, such as #/x; what it points at "
        "was not compared"
    )
    assert unresolved_note([]) is None


def test_specs_without_a_server_or_a_difference_are_refused() -> None:
    old = spec("nullable_31", "old.yaml")
    without = {key: value for key, value in old.items() if key != "servers"}
    with pytest.raises(NoticeError, match="--base-url"):
        from_openapi(without, without | {"paths": spec("nullable_31", "new.yaml")["paths"]})
    drafted = from_openapi(old, spec("nullable_31", "new.yaml"), base="https://x.test/")
    assert drafted.endpoints[0][1] == "https://x.test/orders"
    with pytest.raises(NoticeError, match="same responses"):
        from_openapi(old, old)
    with pytest.raises(NoticeError, match="OpenAPI 3 or Swagger"):
        from_openapi({}, {})


def test_notices_on_several_hosts_name_their_provider() -> None:
    change = [{"path": "$.a", "kind": "field_removed"}]
    endpoints = [("GET", "https://eu.pay.test/a", change), ("GET", "https://us.pay.test/a", change)]
    with pytest.raises(NoticeError, match="--provider"):
        build_notice(endpoints, provider=None, notice_id=None, summary=None, published_at=NOW)
    raw = build_notice(
        endpoints, provider="pay.test", notice_id=None, summary="Hi.", published_at=NOW
    )
    assert raw["provider"] == "pay.test"
    with pytest.raises(NoticeError, match="is not on"):
        build_notice(endpoints, provider="x.test", notice_id=None, summary=None, published_at=NOW)


def test_summaries_say_where_a_field_moves() -> None:
    changes = [
        {
            "path": "$.current_period_end",
            "kind": "field_removed",
            "renamed_to": "$.items.data[*].current_period_end",
        },
        {"path": "$.phone", "kind": "field_removed", "renamed_to": "$.phone_number"},
    ]
    assert summarize([("GET", "https://a.test/v1/subscriptions/{id}", changes)]) == (
        "GET /v1/subscriptions/{id}: current_period_end moves to "
        "items.data[*].current_period_end; phone is renamed to phone_number."
    )


def test_summaries_say_each_phrase_once() -> None:
    changes = [
        {"path": "$.has_downloads", "kind": "field_removed"},
        {"path": "$.parent.has_downloads", "kind": "field_removed"},
        {"path": "$.parent.owner", "kind": "nullable_introduced"},
    ]
    assert summarize([("GET", "https://a.test/repos/{owner}/{repo}", changes)]) == (
        "GET /repos/{owner}/{repo}: has_downloads is removed; owner can be null."
    )


def test_long_summaries_are_cut() -> None:
    changes = [{"path": f"$.field_{i}", "kind": "field_removed"} for i in range(80)]
    text = summarize([("GET", "https://a.test/x", changes)])
    assert len(text) == 500
    assert text.endswith("…")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pypi:partner-sdk@3.0.0", ("pypi", "partner-sdk", "3.0.0")),
        ("npm:@partner/sdk@3.0.0", ("npm", "@partner/sdk", "3.0.0")),
        ("maven:com.partner:sdk@3.0.0", ("maven", "com.partner:sdk", "3.0.0")),
        ("go:github.com/partner/sdk-go@v3.0.0", ("go", "github.com/partner/sdk-go", "v3.0.0")),
    ],
)
def test_sdk_releases(text: str, expected: tuple[str, str, str]) -> None:
    sdk = parse_sdk(text)
    assert (sdk.ecosystem, sdk.name, sdk.fixed_in) == expected


@pytest.mark.parametrize("text", ["partner-sdk@3", "brew:x@1", "pypi:x", "pypi:@1"])
def test_bad_sdk_releases(text: str) -> None:
    with pytest.raises(NoticeError, match="ECOSYSTEM:NAME@VERSION"):
        parse_sdk(text)


def test_feeds_keep_one_copy_of_each_notice_newest_first() -> None:
    older = signed(notice(id="older", published_at="2026-10-01T00:00:00+02:00"))
    first = add_to_feed(None, older)
    current = signed()
    second = add_to_feed(first, current)
    assert [n["id"] for n in second["notices"]] == ["2026-11-03-phone-rename", "older"]
    revised = signed(notice(summary="Revised."))
    third = add_to_feed(second, revised)
    assert third["notices"][0]["summary"] == "Revised."
    assert len(third["notices"]) == 2


def test_written_files_are_public_and_never_half_written(tmp_path: Path) -> None:
    path = tmp_path / ".well-known" / "api-changes.json"
    write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert list(path.parent.iterdir()) == [path]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
        path.chmod(0o640)
        write_json(path, {"a": 2})
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_summaries_name_a_few_endpoints_and_count_the_rest() -> None:
    change = [{"path": "$.phone", "kind": "field_removed"}]
    endpoints = [("GET", f"https://a.test/v1/items{i}", change) for i in range(5)]
    assert summarize(endpoints) == (
        "GET /v1/items0, GET /v1/items1, GET /v1/items2 and 2 more: phone is removed."
    )


def test_big_spec_changes_fit_in_one_notice() -> None:
    ok = {
        "200": {
            "description": "",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}},
        }
    }
    paths = {f"/v1/things{i}/{{id}}": {"get": {"responses": ok}} for i in range(60)}
    thing = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
    old = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://a.test"}],
        "paths": paths,
        "components": {"schemas": {"Thing": thing}},
    }
    new = {
        **old,
        "components": {"schemas": {"Thing": {**thing, "properties": {"id": {"type": "integer"}}}}},
    }
    drafted = from_openapi(old, new)
    assert (len(drafted.endpoints), drafted.omitted_endpoints, drafted.omitted_changes) == (
        50,
        10,
        10,
    )
    raw = build_notice(
        drafted.endpoints, provider=None, notice_id=None, summary=None, published_at=NOW
    )
    assert len(raw["endpoints"]) == 50
