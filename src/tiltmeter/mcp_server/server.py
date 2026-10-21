"""MCP server: lets agents ask whether an API drifted before they rely on it.

Tools are async on purpose: they run on the event loop, so the SQLite history is
never touched from two threads at once.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations

from tiltmeter import __version__
from tiltmeter.core.models import (
    CheckResult,
    DriftStatus,
    EndpointSummary,
    SchemaReport,
    Severity,
    StoredChange,
)
from tiltmeter.errors import TiltmeterError
from tiltmeter.service import DriftService

INSTRUCTIONS: Final = """\
Tiltmeter watches the HTTP APIs listed in its config and learns the shape of their JSON \
responses. Before you rely on one of those APIs, call has_drifted to learn whether its \
responses changed recently, and get_schema to see which fields to expect. Use check_api to \
probe an API right now and get_changes to read its change history. Paths use JSONPath-like \
notation: $ is the response body and [*] means every item of an array.\
"""

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
PROBE: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
MAX_CHANGES: Final = 500


def _guard[T](call: Callable[[], T]) -> T:
    """Turn expected failures (unknown endpoint, bad `since`) into readable tool errors."""
    try:
        return call()
    except (TiltmeterError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def create_server(service: DriftService, *, close_service: bool = False) -> MCPServer:
    """Build the MCP server around a service. ``close_service`` closes it on shutdown."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            if close_service:
                await service.aclose()

    server = MCPServer(
        "tiltmeter",
        title="Tiltmeter",
        description="API schema drift monitor for the APIs your agents depend on.",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
        lifespan=lifespan,
    )

    @server.tool(annotations=READ_ONLY)
    async def list_endpoints() -> list[EndpointSummary]:
        """List the APIs Tiltmeter watches, with the outcome of each one's last check."""
        return service.endpoints()

    @server.tool(annotations=READ_ONLY)
    async def has_drifted(endpoint_id: str, since: str | None = None) -> DriftStatus:
        """Tell whether an API's responses changed at warning level or worse recently.

        Call this before relying on an API. `since` takes a duration such as "24h" or
        "7d", or an ISO 8601 timestamp, and defaults to the last 7 days. The answer
        lists the changes, most severe first, and a one-paragraph summary.
        """
        return _guard(lambda: service.drift_status(endpoint_id, since=since))

    @server.tool(annotations=READ_ONLY)
    async def get_schema(endpoint_id: str) -> SchemaReport:
        """Return the JSON Schema of an API's responses, learned from its recent checks.

        Use it to know which fields to expect, their types, which ones are optional
        (not in `required`) and the values seen for enum-like fields.
        """
        return _guard(lambda: service.schema(endpoint_id))

    @server.tool(annotations=READ_ONLY)
    async def get_changes(
        endpoint_id: str | None = None,
        since: str | None = None,
        min_severity: Severity = Severity.INFO,
        limit: int = 50,
    ) -> list[StoredChange]:
        """List detected changes, newest first.

        Filter by endpoint, by time (`since`: "24h", "7d" or ISO 8601) and by
        minimum severity: info, warning or breaking.
        """
        if endpoint_id is not None:
            _guard(lambda: service.config.endpoint(endpoint_id))
        bounded = max(1, min(limit, MAX_CHANGES))
        return _guard(
            lambda: service.changes(
                endpoint_id, since=since, min_severity=min_severity, limit=bounded
            )
        )

    @server.tool(annotations=PROBE)
    async def check_api(endpoint_id: str, explain: bool = True) -> CheckResult:
        """Call one API now, compare the response with its baseline and record the result.

        With `explain` on and ANTHROPIC_API_KEY set on the server, drift at warning
        level or worse comes with an explanation written by Claude. Configured
        notifications (Slack, webhooks) fire as usual.
        """
        try:
            return await service.check_endpoint(endpoint_id, explain=explain)
        except TiltmeterError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=PROBE)
    async def check_all(explain: bool = True) -> list[CheckResult]:
        """Check every configured API now and return one result per API."""
        return await service.check(explain=explain)

    @server.resource(
        "tiltmeter://endpoints",
        mime_type="application/json",
        description="The watched APIs and the outcome of their last check.",
    )
    async def endpoints_resource() -> list[dict[str, Any]]:
        return [summary.model_dump(mode="json") for summary in service.endpoints()]

    @server.resource(
        "tiltmeter://schema/{endpoint_id}",
        mime_type="application/json",
        description="JSON Schema learned for one API's responses.",
    )
    async def schema_resource(endpoint_id: str) -> dict[str, Any]:
        try:
            report = service.schema(endpoint_id)
        except TiltmeterError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        if report.json_schema is None:
            raise ResourceNotFoundError(f"no baseline for {endpoint_id} yet")
        return report.json_schema

    return server


def run_stdio(config_path: Path) -> None:
    """Serve over stdio. stdout carries the protocol, so logs go to stderr."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    service = DriftService.from_config_file(config_path)
    create_server(service, close_service=True).run("stdio")
