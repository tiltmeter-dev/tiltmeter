"""The ``driftwatch.yaml`` file: which endpoints to watch and how.

Values may contain ``${VAR}`` or ``${VAR:-default}`` placeholders. They are kept
as written in memory and only expanded when a request or notification is sent,
so secrets never sit in the config objects that get logged or traced.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from driftwatch.core.models import ChangeKind, Severity
from driftwatch.core.paths import PathPattern, PathSyntaxError
from driftwatch.errors import ConfigError, UnknownEndpointError

DEFAULT_CONFIG_FILE = "driftwatch.yaml"
DEFAULT_MODEL = "claude-haiku-5-5"

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]


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


class Config(_Model):
    """The whole configuration file."""

    version: Literal[1] = 1
    state_dir: str = ".driftwatch"
    concurrency: int = Field(default=5, ge=1, le=64)
    keep_checks: int = Field(default=500, ge=10, le=100_000)
    severity_overrides: dict[ChangeKind, Severity] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    notify: list[NotifierConfig] = Field(default_factory=list)
    endpoints: list[EndpointConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Config:
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
        raise ConfigError(f"config file not found: {path} (run `driftwatch init` to create one)")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping with an `endpoints` list at the top level")
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


EXAMPLE_CONFIG = """\
# driftwatch configuration. Docs: see README.md
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

# Explanations written by Claude when drift is detected (needs ANTHROPIC_API_KEY).
llm:
  enabled: true
  model: claude-haiku-5-5

# Where to send alerts.
notify: []
#  - type: slack
#    webhook_url: ${SLACK_WEBHOOK_URL}
#    min_severity: warning
#  - type: webhook
#    url: https://example.com/hooks/driftwatch
#    min_severity: breaking
"""
