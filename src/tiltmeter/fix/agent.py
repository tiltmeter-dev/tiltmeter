"""The fix agent: a LangChain Deep Agent whose file tools stop at one worktree.

It can list, read, glob, grep, edit and write files in the worktree, and nothing
else: no shell, no subagents, no deletes. Paths matching ``fix.ignore`` and the
``.git`` folder are denied. Scratch files the harness writes for itself, such as
offloaded tool results or conversation summaries, stay in memory, so the diff
only ever shows real edits.

``deepagents`` and ``langchain`` come with the ``fix`` extra and are only imported
when an agent is built.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from tiltmeter.errors import FixError
from tiltmeter.fix.ignore import deny_globs
from tiltmeter.fix.models import FixReport, FixRequest
from tiltmeter.fix.prompts import SYSTEM_PROMPT, render_fix_request

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command

    from tiltmeter.config import FixConfig

INSTALL_HINT: Final = 'the fix agent needs the "fix" extra: pip install "tiltmeter[fix]"'
SCRATCH: Final = "/.tiltmeter-agent/"
HIDDEN_TOOLS: Final = frozenset({"task", "execute", "delete", "write_todos"})


class FixAgent(Protocol):
    """Anything that edits a checked-out repository to follow a drift event."""

    @property
    def model_name(self) -> str: ...

    async def run(self, request: FixRequest, root: Path) -> FixReport: ...


def _tool_name(tool: Any) -> str | None:
    name = tool.get("name") if isinstance(tool, Mapping) else getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def hidden_tools(names: frozenset[str]) -> AgentMiddleware[Any, Any, Any]:
    """Middleware that keeps ``names`` away from the model and refuses them if called."""
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import ToolMessage

    class HiddenTools(AgentMiddleware[Any, Any, Any]):
        async def awrap_model_call(
            self,
            request: ModelRequest[Any],
            handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
        ) -> ModelResponse[Any]:
            tools = [tool for tool in request.tools if _tool_name(tool) not in names]
            return await handler(request.override(tools=tools))

        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        ) -> ToolMessage | Command[Any]:
            name = request.tool_call["name"]
            if name in names:
                return ToolMessage(
                    content=f"Error: {name} is not available.",
                    tool_call_id=request.tool_call["id"] or "",
                    name=name,
                    status="error",
                )
            return await handler(request)

    return HiddenTools()


def parse_report(result: Any) -> FixReport:
    """The structured response at the end of an agent run."""
    report = result.get("structured_response") if isinstance(result, Mapping) else None
    if isinstance(report, FixReport):
        return report
    if isinstance(report, Mapping):
        return FixReport.model_validate(report)
    raise FixError("the agent stopped without reporting what it changed")


class DeepFixAgent:
    """A :class:`FixAgent` built with ``deepagents.create_deep_agent``."""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        model_name: str,
        max_model_calls: int,
        ignore: Sequence[str] = (),
    ) -> None:
        self._model = model
        self._model_name = model_name
        self._max_model_calls = max_model_calls
        self._ignore = list(ignore)

    @property
    def model_name(self) -> str:
        return self._model_name

    @classmethod
    def from_config(cls, config: FixConfig) -> DeepFixAgent:
        """Build the ``ChatAnthropic`` agent described by the ``fix`` config section."""
        try:
            import deepagents  # noqa: F401
        except ImportError as exc:
            raise FixError(INSTALL_HINT) from exc
        from langchain_anthropic import ChatAnthropic

        options: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "timeout": config.timeout,
            "max_retries": 2,
        }
        if config.effort is not None:
            options["effort"] = config.effort
            options["thinking"] = {"type": "adaptive"}
        return cls(
            ChatAnthropic(**options),
            model_name=config.model,
            max_model_calls=config.max_model_calls,
            ignore=config.ignore,
        )

    def build(self, root: Path) -> Any:
        """The compiled agent, with its file tools rooted at ``root``."""
        from deepagents import FilesystemPermission, create_deep_agent
        from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
        from langchain.agents.middleware import ModelCallLimitMiddleware

        backend = CompositeBackend(
            default=FilesystemBackend(root_dir=root, virtual_mode=True),
            routes={SCRATCH: StateBackend()},
            artifacts_root=SCRATCH.rstrip("/"),
        )
        denied = FilesystemPermission(
            operations=["read", "write"], paths=deny_globs(self._ignore), mode="deny"
        )
        return create_deep_agent(
            self._model,
            system_prompt=SYSTEM_PROMPT,
            backend=backend,
            permissions=[denied],
            response_format=FixReport,
            middleware=[
                hidden_tools(HIDDEN_TOOLS),
                ModelCallLimitMiddleware(run_limit=self._max_model_calls, exit_behavior="error"),
            ],
            name="tiltmeter-fix",
        )

    async def run(self, request: FixRequest, root: Path) -> FixReport:
        from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError

        agent = self.build(root)
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": render_fix_request(request)}]},
                config={
                    "run_name": "tiltmeter.fix",
                    "tags": ["tiltmeter"],
                    "metadata": {"endpoint_id": request.endpoint_id, "check_id": request.check_id},
                },
            )
        except ModelCallLimitExceededError as exc:
            raise FixError(
                f"the agent used its {self._max_model_calls} model calls without finishing "
                "(raise fix.max_model_calls for large repositories)"
            ) from exc
        return parse_report(result)
