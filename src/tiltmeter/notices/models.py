"""The API change notice format, ``api-change-notice/v1``, and the feed that lists notices.

A notice is a provider's own description of a change to the JSON responses of one or
more endpoints, in the vocabulary Tiltmeter uses for drift: the same paths and the same
change kinds, so it turns into a fix request without reading free text. Everything in it
comes from a third party, so validation is strict: unknown keys, control characters and
over-long text are rejected, and every endpoint and the signing key must live on the
provider's own domain. ``docs/notice-spec.md`` is the human-readable specification.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from tiltmeter.core.models import ChangeKind, NoticeRecord
from tiltmeter.core.paths import PathSyntaxError, parse_path

NOTICE_SPEC: Final = "https://tiltmeter.dev/schemas/api-change-notice/v1.json"
FEED_SPEC: Final = "https://tiltmeter.dev/schemas/api-change-feed/v1.json"
FEED_PATH: Final = "/.well-known/api-changes.json"
KEY_PATH: Final = "/.well-known/api-change-key"

MAX_TEXT: Final = 500
MAX_VALUE: Final = 200
MAX_ENDPOINTS: Final = 50
MAX_CHANGES: Final = 200
MAX_SDKS: Final = 20
MAX_FEED_NOTICES: Final = 500

# No control characters and no bidirectional overrides, which could hide text in a
# terminal, a prompt or a pull request. Paths also leave out backticks, which they never
# need, and URLs stick to the characters URLs are made of.
_PLAIN: Final = r"^[^\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]*$"
_PATH_TEXT: Final = r"^[^\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069`]*$"
_URL_TEXT: Final = r"^https://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%{}]+$"
_UNPRINTABLE: Final = re.compile(r"[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]+")
HOST_PATTERN: Final = (
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type Ecosystem = Literal["pypi", "npm", "go", "maven", "nuget", "rubygems", "cargo", "packagist"]
type Host = Annotated[str, Field(max_length=253, pattern=HOST_PATTERN)]
type Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT, pattern=_PLAIN)]
type Value = Annotated[str, Field(max_length=MAX_VALUE, pattern=_PLAIN)]


def on_domain(host: str, provider: str) -> bool:
    """Whether ``host`` is the provider's domain or one of its subdomains."""
    host = host.lower().rstrip(".")
    return host == provider or host.endswith(f".{provider}")


def https_host(url: str) -> str | None:
    """The host of an https URL without credentials, or None for anything else."""
    try:
        parts = urlsplit(url)
        _ = parts.port  # a malformed port raises
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return parts.hostname


def _check_path(value: str) -> str:
    try:
        parse_path(value)
    except PathSyntaxError as exc:
        raise ValueError(str(exc)) from exc
    return value


