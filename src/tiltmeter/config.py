"""The ``tiltmeter.yaml`` file: which endpoints to watch and how.

Values may contain ``${VAR}`` or ``${VAR:-default}`` placeholders. They are kept
as written in memory and only expanded when a request or notification is sent,
so secrets never sit in the config objects that get logged or traced.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tiltmeter.core.models import ChangeKind, Severity
from tiltmeter.core.paths import PathPattern, PathSyntaxError
from tiltmeter.errors import ConfigError, UnknownEndpointError
from tiltmeter.notices.models import (
    HOST_PATTERN,
    SdkName,
    default_feed_url,
    https_host,
    parse_sdk_name,
)

DEFAULT_CONFIG_FILE = "tiltmeter.yaml"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_FIX_MODEL = "claude-sonnet-5-5"
DEFAULT_FIX_IGNORE = (".env*", "*.pem", "*.key", "secrets*", ".tiltmeter")

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]
# A plain alias rather than a `type` statement: Typer reads it for `fix --agent`.
FixAgentKind = Literal["deepagents", "claude-code", "codex"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _stringify(value: Any) -> Any:
    """YAML turns ``limit: 3`` into an int; headers and params must be strings."""
    if isinstance(value, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in value.items()}
    return value


class EndpointConfig(_Model):
    """One HTTP endpoint to watch."""

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
    url: str
    method: HttpMethod = "GET"
    description: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    timeout: float = Field(default=10.0, gt=0, le=300)
    retries: int = Field(default=2, ge=0, le=10)
    sample_size: int = Field(default=1, ge=1, le=20)
    max_body_bytes: int = Field(default=5_000_000, ge=1_000, le=200_000_000)
    ignore_paths: list[str] = Field(default_factory=list)
    strict: bool = False
    baseline_window: int = Field(default=20, ge=1, le=500)
    latency_factor: float | None = Field(default=3.0, gt=1)
    severity_overrides: dict[ChangeKind, Severity] = Field(default_factory=dict)
    store_samples: bool = False

    @field_validator("method", mode="before")
    @classmethod
    def _upper_method(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("headers", "params", mode="before")
    @classmethod
    def _string_values(cls, value: Any) -> Any:
        return _stringify(value)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://", "${")):
            raise ValueError("must start with http:// or https:// (or a ${VAR} placeholder)")
        return value

    @field_validator("ignore_paths")
    @classmethod
    def _check_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                PathPattern(pattern)
            except PathSyntaxError as exc:
                raise ValueError(str(exc)) from exc
        return value

    def display_url(self) -> str:
        """The URL as written, without query string or fragment (they often hold keys)."""
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class SlackNotifierConfig(_Model):
    """Post to a Slack incoming webhook."""

    type: Literal["slack"]
    webhook_url: str
    min_severity: Severity = Severity.WARNING
    on_error: bool = False


class WebhookNotifierConfig(_Model):
    """POST the check result as JSON to any URL."""

    type: Literal["webhook"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    min_severity: Severity = Severity.WARNING
    on_error: bool = False

    @field_validator("headers", mode="before")
    @classmethod
    def _string_values(cls, value: Any) -> Any:
        return _stringify(value)


type NotifierConfig = Annotated[
    SlackNotifierConfig | WebhookNotifierConfig, Field(discriminator="type")
]


class LLMConfig(_Model):
    """Optional drift explanations written by Claude."""

    enabled: bool = True
    model: str = DEFAULT_MODEL
    effort: Effort | None = "low"
    max_tokens: int = Field(default=2048, ge=256, le=32_000)
    timeout: float = Field(default=60.0, gt=0, le=600)
    min_severity: Severity = Severity.WARNING
    include_samples: bool = False


class FixConfig(_Model):
    """``tiltmeter fix``: the agent that adapts your code to drift."""

    agent: FixAgentKind = "deepagents"
    model: str = DEFAULT_FIX_MODEL
    effort: Effort | None = "medium"
    max_tokens: int = Field(default=16_000, ge=1_024, le=64_000)
    timeout: float = Field(default=120.0, gt=0, le=600)
    max_model_calls: int = Field(default=40, ge=1, le=500)
    branch_prefix: str = Field(
        default="tiltmeter/fix",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9][A-Za-z0-9._\-]*)*$",
    )
    ignore: list[str] = Field(default_factory=lambda: list(DEFAULT_FIX_IGNORE))
    test_command: list[str] | None = Field(default=None, min_length=1)
    test_timeout: float = Field(default=300.0, gt=0, le=3600)
    test_rounds: int = Field(default=1, ge=0, le=3)
    test_env: list[str] = Field(default_factory=list)
    test_network: bool = False

    @field_validator("test_command", mode="before")
    @classmethod
    def _split_command(cls, value: Any) -> Any:
        return shlex.split(value) if isinstance(value, str) else value

    @field_validator("test_env")
    @classmethod
    def _check_env_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"{name!r} is not an environment variable name")
        return value

    @field_validator("branch_prefix")
    @classmethod
    def _check_branch_prefix(cls, value: str) -> str:
        if ".." in value or value.endswith(".lock"):
            raise ValueError("must be a valid git branch name")
        return value

    @field_validator("ignore")
    @classmethod
    def _check_ignore(cls, value: list[str]) -> list[str]:
        for pattern in value:
            parts = pattern.strip("/").split("/")
            if not pattern.strip("/") or ".." in parts or pattern.startswith("~"):
                raise ValueError(f"{pattern!r} must be a path pattern inside the repository")
        return value


class SubscriptionConfig(_Model):
    """A provider whose changes this repository follows: its notices, or its spec."""

    provider: str | None = Field(default=None, max_length=253, pattern=HOST_PATTERN)
    feed: str | None = None
    since: datetime | None = None
    openapi: str | None = None
    baseline: str | None = None
    base_url: str | None = None
    sdks: list[SdkName] = Field(default_factory=list, max_length=20)
    versioned: bool = False
    max_bytes: int = Field(default=32_000_000, ge=10_000, le=200_000_000)

    @field_validator("feed", "openapi", "baseline", "base_url")
    @classmethod
    def _check_https(cls, value: str | None) -> str | None:
        if value is not None and https_host(value) is None:
            raise ValueError("must be an https URL")
        return value

    @field_validator("sdks", mode="before")
    @classmethod
    def _sdk_names(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [parse_sdk_name(item) if isinstance(item, str) else item for item in value]
        return value

    @field_validator("since", mode="before")
    @classmethod
    def _midnight_utc(cls, value: Any) -> Any:
        if isinstance(value, date) and not isinstance(value, datetime):
            return datetime(value.year, value.month, value.day, tzinfo=UTC)
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @model_validator(mode="after")
    def _provider_or_feed(self) -> SubscriptionConfig:
        if self.openapi is not None:
            if self.feed is not None or self.since is not None:
                raise ValueError("openapi does not go with feed or since")
        elif self.provider is None and self.feed is None:
            raise ValueError("needs a provider, a feed, or an openapi spec")
        elif self.baseline is not None or self.base_url is not None or self.sdks or self.versioned:
            raise ValueError("baseline, base_url, sdks and versioned only go with openapi")
        return self

    @property
    def feed_url(self) -> str:
        """What to read: the spec, the feed given, or the provider's well-known feed."""
        if self.openapi is not None:
            return self.openapi
        if self.feed is not None:
            return self.feed
        return default_feed_url(self.provider or "")

    @property
    def label(self) -> str:
        return self.provider or https_host(self.feed or self.openapi or "") or "feed"


