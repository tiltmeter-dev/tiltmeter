"""Call an endpoint and turn what comes back into an :class:`Observation`.

Placeholders like ``${STRIPE_API_KEY}`` are expanded here, right before the
request, and every value that came from the environment is scrubbed from error
messages so a failing check never leaks a credential into logs or history.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from driftwatch import __version__
from driftwatch.core.models import Observation
from driftwatch.core.shape import Shape
from driftwatch.errors import MissingVariableError

if TYPE_CHECKING:
    from driftwatch.config import EndpointConfig

type EnvLookup = Callable[[str], str | None]
type Sleep = Callable[[float], Awaitable[None]]

USER_AGENT: Final = f"driftwatch/{__version__}"
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
EXCERPT_CHARS: Final = 4000
_MAX_RETRY_AFTER: Final = 10.0
_MIN_SECRET_LENGTH: Final = 4
_VARIABLE: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class FetchError(Exception):
    """A request that failed for good (after retries)."""


class Expander:
    """Expands ``${VAR}`` / ``${VAR:-default}`` and remembers which secrets it used."""

    def __init__(self, lookup: EnvLookup) -> None:
        self._lookup = lookup
        self._secrets: set[str] = set()

    def expand(self, template: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            value = self._lookup(name)
            if value:
                if len(value) >= _MIN_SECRET_LENGTH:
                    self._secrets.add(value)
                return value
            if default is not None:
                return default
            raise MissingVariableError(name)

        return _VARIABLE.sub(substitute, template)

    def expand_value(self, value: Any) -> Any:
        """Expand placeholders inside any JSON-like value (dicts, lists, strings)."""
        if isinstance(value, str):
            return self.expand(value)
        if isinstance(value, list):
            return [self.expand_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.expand_value(item) for key, item in value.items()}
        return value

    def scrub(self, text: str) -> str:
        """Replace every expanded secret in ``text`` with ``***``."""
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


@dataclass(frozen=True, slots=True)
class _Response:
    status_code: int
    media_type: str | None
    body: bytes
    elapsed_ms: float
    retry_after: float | None


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), _MAX_RETRY_AFTER)
    except ValueError:
        return None  # an HTTP date; fall back to exponential backoff


def _backoff(attempt: int) -> float:
    return float(min(0.5 * 2**attempt, 8.0))


async def _request_once(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: httpx.Headers,
    params: Mapping[str, str],
    body: Any,
    request_timeout: float,
    max_bytes: int,
) -> _Response:
    started = time.perf_counter()
    async with client.stream(
        method, url, headers=headers, params=params, json=body, timeout=request_timeout
    ) as response:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise FetchError(f"response body is larger than {max_bytes} bytes")
            chunks.append(chunk)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return _Response(
        status_code=response.status_code,
        media_type=_media_type(response.headers.get("content-type")),
        body=b"".join(chunks),
        elapsed_ms=elapsed_ms,
        retry_after=_retry_after(response.headers.get("retry-after")),
    )


async def _send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: httpx.Headers,
    params: Mapping[str, str],
    body: Any,
    request_timeout: float,
    retries: int,
    max_bytes: int,
    sleep: Sleep,
) -> _Response:
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            response = await _request_once(
                client,
                method,
                url,
                headers=headers,
                params=params,
                body=body,
                request_timeout=request_timeout,
                max_bytes=max_bytes,
            )
        except httpx.TimeoutException:
            if last:
                raise FetchError(f"timed out after {request_timeout:g}s") from None
            await sleep(_backoff(attempt))
            continue
        except httpx.TransportError as exc:
            if last:
                raise FetchError(f"request failed ({type(exc).__name__}): {exc}") from None
            await sleep(_backoff(attempt))
            continue
        if response.status_code in RETRY_STATUSES and not last:
            delay = response.retry_after
            await sleep(delay if delay is not None else _backoff(attempt))
            continue
        return response
    raise AssertionError("unreachable")  # pragma: no cover


async def observe_endpoint(
    client: httpx.AsyncClient,
    endpoint: EndpointConfig,
    *,
    env: EnvLookup,
    keep_excerpt: bool = False,
    sleep: Sleep = asyncio.sleep,
) -> Observation:
    """Request ``endpoint`` ``sample_size`` times and infer the shape of the bodies."""
    expander = Expander(env)
    try:
        url = expander.expand(endpoint.url)
        headers = httpx.Headers({"User-Agent": USER_AGENT})
        for name, value in endpoint.headers.items():
            headers[name] = expander.expand(value)
        params = {name: expander.expand(value) for name, value in endpoint.params.items()}
        body = expander.expand_value(endpoint.body)
    except MissingVariableError as exc:
        return Observation(error=str(exc))

    shape = Shape()
    statuses: list[int] = []
    latencies: list[float] = []
    media_type: str | None = None
    excerpt: str | None = None
    parsed_ok = True
    samples = 0
    for _ in range(endpoint.sample_size):
        try:
            response = await _send(
                client,
                endpoint.method,
                url,
                headers=headers,
                params=params,
                body=body,
                request_timeout=endpoint.timeout,
                retries=endpoint.retries,
                max_bytes=endpoint.max_body_bytes,
                sleep=sleep,
            )
        except FetchError as exc:
            return Observation(error=expander.scrub(str(exc)))
        statuses.append(response.status_code)
        latencies.append(response.elapsed_ms)
        media_type = media_type or response.media_type
        if keep_excerpt and excerpt is None:
            excerpt = expander.scrub(response.body.decode("utf-8", "replace")[:EXCERPT_CHARS])
        if 200 <= response.status_code < 300:
            try:
                document = json.loads(response.body)
            except ValueError:
                parsed_ok = False
            else:
                shape.add(document)
                samples += 1

    status = next((code for code in statuses if not 200 <= code < 300), statuses[0])
    json_ok = 200 <= status < 300 and parsed_ok and samples > 0
    return Observation(
        status_code=status,
        media_type=media_type,
        latency_ms=statistics.median(latencies),
        json_ok=json_ok,
        shape=shape if json_ok else None,
        samples=samples,
        excerpt=excerpt,
    )
