from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from tests.helpers import API, env_of, make_endpoint, no_sleep
from tiltmeter.core.fetch import USER_AGENT, Expander, observe_endpoint
from tiltmeter.errors import MissingVariableError

URL = f"{API}/v1/items"


async def observe(endpoint: Any = None, env: Any = None, **kwargs: Any) -> Any:
    async with httpx.AsyncClient() as client:
        return await observe_endpoint(
            client, endpoint or make_endpoint(), env=env or env_of(), sleep=no_sleep, **kwargs
        )


def test_expander() -> None:
    expander = Expander(env_of(TOKEN="s3cr3t-token", SHORT="ab"))
    assert expander.expand("Bearer ${TOKEN}") == "Bearer s3cr3t-token"
    assert expander.expand("${MISSING:-fallback}/${SHORT}") == "fallback/ab"
    assert expander.expand_value({"a": ["${TOKEN}", 1, None]}) == {"a": ["s3cr3t-token", 1, None]}
    assert expander.scrub("token=s3cr3t-token&x=ab") == "token=***&x=ab"
    with pytest.raises(MissingVariableError, match="NOPE"):
        expander.expand("${NOPE}")


@respx.mock
async def test_successful_json_observation() -> None:
    route = respx.get(URL).respond(
        200,
        json={"id": 1, "tags": ["a"]},
        headers={"content-type": "application/json; charset=utf-8"},
    )
    observation = await observe()
    assert observation.error is None
    assert observation.status_code == 200
    assert observation.media_type == "application/json"
    assert observation.json_ok
    assert observation.samples == 1
    assert observation.shape is not None
    assert set(observation.shape.properties) == {"id", "tags"}
    assert observation.latency_ms is not None
    assert observation.excerpt is None
    assert route.calls.last.request.headers["user-agent"] == USER_AGENT


@respx.mock
async def test_placeholders_are_expanded_everywhere() -> None:
    route = respx.post(f"{API}/graphql").respond(200, json={"data": {}})
    endpoint = make_endpoint(
        url="${BASE}/graphql",
        method="post",
        headers={"Authorization": "Bearer ${TOKEN}", "User-Agent": "custom"},
        params={"key": "${TOKEN}", "limit": 3},
        body={"query": "{ viewer { login } }", "variables": {"token": "${TOKEN}"}},
    )
    observation = await observe(endpoint, env_of(BASE=API, TOKEN="tok-123456"))
    assert observation.json_ok
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-123456"
    assert request.headers["user-agent"] == "custom"
    assert request.url.params["key"] == "tok-123456"
    assert request.url.params["limit"] == "3"
    assert json.loads(request.content)["variables"] == {"token": "tok-123456"}


async def test_missing_variable_is_an_error_observation() -> None:
    observation = await observe(make_endpoint(headers={"Authorization": "Bearer ${TOKEN}"}))
    assert observation.error == "environment variable TOKEN is not set"
    assert observation.status_code is None


@respx.mock
async def test_retries_transient_statuses_honouring_retry_after() -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"retry-after": "2"}),
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    async with httpx.AsyncClient() as client:
        observation = await observe_endpoint(
            client, make_endpoint(retries=2), env=env_of(), sleep=sleep
        )
    assert observation.status_code == 200
    assert delays == [2.0, 1.0]


@respx.mock
async def test_gives_up_and_reports_the_last_status() -> None:
    route = respx.get(URL).respond(503)
    observation = await observe(make_endpoint(retries=2))
    assert observation.status_code == 503
    assert observation.error is None
    assert not observation.json_ok
    assert route.call_count == 3


@respx.mock
async def test_timeouts_become_errors() -> None:
    route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    observation = await observe(make_endpoint(retries=1, timeout=2.5))
    assert observation.error == "timed out after 2.5s"
    assert route.call_count == 2


@respx.mock
async def test_transport_errors_are_scrubbed_of_secrets() -> None:
    respx.get(url__startswith=URL).mock(
        side_effect=httpx.ConnectError("cannot reach https://api.test/v1/items?key=SUPERSECRET")
    )
    endpoint = make_endpoint(params={"key": "${API_KEY}"})
    observation = await observe(endpoint, env_of(API_KEY="SUPERSECRET"))
    assert observation.error is not None
    assert "SUPERSECRET" not in observation.error
    assert "***" in observation.error


@respx.mock
async def test_non_json_bodies() -> None:
    respx.get(URL).respond(200, text="<html>login</html>", headers={"content-type": "text/html"})
    observation = await observe()
    assert observation.status_code == 200
    assert observation.media_type == "text/html"
    assert not observation.json_ok
    assert observation.shape is None


@respx.mock
async def test_samples_are_merged_and_a_failing_sample_wins() -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"a": 1}),
            httpx.Response(200, json={"a": 2, "b": 1}),
            httpx.Response(200, json={"a": 3}),
        ]
    )
    observation = await observe(make_endpoint(sample_size=3))
    assert observation.samples == 3
    assert observation.shape is not None
    assert observation.shape.count == 3
    assert not observation.shape.is_required("b")

    respx.get(URL).mock(side_effect=[httpx.Response(200, json={}), httpx.Response(500)])
    mixed = await observe(make_endpoint(sample_size=2))
    assert mixed.status_code == 500
    assert not mixed.json_ok


@respx.mock
async def test_oversized_bodies_are_rejected() -> None:
    respx.get(URL).respond(200, json={"blob": "x" * 5000})
    observation = await observe(make_endpoint(max_body_bytes=1000))
    assert observation.error == "response body is larger than 1000 bytes"