class Config(_Model):
    """The whole configuration file."""

    version: Literal[1] = 1
    state_dir: str = ".tiltmeter"
    concurrency: int = Field(default=5, ge=1, le=64)
    keep_checks: int = Field(default=500, ge=10, le=100_000)
    severity_overrides: dict[ChangeKind, Severity] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    fix: FixConfig = Field(default_factory=FixConfig)
    notify: list[NotifierConfig] = Field(default_factory=list)
    subscriptions: list[SubscriptionConfig] = Field(default_factory=list)
    endpoints: list[EndpointConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Config:
        if not self.endpoints and not self.subscriptions:
            raise ValueError("add at least one endpoint, or a subscription to a provider")
        seen: set[str] = set()
        for endpoint in self.endpoints:
            if endpoint.id in seen:
                raise ValueError(f"duplicate endpoint id {endpoint.id!r}")
            seen.add(endpoint.id)
        return self

    @property
    def endpoint_ids(self) -> list[str]:
        return [endpoint.id for endpoint in self.endpoints]

    def endpoint(self, endpoint_id: str) -> EndpointConfig:
        for endpoint in self.endpoints:
            if endpoint.id == endpoint_id:
                return endpoint
        raise UnknownEndpointError(endpoint_id, self.endpoint_ids)

    def select(self, endpoint_ids: Sequence[str] | None = None) -> list[EndpointConfig]:
        """The endpoints with these ids, in the given order; all of them when empty."""
        if not endpoint_ids:
            return list(self.endpoints)
        return [self.endpoint(endpoint_id) for endpoint_id in dict.fromkeys(endpoint_ids)]

    def overrides_for(self, endpoint: EndpointConfig) -> dict[ChangeKind, Severity]:
        """Global severity overrides, with the endpoint's own taking precedence."""
        return {**self.severity_overrides, **endpoint.severity_overrides}


def load_config(path: Path) -> Config:
    """Read and validate a config file, with readable errors."""
    if not path.is_file():
        raise ConfigError(f"config file not found: {path} (run `tiltmeter init` to create one)")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: expected a mapping with an `endpoints` or `subscriptions` list "
            "at the top level"
        )
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid configuration\n{format_validation_error(exc)}") from exc


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def resolve_state_dir(config: Config, config_path: Path) -> Path:
    """``state_dir`` resolved relative to the config file's folder."""
    state_dir = Path(config.state_dir).expanduser()
    if not state_dir.is_absolute():
        state_dir = config_path.resolve().parent / state_dir
    return state_dir


