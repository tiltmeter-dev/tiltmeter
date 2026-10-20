"""Ask Claude to explain a drift event, through LangChain's ``ChatAnthropic``.

The model gets the classified changes and the field lists of the affected
objects: shapes, never response values, unless ``llm.include_samples`` is on.
Structured outputs (``output_config.format``) make the reply parse into an
:class:`~driftwatch.core.models.Explanation` without forcing a tool call, which
recent Claude models reject.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable

from driftwatch.core.models import TRANSPORT_KINDS, Change, Explanation
from driftwatch.core.paths import PathSyntaxError, Segment, format_path, parse_path
from driftwatch.core.shape import Shape
from driftwatch.errors import DriftwatchError
from driftwatch.llm.prompts import SYSTEM_PROMPT, render_request

if TYPE_CHECKING:
    from driftwatch.config import EndpointConfig, LLMConfig

MAX_AFFECTED_OBJECTS: Final = 8


class ExplanationError(DriftwatchError):
    """The model could not produce a usable explanation."""


@dataclass(frozen=True, slots=True)
class AffectedObject:
    """The fields of an object that contains a change, before and after."""

    path: str
    before: dict[str, str]
    after: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExplanationRequest:
    """Everything the model sees about one drift event."""

    endpoint_id: str
    method: str
    url: str
    description: str | None
    changes: Sequence[Change]
    affected: Sequence[AffectedObject] = field(default_factory=tuple)
    excerpt: str | None = None


class Explainer(Protocol):
    """Anything that can turn a drift event into an :class:`Explanation`."""

    @property
    def model_name(self) -> str: ...

    async def explain(self, request: ExplanationRequest) -> Explanation: ...


def build_request(
    endpoint: EndpointConfig,
    changes: Sequence[Change],
    *,
    baseline: Shape | None,
    observed: Shape | None,
    excerpt: str | None = None,
) -> ExplanationRequest:
    """Collect the changes plus the before/after fields of every affected object."""
    affected: list[AffectedObject] = []
    seen: set[tuple[Segment, ...]] = set()
    for change in changes:
        if change.kind in TRANSPORT_KINDS:
            continue
        try:
            segments = parse_path(change.path)
        except PathSyntaxError:
            continue
        parent = tuple(segments[:-1])
        if parent in seen:
            continue
        seen.add(parent)
        before = baseline.at(parent) if baseline is not None else None
        after = observed.at(parent) if observed is not None else None
        before_fields = before.field_types() if before is not None else {}
        after_fields = after.field_types() if after is not None else {}
        if before_fields or after_fields:
            affected.append(AffectedObject(format_path(parent), before_fields, after_fields))
        if len(affected) >= MAX_AFFECTED_OBJECTS:
            break
    return ExplanationRequest(
        endpoint_id=endpoint.id,
        method=endpoint.method,
        url=endpoint.display_url(),
        description=endpoint.description,
        changes=list(changes),
        affected=affected,
        excerpt=excerpt,
    )


def build_messages(request: ExplanationRequest) -> list[BaseMessage]:
    return [SystemMessage(SYSTEM_PROMPT), HumanMessage(render_request(request))]


def parse_output(output: Any) -> Explanation:
    """Validate what the structured-output runnable returned (``include_raw=True``)."""
    if isinstance(output, Explanation):
        return output
    if not isinstance(output, Mapping):
        raise ExplanationError(f"unexpected model output of type {type(output).__name__}")
    raw = output.get("raw")
    metadata = getattr(raw, "response_metadata", None) or {}
    stop_reason = metadata.get("stop_reason")
    if stop_reason == "refusal":
        raise ExplanationError("the model declined to explain this change")
    if stop_reason == "max_tokens":
        raise ExplanationError("the explanation was cut off; raise llm.max_tokens")
    parsed = output.get("parsed")
    if isinstance(parsed, Explanation):
        return parsed
    if isinstance(parsed, Mapping):
        return Explanation.model_validate(parsed)
    error = output.get("parsing_error")
    raise ExplanationError(f"the model reply did not match the schema: {error}")


class ClaudeExplainer:
    """An :class:`Explainer` backed by a LangChain structured-output runnable."""

    def __init__(self, runnable: Runnable[Any, Any], *, model_name: str) -> None:
        self._runnable = runnable
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @classmethod
    def from_config(cls, config: LLMConfig) -> ClaudeExplainer:
        """Build the ``ChatAnthropic`` pipeline described by the ``llm`` config section."""
        from langchain_anthropic import ChatAnthropic  # slow to import; only when needed

        options: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "timeout": config.timeout,
            "max_retries": 2,
        }
        if config.effort is not None:
            # Adaptive thinking with display omitted: no reasoning summaries to pay for.
            options["effort"] = config.effort
            options["thinking"] = {"type": "adaptive"}
        chat = ChatAnthropic(**options)
        structured = chat.with_structured_output(
            Explanation, method="json_schema", include_raw=True
        )
        return cls(structured, model_name=config.model)

    async def explain(self, request: ExplanationRequest) -> Explanation:
        output = await self._runnable.ainvoke(
            build_messages(request),
            config={
                "run_name": "driftwatch.explain",
                "tags": ["driftwatch"],
                "metadata": {"endpoint_id": request.endpoint_id},
            },
        )
        return parse_output(output)


def has_credentials(env: Callable[[str], str | None]) -> bool:
    """True when ``ChatAnthropic`` will find an API key."""
    return bool(env("ANTHROPIC_API_KEY"))
