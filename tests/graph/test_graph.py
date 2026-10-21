from __future__ import annotations

import json

import httpx
import respx

from tests.helpers import API, FakeClock, env_of, make_config, make_endpoint, no_sleep
from tiltmeter.core.store import Store
from tiltmeter.graph import DriftContext, build_graph

URL = f"{API}/v1/items"


def test_graph_shape() -> None:
    graph = build_graph().get_graph()
    nodes = set(graph.nodes)
    assert {"observe", "load_baseline", "compare", "explain", "persist", "notify"} <= nodes
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("observe", "persist") in edges  # fetch failures skip straight to persist
    assert ("compare", "explain") in edges
    assert ("persist", "notify") in edges


@respx.mock
async def test_state_never_carries_secrets_or_bodies() -> None:
    respx.get(url__startswith=URL).respond(200, json={"email": "ada@example.com", "id": 1})
    endpoint = make_endpoint(
        headers={"Authorization": "Bearer ${TOKEN}"}, params={"key": "${TOKEN}"}
    )
    config = make_config(endpoint)
    async with httpx.AsyncClient() as client:
        context = DriftContext(
            config=config,
            store=Store(":memory:"),
            http=client,
            env=env_of(TOKEN="very-secret-token"),
            clock=FakeClock(),
            sleep=no_sleep,
        )
        state = await build_graph().ainvoke(
            {"endpoint_id": "items", "use_llm": False, "send_notifications": False, "notes": []},
            context=context,
        )
    dumped = json.dumps(state, default=repr)
    assert "very-secret-token" not in dumped
    assert "ada@example.com" not in dumped  # response values are not part of the state
    assert state["result"].first_run
    assert state["result"].check_id == 1
