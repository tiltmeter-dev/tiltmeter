from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import respx
from mcp import Client, StdioServerParameters

from driftwatch.mcp_server.server import create_server
from tests.helpers import API, FakeClock, FakeExplainer
from tests.service_kit import make_service

URL = f"{API}/v1/items"
TOOLS = {"list_endpoints", "has_drifted", "get_schema", "get_changes", "check_api", "check_all"}


async def test_tools_are_described_and_annotated() -> None:
    async with make_service() as service, Client(create_server(service)) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(listed) == TOOLS
        assert listed["has_drifted"].annotations is not None
        assert listed["has_drifted"].annotations.read_only_hint is True
        assert listed["check_api"].annotations is not None
        assert listed["check_api"].annotations.open_world_hint is True
        assert "before relying on an API" in (listed["has_drifted"].description or "")


@respx.mock
async def test_agent_workflow() -> None:
    route = respx.get(URL).respond(200, json={"data": [{"id": 1, "phone": "555"}]})
    clock = FakeClock()
    explainer = FakeExplainer()
    async with (
        make_service(explainer=explainer, clock=clock) as service,
        Client(create_server(service)) as client,
    ):
        first = await client.call_tool("check_api", {"endpoint_id": "items"})
        assert not first.is_error
        assert first.structured_content is not None
        assert first.structured_content["outcome"] == "baseline"

        route.respond(200, json={"data": [{"id": 1, "phone_number": "555"}]})
        clock.advance(minutes=1)
        second = await client.call_tool("check_api", {"endpoint_id": "items"})
        assert second.structured_content is not None
        assert second.structured_content["max_severity"] == "breaking"
        assert second.structured_content["explanation"]["confidence"] == "high"

        status = await client.call_tool("has_drifted", {"endpoint_id": "items"})
        assert status.structured_content is not None
        assert status.structured_content["drifted"] is True
        assert "no longer returned" in status.structured_content["summary"]

        schema = await client.call_tool("get_schema", {"endpoint_id": "items"})
        assert schema.structured_content is not None
        assert schema.structured_content["baseline_checks"] == 2

        changes = await client.call_tool(
            "get_changes", {"endpoint_id": "items", "min_severity": "breaking"}
        )
        assert changes.structured_content is not None
        assert [c["path"] for c in changes.structured_content["result"]] == ["$.data[*].phone"]

        endpoints = await client.call_tool("list_endpoints", {})
        assert endpoints.structured_content is not None
        assert endpoints.structured_content["result"][0]["last_outcome"] == "drift"

        everything = await client.call_tool("check_all", {"explain": False})
        assert everything.structured_content is not None
        assert len(everything.structured_content["result"]) == 1

        listing = await client.read_resource("driftwatch://endpoints")
        assert '"id": "items"' in listing.contents[0].text  # type: ignore[union-attr]
        learned = await client.read_resource("driftwatch://schema/items")
        assert '"$schema"' in learned.contents[0].text  # type: ignore[union-attr]


async def test_errors_are_readable() -> None:
    async with make_service() as service, Client(create_server(service)) as client:
        unknown = await client.call_tool("has_drifted", {"endpoint_id": "ghost"})
        assert unknown.is_error
        assert "unknown endpoint 'ghost'" in unknown.content[0].text  # type: ignore[union-attr]
        bad_since = await client.call_tool(
            "has_drifted", {"endpoint_id": "items", "since": "yesterday"}
        )
        assert bad_since.is_error
        no_endpoint = await client.call_tool("get_changes", {"endpoint_id": "ghost"})
        assert no_endpoint.is_error
        probe = await client.call_tool("check_api", {"endpoint_id": "ghost"})
        assert probe.is_error
        with pytest.raises(Exception, match="no baseline"):
            await client.read_resource("driftwatch://schema/items")
        with pytest.raises(Exception, match="unknown endpoint"):
            await client.read_resource("driftwatch://schema/ghost")


async def test_lifespan_closes_the_service_when_asked() -> None:
    kept = make_service()
    async with Client(create_server(kept)) as client:
        await client.list_tools()
    assert kept.store.list_checks() == []  # still open
    closed = make_service()
    async with Client(create_server(closed, close_service=True)) as client:
        await client.list_tools()
    with pytest.raises(sqlite3.ProgrammingError):
        closed.store.list_checks()
    await kept.aclose()


async def test_stdio_server_end_to_end(tmp_path: Path) -> None:
    config = tmp_path / "driftwatch.yaml"
    config.write_text("endpoints:\n  - id: local\n    url: http://127.0.0.1:9/never\n")
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "driftwatch", "serve", "-c", str(config)]
    )
    async with Client(parameters) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools == TOOLS
        status = await client.call_tool("has_drifted", {"endpoint_id": "local"})
        assert status.structured_content is not None
        assert status.structured_content["drifted"] is False
        assert "never been checked" in status.structured_content["summary"]