_EXAMPLE_ENDPOINTS = """\
# tiltmeter configuration. Docs: see README.md
version: 1

endpoints:
  # A public API, no credentials needed.
  - id: github_repo
    description: Repository metadata from the GitHub REST API
    url: https://api.github.com/repos/langchain-ai/langchain
    headers:
      Accept: application/vnd.github+json

  # Secrets come from environment variables and are expanded at request time.
  # - id: stripe_customers
  #   url: https://api.stripe.com/v1/customers
  #   params:
  #     limit: 10
  #   headers:
  #     Authorization: Bearer ${STRIPE_API_KEY}
  #   ignore_paths:
  #     - $.data[*].metadata      # free-form, changes all the time
  #   severity_overrides:
  #     field_now_optional: warning

"""

# Everything after the endpoints; `tiltmeter init --from-har` reuses it.
EXAMPLE_SETTINGS = """\
# Explanations written by Claude when drift is detected (needs ANTHROPIC_API_KEY).
llm:
  enabled: true
  model: claude-haiku-5-5

# `tiltmeter fix <check id>` lets an agent adapt your code to drift and open a pull
# request. The default agent needs ANTHROPIC_API_KEY and `pip install "tiltmeter[fix]"`;
# `agent: claude-code` or `agent: codex` runs on your Claude Code or Codex login instead.
# fix:
#   agent: deepagents        # or claude-code, or codex
#   model: claude-sonnet-5-5
#   ignore: [".env*", "*.pem", "*.key", "secrets*", ".tiltmeter"]

# Change notices that API providers publish: when one affects this repository,
# `tiltmeter notices apply` lets the fix agent open the pull request.
# subscriptions:
#   - provider: api.partner.example                      # reads its /.well-known/api-changes.json
#   - feed: https://status.other.example/api-changes.json
#   - openapi: https://raw.githubusercontent.com/acme/api/main/openapi.json  # its spec, diffed

# Where to send alerts.
notify: []
#  - type: slack
#    webhook_url: ${SLACK_WEBHOOK_URL}
#    min_severity: warning
#  - type: webhook
#    url: https://example.com/hooks/tiltmeter
#    min_severity: breaking
"""

EXAMPLE_CONFIG = _EXAMPLE_ENDPOINTS + EXAMPLE_SETTINGS
