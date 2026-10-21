"""Endpoints from a browser HAR export (DevTools, Network tab, "Export HAR").

``tiltmeter init --from-har`` keeps the requests that got a 2xx JSON response, one
endpoint per method, host and path (and GraphQL operation). Requests that may change
data (POST, PUT, PATCH, DELETE) are left out unless asked for, because a monitor would
replay them on every check; GraphQL queries count as reads.

Credentials never reach the generated file: sensitive headers, query parameters and
JSON body fields become ``${VAR}`` placeholders, and the values the browser sent are not
written anywhere.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml

from tiltmeter.config import EXAMPLE_SETTINGS, EndpointConfig
from tiltmeter.errors import HarError

WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Headers the HTTP client sets itself, or that only mean something inside a browser.
_DROPPED_HEADERS: Final = frozenset(
    {
        "accept-encoding",
        "accept-language",
        "cache-control",
        "connection",
        "content-length",
        "dnt",
        "expect",
        "host",
        "keep-alive",
        "origin",
        "pragma",
        "priority",
        "referer",
        "te",
        "transfer-encoding",
        "upgrade-insecure-requests",
        "user-agent",
        "x-client-data",
    }
)
_DROPPED_PREFIXES: Final = (":", "sec-", "if-")

_SENSITIVE_NAMES: Final = frozenset({"authorization", "proxy-authorization", "cookie"})
_SENSITIVE_PARTS: Final = (
    "apikey",
    "auth",
    "credential",
    "csrf",
    "passwd",
    "password",
    "secret",
    "session",
    "signature",
    "token",
    "xsrf",
)
_SENSITIVE_WORDS: Final = frozenset({"jwt", "key", "otp", "pin", "sid", "sig"})
_AUTH_SCHEME: Final = re.compile(r"^(?:Bearer|Basic|Token|Digest|JWT)\s+", re.IGNORECASE)
# A JSON Web Token is a credential whatever the field holding it is called.
_JWT: Final = re.compile(r"eyJ[\w-]+\.eyJ[\w-]+\.")

# Path segments that identify a record rather than a resource: 42, a UUID, a hash, a token.
_RECORD_SEGMENT: Final = re.compile(
    r"^(?:\d+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{16,}|(?=.*\d)[\w-]{24,})$",
    re.IGNORECASE,
)
_NOISE_SEGMENT: Final = re.compile(r"^(?:api|rest|v\d+(?:\.\d+)*)$", re.IGNORECASE)
_GRAPHQL_OPERATION: Final = re.compile(
    r"(?:^|[\s}])(query|mutation|subscription)\b\s*([A-Za-z_]\w*)?"
)
_MAX_ID_LENGTH: Final = 48


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What ``endpoints_from_har`` found, for the config file and the summary."""

    endpoints: list[EndpointConfig]
    variables: list[str]  # placeholders to set before the first check, sorted
    requests: int  # entries in the HAR
    hosts: dict[str, int]  # endpoints per host
    other_hosts: int  # requests left out by the host filter
    writes: int  # requests left out because they may change data


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    host: str
    url: str  # scheme, host and path: no credentials, query or fragment
    path: str
    headers: list[tuple[str, str]]
    params: dict[str, str]
    body: Any
    read: bool
    operation: str | None  # GraphQL operation name ("" when unnamed), None for plain HTTP

    @property
    def key(self) -> tuple[str, str, str, str | None]:
        return (self.method, self.host, self.path, self.operation)


