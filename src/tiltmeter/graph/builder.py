"""Wire the nodes into a compiled LangGraph ``StateGraph``.

::

    START → observe ─┬─ (fetch failed) ──────────────────────────────┐
                     └→ load_baseline → compare ─┬→ explain ─┐       │
                                                 └───────────┴→ persist ─┬→ notify → END
                                                                         └────────→ END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from tiltmeter.graph import nodes
from tiltmeter.graph.state import DriftContext, DriftState


def build_graph() -> CompiledStateGraph[DriftState, DriftContext, Any, Any]:
    """The per-endpoint drift check as a compiled graph (no checkpointer: runs are short)."""
    graph = StateGraph(DriftState, context_schema=DriftContext)
    graph.add_node("observe", nodes.observe)
    graph.add_node("load_baseline", nodes.load_baseline)
    graph.add_node("compare", nodes.compare)
    graph.add_node("explain", nodes.explain)
    graph.add_node("persist", nodes.persist)
    graph.add_node("notify", nodes.notify)
    graph.add_edge(START, "observe")
    graph.add_conditional_edges("observe", nodes.after_observe, ["load_baseline", "persist"])
    graph.add_edge("load_baseline", "compare")
    graph.add_conditional_edges("compare", nodes.after_compare, ["explain", "persist"])
    graph.add_edge("explain", "persist")
    graph.add_conditional_edges("persist", nodes.after_persist, ["notify", END])
    graph.add_edge("notify", END)
    return graph.compile(name="tiltmeter")
