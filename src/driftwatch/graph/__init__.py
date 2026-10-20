"""LangGraph orchestration of one check: observe, compare, explain, persist, notify.

The graph only sequences steps and routes between them. Every decision about
what counts as drift lives in :mod:`driftwatch.core`.
"""

from driftwatch.graph.builder import build_graph
from driftwatch.graph.state import DriftContext, DriftState

__all__ = ["DriftContext", "DriftState", "build_graph"]