def printable(text: str, limit: int = 300) -> str:
    """Text that came from a third party, made safe to show on one line."""
    text = " ".join(_UNPRINTABLE.sub(" ", text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


type JsonPath = Annotated[
    str, Field(max_length=MAX_VALUE, pattern=_PATH_TEXT), AfterValidator(_check_path)
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Address(_Strict):
    """How customers call an endpoint."""

    method: HttpMethod
    url: str = Field(
        max_length=2048,
        pattern=_URL_TEXT,
        description="https URL without query string; {name} marks a path parameter.",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if https_host(value) is None:
            raise ValueError("must be an https URL")
        parts = urlsplit(value)
        if parts.query or parts.fragment:
            raise ValueError("must not have a query string or fragment")
        return value


class MovedEndpoint(_Address):
    """Where a removed endpoint lives on."""


class NoticeChange(_Strict):
    """One change to the responses, or to what requests may send, as Tiltmeter reports it."""

    path: JsonPath = Field(
        description=(
            "Location in the response body: $ is the root and [*] every array item. With "
            "value_retired, the request member or path parameter instead, such as $.model."
        )
    )
    kind: ChangeKind
    before: Value | None = Field(
        default=None, description="The type or values before; with value_retired, the value."
    )
    after: Value | None = Field(default=None, description="The type or values after.")
    renamed_to: JsonPath | None = Field(
        default=None, description="Where a removed field lives on. Only with field_removed."
    )
    moved_to: MovedEndpoint | None = Field(
        default=None, description="Where a removed endpoint lives on. Only with endpoint_removed."
    )
    replaced_by: Value | None = Field(
        default=None, description="The value to send instead. Only with value_retired."
    )
    note: Text | None = Field(
        default=None,
        description="The provider's own words on this change, such as the new format.",
    )
    applies_to: Text | None = Field(
        default=None,
        description=(
            "Who this change affects, when that is not every caller and not what the "
            "notice's applies_to says."
        ),
    )

    @model_validator(mode="after")
    def _members_of_their_kind(self) -> NoticeChange:
        if self.renamed_to is not None and self.kind is not ChangeKind.FIELD_REMOVED:
            raise ValueError("renamed_to only goes with kind field_removed")
        if self.moved_to is not None and self.kind is not ChangeKind.ENDPOINT_REMOVED:
            raise ValueError("moved_to only goes with kind endpoint_removed")
        if self.replaced_by is not None and self.kind is not ChangeKind.VALUE_RETIRED:
            raise ValueError("replaced_by only goes with kind value_retired")
        if self.kind is ChangeKind.ENDPOINT_REMOVED and self.path != "$":
            raise ValueError("an endpoint_removed change has the path $")
        if self.kind is ChangeKind.VALUE_RETIRED and not self.before:
            raise ValueError("a value_retired change names the value in before")
        return self


class NoticeEndpoint(_Address):
    """An endpoint whose responses, or what its requests may send, change, and how."""

    changes: list[NoticeChange] = Field(min_length=1, max_length=MAX_CHANGES)


class SdkName(_Strict):
    """A package of the provider's SDK, in one ecosystem."""

    ecosystem: Ecosystem
    name: str = Field(max_length=214, pattern=r"^[A-Za-z0-9@][A-Za-z0-9@/:._\-]*$")


def parse_sdk_name(text: str) -> SdkName:
    """``ecosystem:name``, such as ``pypi:stripe`` or ``npm:@acme/sdk``."""
    ecosystem, _, name = text.partition(":")
    if not name:
        raise ValueError(f"expected ECOSYSTEM:NAME, such as pypi:stripe, got {text!r}")
    return SdkName.model_validate({"ecosystem": ecosystem, "name": name})


class SdkRelease(SdkName):
    """A release of the provider's SDK that already follows the change."""

    fixed_in: str = Field(max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_\-]*$")


class NoticeSignature(_Strict):
    """An Ed25519 signature over the notice, and where the provider publishes the key."""

    alg: Literal["ed25519"]
    key: str = Field(
        max_length=2048,
        pattern=_URL_TEXT,
        description="The provider's key file, always https://<provider>/.well-known/api-change-key.",
    )
    value: str = Field(
        pattern=r"^[A-Za-z0-9+/]{86}==$", description="Base64 of the 64-byte signature."
    )

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if https_host(value) is None:
            raise ValueError("must be an https URL")
        return value


class ApiChangeNotice(_Strict):
    """A provider's announcement of a change to the JSON responses of its API, or to what
    its requests may send."""

    spec: Literal["https://tiltmeter.dev/schemas/api-change-notice/v1.json"]
    id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$",
        description="Unique among the provider's notices; never reused.",
    )
    provider: Host = Field(
        description="The provider's domain. Endpoints and the signing key must live on it."
    )
    published_at: AwareDatetime
    effective_at: AwareDatetime | None = Field(
        default=None, description="When the old responses stop."
    )
    summary: Text
    applies_to: Text | None = Field(
        default=None,
        description=(
            "Who the change affects, when that is not every caller: a plan, a deployment or "
            "an API version. Callers outside it keep the old behaviour."
        ),
    )
    endpoints: list[NoticeEndpoint] = Field(min_length=1, max_length=MAX_ENDPOINTS)
    sdks: list[SdkRelease] = Field(default_factory=list, max_length=MAX_SDKS)
    signature: NoticeSignature | None = None

    @model_validator(mode="after")
    def _on_the_provider_domain(self) -> ApiChangeNotice:
        moved = [c.moved_to for e in self.endpoints for c in e.changes if c.moved_to is not None]
        for address in [*self.endpoints, *moved]:
            host = https_host(address.url) or ""
            if not on_domain(host, self.provider):
                raise ValueError(f"endpoint {address.url} is not on {self.provider}")
        if self.signature is not None and self.signature.key != default_key_url(self.provider):
            # Pinned, so that no other page on the domain, such as one that echoes its
            # input, can pass for the key file.
            raise ValueError(f"signature.key must be {default_key_url(self.provider)}")
        return self

    @property
    def hosts(self) -> list[str]:
        return list(dict.fromkeys(https_host(endpoint.url) or "" for endpoint in self.endpoints))


class NoticeFeed(_Strict):
    """The file a provider serves at ``/.well-known/api-changes.json``.

    Readers validate each notice on its own, so one bad entry never hides the others.
    """

    spec: Literal["https://tiltmeter.dev/schemas/api-change-feed/v1.json"]
    notices: list[ApiChangeNotice] = Field(max_length=MAX_FEED_NOTICES, description="Newest first.")


def default_key_url(provider: str) -> str:
    return f"https://{provider}{KEY_PATH}"


def default_feed_url(provider: str) -> str:
    return f"https://{provider}{FEED_PATH}"


def notice_json_schema() -> dict[str, Any]:
    """The JSON Schema of a notice, as published at :data:`NOTICE_SPEC`."""
    return _published(ApiChangeNotice.model_json_schema(), NOTICE_SPEC)


def feed_json_schema() -> dict[str, Any]:
    """The JSON Schema of a feed, as published at :data:`FEED_SPEC`."""
    return _published(NoticeFeed.model_json_schema(), FEED_SPEC)


def _published(schema: dict[str, Any], url: str) -> dict[str, Any]:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": url, **schema}


# -- what a subscriber learns -----------------------------------------------------


class Impact(BaseModel):
    """Whether a repository uses what a notice changes, found by plain text search."""

    affected: bool
    reason: str
    endpoints: list[int] = Field(
        default_factory=list,
        description="Positions of the notice's endpoints the repository uses, most likely first.",
    )
    files: list[str] = Field(
        default_factory=list, description="Files to start from, most relevant first."
    )
    anchors: list[str] = Field(
        default_factory=list, description="Hosts, paths and SDKs of the API that were found."
    )
    fields: list[str] = Field(
        default_factory=list, description="Changed field names found near those uses."
    )


class SpecInfo(BaseModel):
    """Where a change found by comparing two versions of a provider's spec comes from."""

    url: str
    baseline_url: str | None = Field(
        default=None, description="Where the old version came from, when not from url."
    )
    old_version: str | None = None
    new_version: str | None = None
    old_fetched_at: str
    new_fetched_at: str
    old_digest: str
    new_digest: str
    versioned: bool = Field(
        default=False, description="Clients pin a version, so a new version is opt-in."
    )
    endpoints_changed: int = Field(description="Endpoints whose responses changed.")
    omitted_endpoints: int = Field(
        default=0, description="Changed endpoints left out of this entry, as less relevant."
    )
    omitted_changes: int = Field(default=0, description="Changes left out past the limits.")

    @property
    def new_api_version(self) -> bool:
        """Whether a versioned API published a new version, which clients adopt on purpose."""
        return self.versioned and self.old_version != self.new_version


type EntryState = Literal["new", "done", "skipped", "rejected"]


class FeedEntry(BaseModel):
    """One notice from a subscribed feed, and where it stands for this repository."""

    feed: str
    state: EntryState = Field(
        description=(
            "new: verified and not handled yet; done: handled before; skipped: older than "
            "the subscription's since date or listed twice; rejected: invalid or unsigned."
        )
    )
    provider: str | None = None
    id: str | None = None
    source: Literal["notice", "spec"] = Field(
        default="notice",
        description="notice: published by the provider; spec: found by comparing its spec.",
    )
    problem: str | None = Field(default=None, description="Why it was skipped or rejected.")
    notice: ApiChangeNotice | None = None
    spec: SpecInfo | None = None
    record: NoticeRecord | None = None
    impact: Impact | None = None


class FeedReport(BaseModel):
    """What one subscription holds: a provider's feed, or the changes to its spec."""

    subscription: str
    feed: str
    kind: Literal["feed", "openapi"] = "feed"
    error: str | None = Field(default=None, description="Why the feed could not be read.")
    note: str | None = Field(
        default=None, description="What happened to the spec baseline, for openapi ones."
    )
    entries: list[FeedEntry] = Field(default_factory=list)