def load_har(path: Path) -> list[dict[str, Any]]:
    """The entries of a HAR file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HarError(f"{path}: not a HAR file ({exc})") from exc
    log = data.get("log") if isinstance(data, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise HarError(f"{path}: not a HAR file (no log.entries)")
    return [entry for entry in entries if isinstance(entry, dict)]


def endpoints_from_har(
    entries: Sequence[Mapping[str, Any]],
    hosts: Sequence[str] = (),
    *,
    include_writes: bool = False,
) -> ImportResult:
    """One endpoint per JSON API the browser called, with credentials as placeholders.

    ``hosts`` keeps only those hosts and their subdomains.
    """
    wanted = [host.lower().strip(".") for host in hosts if host.strip(".")]
    seen: set[tuple[str, str, str, str | None]] = set()
    ids: set[str] = set()
    variables: set[str] = set()
    endpoints: list[EndpointConfig] = []
    per_host: dict[str, int] = {}
    other_hosts = writes = 0
    for entry in entries:
        request = _request(entry)
        if request is None:
            continue
        if wanted and not any(_within(request.host, host) for host in wanted):
            other_hosts += 1
            continue
        if not request.read and not include_writes:
            writes += 1
            continue
        if request.key in seen:
            continue
        seen.add(request.key)
        endpoint_id = _unique(_endpoint_id(request), ids)
        ids.add(endpoint_id)
        endpoints.append(_endpoint(endpoint_id, request, _Redactor(request.host, variables)))
        per_host[request.host] = per_host.get(request.host, 0) + 1
    return ImportResult(
        endpoints=endpoints,
        variables=sorted(variables),
        requests=len(entries),
        hosts=per_host,
        other_hosts=other_hosts,
        writes=writes,
    )


def render_config(result: ImportResult, source: str) -> str:
    """The ``tiltmeter.yaml`` text for an import, with the usual settings after it."""
    lines = [
        f"# tiltmeter configuration built from {source} by `tiltmeter init --from-har`.",
        "# Review it: drop the endpoints you don't need, and add ignore_paths for fields",
        "# that change on every call. Docs: https://tiltmeter.dev",
    ]
    if result.variables:
        lines += [
            "#",
            "# Credentials were replaced by placeholders. Set these environment variables",
            "# before running `tiltmeter check`:",
            *(f"#   {name}" for name in result.variables),
        ]
    document = {"version": 1, "endpoints": [_as_yaml(endpoint) for endpoint in result.endpoints]}
    body = yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)
    return "\n".join(lines) + "\n\n" + body + "\n" + EXAMPLE_SETTINGS


def is_sensitive(name: str) -> bool:
    """Whether a header, parameter or body field name looks like it holds a credential."""
    lowered = name.lower()
    if lowered in _SENSITIVE_NAMES or any(part in lowered for part in _SENSITIVE_PARTS):
        return True
    return not _SENSITIVE_WORDS.isdisjoint(_slug(name).split("_"))


class _Redactor:
    """Swaps credential values for ``${HOST_NAME}`` placeholders and records the names."""

    def __init__(self, host: str, variables: set[str]) -> None:
        prefix = _slug(host.split(":", 1)[0] if host.count(":") == 1 else host).upper()
        self._prefix = f"HOST_{prefix}" if not prefix or prefix[0].isdigit() else prefix
        self._variables = variables

    def placeholder(self, name: str) -> str:
        variable = f"{self._prefix}_{_slug(name).upper() or 'SECRET'}"
        self._variables.add(variable)
        return "${" + variable + "}"

    def header(self, name: str, value: str) -> str:
        if not _secret_field(name, value):
            return value
        scheme = _AUTH_SCHEME.match(value)
        return (scheme.group(0) if scheme else "") + self.placeholder(name)

    def param(self, name: str, value: str) -> str:
        return self.placeholder(name) if _secret_field(name, value) else value

    def body(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._secret(item, str(key))
                if is_sensitive(str(key)) or (isinstance(item, str) and _JWT.search(item))
                else self.body(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.body(item) for item in value]
        return value

    def _secret(self, value: Any, name: str) -> Any:
        """Everything under a sensitive field, down to the leaves."""
        if isinstance(value, dict):
            return {key: self._secret(item, f"{name}_{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [self._secret(item, name) for item in value]
        if value is None or isinstance(value, bool) or value == "":
            return value
        return self.placeholder(name)


def _secret_field(name: str, value: str) -> bool:
    return bool(value) and (is_sensitive(name) or _JWT.search(value) is not None)


def _request(entry: Mapping[str, Any]) -> _Request | None:
    """The parts of a HAR entry worth monitoring, or None for anything else."""
    request, response = entry.get("request"), entry.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        return None
    method = str(request.get("method", "")).upper()
    if method != "GET" and method not in WRITE_METHODS:
        return None
    status = response.get("status")
    content = response.get("content")
    if not isinstance(status, int) or not 200 <= status < 300:
        return None
    if not isinstance(content, dict) or not _is_json(content.get("mimeType")):
        return None
    try:
        parts = urlsplit(str(request.get("url", "")))
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    host = hostname if port is None else f"{hostname}:{port}"
    path = parts.path or "/"

    body: Any = None
    post = request.get("postData")
    text = post.get("text") if isinstance(post, dict) else None
    if method in WRITE_METHODS and isinstance(text, str) and text.strip():
        try:
            body = json.loads(text)
        except ValueError:
            return None  # a form or file upload: only JSON bodies can be replayed

    params: dict[str, str] = {}
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        params.setdefault(name, value)

    headers: list[tuple[str, str]] = []
    names: set[str] = set()
    for header in request.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name, value = str(header.get("name", "")), str(header.get("value", ""))
        lowered = name.lower()
        if not name or lowered in names or _dropped(lowered, has_body=body is not None):
            continue
        names.add(lowered)
        headers.append((name, value))

    graphql = _graphql(path, body, params)
    return _Request(
        method=method,
        host=host,
        url=urlunsplit((parts.scheme, host, path, "", "")),
        path=path,
        headers=headers,
        params=params,
        body=body,
        read=method == "GET" or (graphql is not None and graphql[0]),
        operation=graphql[1] if graphql else None,
    )


def _graphql(path: str, body: Any, params: Mapping[str, str]) -> tuple[bool, str] | None:
    """(is a query, operation name) for a GraphQL request, None for plain HTTP."""
    source: Mapping[str, Any] = body if isinstance(body, dict) else params
    query = source.get("query")
    text = query.strip() if isinstance(query, str) else ""
    match = _GRAPHQL_OPERATION.search(text) if "{" in text else None
    if not path.rstrip("/").lower().endswith("graphql") and not (match or text.startswith("{")):
        return None
    kind = match.group(1) if match else ("query" if text.startswith("{") else None)
    name = source.get("operationName")
    if not isinstance(name, str) or not name:
        name = (match.group(2) if match else None) or ""
    return kind == "query", name


def _endpoint(endpoint_id: str, request: _Request, redact: _Redactor) -> EndpointConfig:
    description = f"{request.method} {request.url}"
    if request.operation is not None:
        description += f" (GraphQL {request.operation or 'operation'})"
    return EndpointConfig(
        id=endpoint_id,
        description=description,
        url=request.url,
        method=request.method,  # type: ignore[arg-type]  # checked in _request
        headers={name: redact.header(name, value) for name, value in request.headers},
        params={name: redact.param(name, value) for name, value in request.params.items()},
        body=redact.body(request.body),
    )


def _endpoint_id(request: _Request) -> str:
    if request.operation is not None:
        words = ["graphql", request.operation]
    else:
        segments = [s for s in request.path.split("/") if s and not _RECORD_SEGMENT.match(s)]
        words = [s for s in segments if not _NOISE_SEGMENT.match(s)] or segments
        words = [re.sub(r"\.json$", "", word, flags=re.IGNORECASE) for word in words]
        words = words or [request.host]
    if not request.read:
        words.insert(0, request.method.lower())
    return _slug("_".join(words))[:_MAX_ID_LENGTH].strip("_") or "endpoint"


def _unique(base: str, taken: set[str]) -> str:
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}_{number}", number + 1
    return candidate


def _slug(text: str) -> str:
    """``X-Api-Key`` and ``apiKey`` both become ``x_api_key`` and ``api_key``."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def _within(host: str, wanted: str) -> bool:
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name == wanted or name.endswith("." + wanted)


def _dropped(name: str, *, has_body: bool) -> bool:
    if name in _DROPPED_HEADERS or name.startswith(_DROPPED_PREFIXES):
        return True
    return name == "content-type" and not has_body


def _is_json(mime_type: Any) -> bool:
    if not isinstance(mime_type, str):
        return False
    media = mime_type.split(";", 1)[0].strip().lower()
    return media in ("application/json", "text/json", "application/x-json") or media.endswith(
        "+json"
    )


def _as_yaml(endpoint: EndpointConfig) -> dict[str, Any]:
    data = endpoint.model_dump(mode="json", exclude_defaults=True)
    order = ("id", "description", "method", "url", "params", "headers", "body")
    return {key: data[key] for key in order if key in data}


class _Dumper(yaml.SafeDumper):
    """Indented lists, and multi-line strings (GraphQL queries) as ``|`` blocks."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)
