from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tiltmeter.config import load_config
from tiltmeter.errors import HarError
from tiltmeter.har import endpoints_from_har, is_sensitive, load_har, render_config

SAMPLE = Path(__file__).parent / "fixtures" / "har" / "sample.har"
# Every credential, and the one response body value, in the sample starts with SECRET-.
SECRET = re.compile(r"SECRET-[A-Z-]+")


def entries() -> list[dict[str, Any]]:
    return load_har(SAMPLE)


def by_id(**options: Any) -> dict[str, Any]:
    result = endpoints_from_har(entries(), **options)
    return {endpoint.id: endpoint for endpoint in result.endpoints}


def test_keeps_one_endpoint_per_json_api() -> None:
    result = endpoints_from_har(entries())
    assert [e.id for e in result.endpoints] == [
        "customers",
        "customers_invoices",
        "customers_2",
        "collect",
        "graphql_get_user",
        "items",
        "customers_3",
    ]
    assert result.requests == 14
    assert result.hosts == {"api.example.com": 5, "analytics.tracker.com": 1, "localhost:8765": 1}
    # The repeated GET keeps its first query string.
    assert result.endpoints[0].params["limit"] == "10"


def test_leaves_out_writes_unless_asked() -> None:
    assert endpoints_from_har(entries()).writes == 2  # POST /v1/orders and the mutation
    endpoints = by_id(include_writes=True)
    assert endpoints["post_orders"].method == "POST"
    assert endpoints["post_graphql_update_user"].body["query"].startswith("mutation")
    # A form post can't be replayed as JSON, so it is skipped either way.
    assert not any("auth.example.com" in e.url for e in endpoints.values())


def test_host_filter_matches_subdomains() -> None:
    result = endpoints_from_har(entries(), ["example.com"])
    assert set(result.hosts) == {"api.example.com"}
    assert result.other_hosts == 2  # the tracker and localhost
    assert set(endpoints_from_har(entries(), ["localhost"]).hosts) == {"localhost:8765"}


def test_headers_drop_browser_noise() -> None:
    customers = by_id()["customers"]
    assert list(customers.headers) == ["accept", "authorization", "cookie", "x-request-id"]
    items = by_id()["items"]
    assert "If-None-Match" not in items.headers
    graphql = by_id()["graphql_get_user"]
    assert graphql.headers == {"Content-Type": "application/json"}


def test_credentials_become_placeholders() -> None:
    endpoints = by_id(include_writes=True)
    customers = endpoints["customers"]
    assert customers.headers["authorization"] == "Bearer ${API_EXAMPLE_COM_AUTHORIZATION}"
    assert customers.headers["cookie"] == "${API_EXAMPLE_COM_COOKIE}"
    assert customers.params == {"limit": "10", "access_token": "${API_EXAMPLE_COM_ACCESS_TOKEN}"}
    assert endpoints["items"].headers["X-Api-Key"] == "${API_EXAMPLE_COM_X_API_KEY}"
    assert endpoints["collect"].params == {"key": "${ANALYTICS_TRACKER_COM_KEY}", "page": "home"}
    variables = endpoints["graphql_get_user"].body["variables"]
    assert variables == {"id": "1", "sessionToken": "${API_EXAMPLE_COM_SESSION_TOKEN}"}
    order = endpoints["post_orders"].body
    assert order["password"] == "${API_EXAMPLE_COM_PASSWORD}"
    assert order["credentials"] == {"user": "${API_EXAMPLE_COM_CREDENTIALS_USER}", "remember": True}
    assert order["quantity"] == 2
    # User info in the URL is dropped, not turned into a placeholder.
    assert endpoints["customers_2"].url.startswith("https://api.example.com/v1/customers/550e")


def test_variables_are_listed_once_and_sorted() -> None:
    assert endpoints_from_har(entries()).variables == [
        "ANALYTICS_TRACKER_COM_KEY",
        "API_EXAMPLE_COM_ACCESS_TOKEN",
        "API_EXAMPLE_COM_AUTHORIZATION",
        "API_EXAMPLE_COM_COOKIE",
        "API_EXAMPLE_COM_SESSION_TOKEN",
        "API_EXAMPLE_COM_X_API_KEY",
    ]


