from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import respx
from mcp import Client, StdioServerParameters

from tests.fix.kit import make_repo
from tests.helpers import API, FakeClock, FakeExplainer
from tests.notices.test_scan import GROQ, KNOWN, LLM, MAIN
from tests.service_kit import make_service
from tiltmeter.mcp_server.server import create_server

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

        listing = await client.read_resource("tiltmeter://endpoints")
        assert '"id": "items"' in listing.contents[0].text  # type: ignore[union-attr]
        learned = await client.read_resource("tiltmeter://schema/items")
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
            await client.read_resource("tiltmeter://schema/items")
        with pytest.raises(Exception, match="unknown endpoint"):
            await client.read_resource("tiltmeter://schema/ghost")


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
    config = tmp_path / "tiltmeter.yaml"
    config.write_text("endpoints:\n  - id: local\n    url: http://127.0.0.1:9/never\n")
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "tiltmeter", "serve", "-c", str(config)]
    )
    async with Client(parameters) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools == TOOLS | {"scan_code"}
        status = await client.call_tool("has_drifted", {"endpoint_id": "local"})
        assert status.structured_content is not None
        assert status.structured_content["drifted"] is False
        assert "never been checked" in status.structured_content["summary"]
        # The project is the folder of the config file.
        (tmp_path / "llm.py").write_text(LLM, encoding="utf-8")
        found = await client.call_tool("scan_code", {})
        assert found.structured_content is not None
        assert [entry["id"] for entry in found.structured_content["affected"]] == [GROQ]


async def test_scan_code_finds_known_changes_in_the_project(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "main.py": MAIN})
    async with make_service() as service, Client(create_server(service, root=repo)) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(listed) == TOOLS | {"scan_code"}
        annotations = listed["scan_code"].annotations
        assert annotations is not None
        assert (annotations.read_only_hint, annotations.open_world_hint) == (True, False)
        assert "Nothing is fetched, sent or changed" in (listed["scan_code"].description or "")

        found = await client.call_tool("scan_code", {})
        assert not found.is_error
        report = found.structured_content
        assert report is not None
        [entry] = report["affected"]
        assert (entry["id"], entry["due"]) == (GROQ, True)
        [match] = entry["matches"]
        assert (match["file"], match["line"], match["term"]) == ("app/llm.py", 8, "llama3-8b-8192")
        assert match["detail"] == "retired; use openai/gpt-oss-20b"
        assert entry["fix_command"] == f"tiltmeter fix --notice {GROQ} --allow-unsigned --repo ."
        assert (report["entries"], report["bundled"], report["unaffected"]) == (KNOWN, True, [])

        everything = await client.call_tool("scan_code", {"include_unaffected": True})
        assert everything.structured_content is not None
        assert len(everything.structured_content["unaffected"]) == KNOWN - 1

        folder = await client.call_tool("scan_code", {"path": "app"})
        assert folder.structured_content is not None
        assert folder.structured_content["affected"][0]["matches"][0]["file"] == "llm.py"


async def test_scan_code_reads_no_folder_but_the_project(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "main.py": MAIN})
    async with make_service() as service, Client(create_server(service, root=repo)) as client:
        for path in ("..", "app/../..", "/", str(tmp_path), "../" + repo.name + "/.."):
            refused = await client.call_tool("scan_code", {"path": path})
            assert refused.is_error, path
            assert "is outside the project folder" in refused.content[0].text  # type: ignore[union-attr]
        not_a_path = await client.call_tool("scan_code", {"path": "app\0"})
        assert not_a_path.is_error
        a_file = await client.call_tool("scan_code", {"path": "main.py"})
        assert a_file.is_error
        assert "is not a folder" in a_file.content[0].text  # type: ignore[union-attr]
        negative = await client.call_tool("scan_code", {"within_days": -1})
        assert negative.is_error
        # The project by its own absolute path is still the project.
        own = await client.call_tool("scan_code", {"path": str(repo)})
        assert not own.is_error
        try:
            (repo / "elsewhere").symlink_to(tmp_path, target_is_directory=True)
        except OSError:  # no right to make links, as on some Windows machines
            return
        linked = await client.call_tool("scan_code", {"path": "elsewhere"})
        assert linked.is_error
        assert "is outside the project folder" in linked.content[0].text  # type: ignore[union-attr]
