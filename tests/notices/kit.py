"""Notices, keys, feeds and a fake code search for the provider-mode tests, matching
``tests/fix/kit.py``.

The provider is the billing API the kit repository already calls: its client reads
``customer["phone"]`` from ``https://api.billing.test/v1/customers/{id}``.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tiltmeter.errors import HuntError
from tiltmeter.notices.hunt import PER_PAGE, QueryRefused, RepoFacts, SearchPage
from tiltmeter.notices.models import FEED_SPEC, NOTICE_SPEC
from tiltmeter.notices.signing import key_line, sign

PROVIDER = "api.billing.test"
FEED_URL = f"https://{PROVIDER}/.well-known/api-changes.json"
KEY_URL = f"https://{PROVIDER}/.well-known/api-change-key"
NOTICE_URL = f"https://{PROVIDER}/notices/phone-rename.json"
NOTICE_ID = "2026-11-03-phone-rename"

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
OTHER_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
KEY_FILE = f"# Tiltmeter notice keys\n{key_line(KEY.public_key())}\n"

_NOTICE: dict[str, Any] = {
    "spec": NOTICE_SPEC,
    "id": NOTICE_ID,
    "provider": PROVIDER,
    "published_at": "2026-11-03T12:00:00Z",
    "effective_at": "2026-12-01T00:00:00Z",
    "summary": "Customers now return phone_number instead of phone, and balance as a string.",
    "endpoints": [
        {
            "method": "GET",
            "url": f"https://{PROVIDER}/v1/customers/{{id}}",
            "changes": [
                {"path": "$.phone", "kind": "field_removed", "renamed_to": "$.phone_number"},
                {
                    "path": "$.balance",
                    "kind": "type_changed",
                    "before": "integer",
                    "after": "string",
                    "note": 'Decimal string in the account currency, such as "12.00".',
                },
            ],
        }
    ],
    "sdks": [{"ecosystem": "pypi", "name": "billing-sdk", "fixed_in": "3.0.0"}],
}


def notice(**overrides: Any) -> dict[str, Any]:
    """An unsigned notice; ``overrides`` replace top-level members."""
    data = copy.deepcopy(_NOTICE)
    data.update(overrides)
    return data


def signed(raw: dict[str, Any] | None = None, key: Ed25519PrivateKey = KEY) -> dict[str, Any]:
    return sign(raw if raw is not None else notice(), key, key_url=KEY_URL)


def feed(*notices: Any) -> dict[str, Any]:
    return {"spec": FEED_SPEC, "notices": list(notices)}


def serve(*notices: Any, key_file: str = KEY_FILE) -> tuple[respx.Route, respx.Route]:
    """Mock the provider's feed and key file (inside an active ``respx.mock``)."""
    feed_route = respx.get(FEED_URL).respond(200, json=feed(*notices))
    key_route = respx.get(KEY_URL).respond(200, text=key_file)
    return feed_route, key_route


def repo_facts(full_name: str, clone_url: Path | str, **overrides: Any) -> RepoFacts:
    """A public repository pushed three days ago, to clone from ``clone_url``."""
    data: dict[str, Any] = {
        "full_name": full_name,
        "url": f"https://github.com/{full_name}",
        "clone_url": str(clone_url),
        "stars": 10,
        "pushed_at": datetime.now(UTC) - timedelta(days=3),
        "default_branch": "main",
    }
    data.update(overrides)
    return RepoFacts(**data)


class FakeCodeSearch:
    """Answers code searches from a script instead of asking GitHub.

    Every query finds every repository in ``facts``, unless ``hits`` lists what one query
    finds. ``error`` is raised by every search after the first ``fails_after``, and
    ``refused`` by the queries it names.
    """

    def __init__(
        self,
        facts: Sequence[RepoFacts] = (),
        *,
        hits: dict[str, list[str]] | None = None,
        error: HuntError | None = None,
        fails_after: int = 0,
        refused: Sequence[str] = (),
    ) -> None:
        self.facts = {repo.full_name: repo for repo in facts}
        self.hits = hits
        self.error = error
        self.fails_after = fails_after
        self.refused = refused
        self.searched: list[tuple[str, int]] = []
        self.described: list[list[str]] = []

    def search(self, query: str, *, page: int) -> SearchPage:
        self.searched.append((query, page))
        if self.error is not None and len(self.searched) > self.fails_after:
            raise self.error
        if query in self.refused:
            raise QueryRefused("gh: Validation Failed (HTTP 422)")
        names = list(self.facts) if self.hits is None else self.hits.get(query, [])
        shown = names[(page - 1) * PER_PAGE : page * PER_PAGE]
        return SearchPage(total=len(names), repositories=shown, results=len(shown))

    def describe(self, names: Sequence[str]) -> list[RepoFacts]:
        self.described.append(list(names))
        return [self.facts[name] for name in names if name in self.facts]


SPEC_URL = "https://raw.example.test/billing/openapi.json"


def billing_spec(version: str = "1.0", *, renamed: bool = False, **extra: Any) -> dict[str, Any]:
    """The billing API's spec; ``renamed`` makes phone phone_number and balance a string."""
    phone = "phone_number" if renamed else "phone"
    customer: dict[str, Any] = {
        "type": "object",
        "required": ["id", phone, "balance"],
        "properties": {
            "id": {"type": "integer"},
            phone: {"type": "string"},
            "balance": {"type": "string" if renamed else "integer"},
            **extra,
        },
    }
    ok = {
        "200": {
            "description": "A customer.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Customer"}}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Billing", "version": version},
        "servers": [{"url": f"https://{PROVIDER}"}],
        "paths": {
            "/v1/customers/{id}": {"get": {"responses": ok}},
            "/v1/invoices/{id}": {"get": {"responses": ok}},
        },
        "components": {"schemas": {"Customer": customer}},
    }