@respx.mock
async def test_excerpt_is_kept_on_request_and_scrubbed() -> None:
    respx.get(url__startswith=URL).respond(200, json={"echo": "key=TOPSECRET1"})
    endpoint = make_endpoint(params={"k": "${K}"})
    observation = await observe(endpoint, env_of(K="TOPSECRET1"), keep_excerpt=True)
    assert observation.excerpt is not None
    assert "TOPSECRET1" not in observation.excerpt
    assert "***" in observation.excerpt


@respx.mock
async def test_the_query_string_written_in_the_url_is_sent() -> None:
    route = respx.get(url__startswith=URL).respond(200, json={})
    await observe(make_endpoint(url=f"{URL}?limit=10&expand=data"))
    assert route.calls.last.request.url.query == b"limit=10&expand=data"
    # Parameters join it, and one of the same name replaces what the URL says.
    await observe(make_endpoint(url=f"{URL}?limit=10&page=1", params={"page": 2, "q": "a b"}))
    assert dict(route.calls.last.request.url.params) == {"limit": "10", "page": "2", "q": "a b"}


OTHER = "https://cdn.other.test"
SECRET = "sekret-value"
KEYED = {"X-Api-Key": "${KEY}", "Authorization": "Bearer ${KEY}", "User-Agent": "custom"}


def redirect(status: int, location: str) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


@respx.mock
async def test_redirects_on_the_same_server_keep_the_headers() -> None:
    respx.get(URL).mock(return_value=redirect(302, "/v2/items"))
    moved = respx.get(f"{API}/v2/items").respond(200, json={"ok": True})
    observation = await observe(make_endpoint(headers=KEYED), env_of(KEY=SECRET))
    assert observation.status_code == 200
    assert observation.json_ok
    assert moved.calls.last.request.headers["x-api-key"] == SECRET
    assert moved.calls.last.request.headers["authorization"] == f"Bearer {SECRET}"

    # The same host answering on https is still the server the config names.
    plain = URL.replace("https://", "http://")
    respx.get(plain).mock(return_value=redirect(301, URL))
    secure = respx.get(URL).respond(200, json={"ok": True})
    await observe(make_endpoint(url=plain, headers=KEYED), env_of(KEY=SECRET))
    assert secure.calls.last.request.headers["x-api-key"] == SECRET


@pytest.mark.parametrize(
    "elsewhere",
    [f"{OTHER}/collect", "https://api.test:8443/collect", "http://api.test/collect"],
    ids=["another host", "another port", "https to http"],
)
@respx.mock
async def test_redirects_elsewhere_leave_the_headers_of_the_config_behind(elsewhere: str) -> None:
    respx.get(URL).mock(return_value=redirect(302, elsewhere))
    # ...and they stay behind when a later redirect comes back.
    other = respx.get(elsewhere).mock(return_value=redirect(302, f"{API}/v2/items"))
    back = respx.get(f"{API}/v2/items").respond(200, json={"ok": True})
    observation = await observe(make_endpoint(headers=KEYED), env_of(KEY=SECRET))
    assert observation.json_ok
    for request in (other.calls.last.request, back.calls.last.request):
        assert "x-api-key" not in request.headers
        assert "authorization" not in request.headers
        assert SECRET not in repr(request.headers.raw)
        assert request.headers["user-agent"] == USER_AGENT


@respx.mock
async def test_a_redirect_never_carries_the_body_to_another_server() -> None:
    endpoint = make_endpoint(method="post", body={"client_secret": "${KEY}"})
    respx.post(URL).mock(return_value=redirect(307, f"{OTHER}/collect"))
    other = respx.route(host="cdn.other.test").respond(200, json={})
    observation = await observe(endpoint, env_of(KEY=SECRET))
    assert observation.error == (
        "redirected to another server (cdn.other.test), which would get the request body; "
        "not followed"
    )
    assert not other.called

    # A 303 asks for a GET without the body, and the same server may have the body again.
    respx.post(URL).mock(return_value=redirect(303, f"{OTHER}/result"))
    assert (await observe(endpoint, env_of(KEY=SECRET))).json_ok
    assert other.calls.last.request.method == "GET"
    assert other.calls.last.request.content == b""
    respx.post(URL).mock(return_value=redirect(307, "/v2/items"))
    moved = respx.post(f"{API}/v2/items").respond(200, json={})
    assert (await observe(endpoint, env_of(KEY=SECRET))).json_ok
    assert json.loads(moved.calls.last.request.content) == {"client_secret": SECRET}


@respx.mock
async def test_a_redirect_loop_is_an_error() -> None:
    route = respx.get(URL).mock(return_value=redirect(302, URL))
    observation = await observe()
    assert observation.error == "redirected more than 20 times"
    assert route.call_count == 21


@respx.mock
async def test_failures_nobody_planned_for_are_errors_too() -> None:
    respx.get(URL).mock(
        side_effect=lambda request: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
        )
    )
    garbled = await observe()
    assert garbled.error is not None
    assert garbled.error.startswith("request failed (DecodingError)")

    newline = await observe(make_endpoint(url="${BASE}/v1/items"), env_of(BASE=f"{API}\n"))
    assert newline.error is not None
    assert newline.error.startswith("the URL is not valid: ")

    deep = b"[" * 100_000 + b"]" * 100_000
    respx.get(URL).respond(200, content=deep, headers={"content-type": "application/json"})
    nested = await observe()
    assert nested.error is None
    assert nested.status_code == 200
    assert not nested.json_ok

    respx.get(URL).mock(side_effect=RuntimeError("no route for key=SUPERSECRET"))
    endpoint = make_endpoint(params={"key": "${API_KEY}"})
    odd = await observe(endpoint, env_of(API_KEY="SUPERSECRET"))
    assert odd.error == "request failed (RuntimeError): no route for key=***"