@pytest.mark.parametrize("include_writes", [False, True])
def test_rendered_config_loads_and_holds_no_secret(tmp_path: Path, include_writes: bool) -> None:
    result = endpoints_from_har(entries(), include_writes=include_writes)
    text = render_config(result, "sample.har")
    assert SECRET.findall(text) == []
    assert "sid=" not in text
    path = tmp_path / "tiltmeter.yaml"
    path.write_text(text, encoding="utf-8")
    config = load_config(path)
    assert config.endpoint_ids == [endpoint.id for endpoint in result.endpoints]
    assert config.endpoint("graphql_get_user").body["query"].startswith("query GetUser(")
    assert "#   API_EXAMPLE_COM_COOKIE" in text


@pytest.mark.parametrize(
    ("name", "sensitive"),
    [
        ("Authorization", True),
        ("Cookie", True),
        ("X-Api-Key", True),
        ("apiKey", True),
        ("api_key", True),
        ("x-csrf-token", True),
        ("sessionToken", True),
        ("sig", True),
        ("password", True),
        ("Accept", False),
        ("limit", False),
        ("keyword", False),
        ("monkey", False),
        ("page", False),
    ],
)
def test_is_sensitive(name: str, sensitive: bool) -> None:
    assert is_sensitive(name) is sensitive


def har_with(*requests: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "request": {"method": method, "url": url, "headers": []},
            "response": {"status": 200, "content": {"mimeType": "application/json"}},
        }
        for method, url in requests
    ]


def test_ids_skip_record_segments_and_stay_valid() -> None:
    result = endpoints_from_har(
        har_with(
            ("GET", "https://x.io/api/v3/users/123/orders/9f86d081884c7d659a2feaa0c55ad015"),
            ("GET", "https://x.io/"),
            ("GET", "https://x.io/" + "/".join(["segment"] * 20)),
            ("GET", "https://203.0.113.7:8443/status"),
        )
    )
    assert [e.id for e in result.endpoints] == [
        "users_orders",
        "x_io",
        result.endpoints[2].id,
        "status",
    ]
    assert len(result.endpoints[2].id) <= 48
    assert result.hosts["203.0.113.7:8443"] == 1


def test_numeric_hosts_get_a_valid_variable_name() -> None:
    entry = har_with(("GET", "https://203.0.113.7/data?token=abc"))
    result = endpoints_from_har(entry)
    assert result.variables == ["HOST_203_0_113_7_TOKEN"]


def test_skips_entries_that_are_not_json_apis() -> None:
    weird: list[dict[str, Any]] = [
        {"request": "nope"},
        {
            "request": {"method": "GET", "url": "data:application/json,{}"},
            "response": {"status": 200, "content": {"mimeType": "application/json"}},
        },
        {
            "request": {"method": "GET", "url": "https://x.io:bad/"},
            "response": {"status": 200, "content": {"mimeType": "application/json"}},
        },
        {
            "request": {"method": "HEAD", "url": "https://x.io/"},
            "response": {"status": 200, "content": {"mimeType": "application/json"}},
        },
        {
            "request": {"method": "GET", "url": "https://x.io/"},
            "response": {"status": 304, "content": {"mimeType": "application/json"}},
        },
    ]
    assert endpoints_from_har(weird).endpoints == []


def test_load_har_errors(tmp_path: Path) -> None:
    broken = tmp_path / "broken.har"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(HarError, match="not a HAR file"):
        load_har(broken)
    other = tmp_path / "other.har"
    other.write_text(json.dumps({"entries": []}), encoding="utf-8")
    with pytest.raises(HarError, match=r"no log\.entries"):
        load_har(other)


def test_load_har_accepts_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "bom.har"
    path.write_bytes(b"\xef\xbb\xbf" + SAMPLE.read_bytes())
    assert len(load_har(path)) == 14


def test_tokens_are_caught_by_their_shape() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    entry = har_with(("GET", f"https://x.io/me?state={jwt}"))
    entry[0]["request"]["headers"] = [{"name": "X-Firebase-AppCheck", "value": jwt}]
    endpoint = endpoints_from_har(entry).endpoints[0]
    assert endpoint.params == {"state": "${X_IO_STATE}"}
    assert endpoint.headers == {"X-Firebase-AppCheck": "${X_IO_X_FIREBASE_APP_CHECK}"}
