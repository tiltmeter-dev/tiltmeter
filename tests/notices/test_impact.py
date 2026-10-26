from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.fix.kit import CLIENT, MODELS, make_repo
from tests.notices.kit import notice
from tiltmeter.fix.git import Git
from tiltmeter.notices.impact import (
    Candidate,
    RepoIndex,
    assess,
    choose_endpoints,
    fields_near,
    name_variants,
    path_anchors,
)
from tiltmeter.notices.models import ApiChangeNotice, Impact, SdkName


def check(tmp_path: Path, files: dict[str, str], **overrides: object) -> Impact:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, _ = make_repo(tmp_path, files)
    git = Git.open(repo)
    model = ApiChangeNotice.model_validate(notice(**overrides))
    return assess(RepoIndex.read(git.root, git.tracked_files(), ignore=[".env*"]), model)


def test_the_kit_repository_is_affected(tmp_path: Path) -> None:
    impact = check(tmp_path, {"client.py": CLIENT, "models.py": MODELS})
    assert impact.affected
    assert impact.files == ["client.py", "models.py"]
    assert impact.anchors == ["api.billing.test", "/v1/customers", "/customers"]
    assert impact.fields == ["phone"]
    assert impact.reason == (
        "the repository mentions api.billing.test, /v1/customers and /customers, "
        "and code near them reads phone"
    )


def test_a_repository_that_never_calls_the_api_is_not(tmp_path: Path) -> None:
    impact = check(tmp_path, {"app.py": "print('phone and balance')\n"})
    assert not impact.affected
    assert impact.reason == (
        "no tracked file mentions api.billing.test, /v1/customers, /customers or billing-sdk"
    )


def test_calling_the_api_without_reading_the_fields_is_not(tmp_path: Path) -> None:
    client = 'BASE = "https://api.billing.test/v1"\nprint(get(BASE)["email"])\n'
    impact = check(tmp_path, {"billing/client.py": client})
    assert not impact.affected
    assert impact.anchors == ["api.billing.test"]
    assert impact.reason == (
        "the repository mentions api.billing.test, but no file near those mentions "
        "reads phone or balance"
    )


def test_fields_count_near_the_api_only(tmp_path: Path) -> None:
    files = {
        "billing/client.py": 'URL = "https://api.billing.test/v1/customers"\n',
        "billing/models/customer.py": "class Customer:\n    balance: int\n",
        "ui/form.py": 'LABEL = "phone"\n',
    }
    impact = check(tmp_path, files)
    assert impact.affected
    assert impact.files == ["billing/models/customer.py", "billing/client.py"]
    assert impact.fields == ["balance"]


def test_names_are_matched_whole_and_in_code_spellings(tmp_path: Path) -> None:
    client = (
        'URL = "https://api.billing.test/v1/customers"\n'
        "print(customer['phone_number'], customer.telephone)\n"
    )
    assert not check(tmp_path / "a", {"client.py": client}).affected
    data = notice()
    data["endpoints"][0]["changes"] = [{"path": "$.phone_number", "kind": "field_removed"}]
    camel = 'fetch("https://api.billing.test/v1/customers").then(c => c.phoneNumber)\n'
    impact = check(tmp_path / "b", {"client.js": camel}, endpoints=data["endpoints"])
    assert impact.fields == ["phone_number"]


def test_sdk_dependencies_are_anchors(tmp_path: Path) -> None:
    files = {
        "requirements.txt": "Billing_SDK==2.4.1\n",
        "app/sync.py": "def sync(customer):\n    return customer.phone\n",
    }
    impact = check(tmp_path, files)
    assert impact.affected
    assert impact.anchors == ["billing-sdk"]
    assert impact.files == ["app/sync.py", "requirements.txt"]


def test_docs_lock_files_ignored_paths_and_links_are_not_read(tmp_path: Path) -> None:
    mention = "https://api.billing.test/v1/customers phone\n"
    files = {
        "README.md": mention,
        "uv.lock": 'name = "billing-sdk"\n',
        ".env.example": f"BILLING_URL={mention}",
        "data.bin": "\0" + mention,
    }
    repo, _ = make_repo(tmp_path, files)
    git = Git.open(repo)
    if sys.platform != "win32":  # creating links needs extra rights on Windows
        (repo / "link.py").symlink_to(repo / "README.md")
        git.run("add", "link.py")
    model = ApiChangeNotice.model_validate(notice())
    impact = assess(RepoIndex.read(git.root, git.tracked_files(), ignore=[".env*"]), model)
    assert not impact.affected


def test_notices_without_code_changes_affect_nobody(tmp_path: Path) -> None:
    data = notice()
    data["endpoints"][0]["changes"] = [
        {"path": "$", "kind": "status_changed", "before": "200", "after": "410"}
    ]
    impact = check(tmp_path, {"client.py": CLIENT}, endpoints=data["endpoints"])
    assert not impact.affected
    assert impact.reason == "the notice has no changes code has to follow"


@pytest.mark.parametrize(
    ("url", "anchors"),
    [
        ("https://a.test/v1/customers/{id}/invoices", ["/v1/customers", "/customers"]),
        ("https://a.test/customers", ["/customers"]),
        ("https://a.test/v2", []),
        ("https://a.test/", []),
        ("https://a.test/{tenant}/items", []),
    ],
)
def test_path_anchors(url: str, anchors: list[str]) -> None:
    assert path_anchors(url) == anchors


def test_name_variants() -> None:
    assert name_variants("phone") == ["phone", "Phone"]
    assert name_variants("created_at") == ["created_at", "createdAt", "CreatedAt"]
    assert name_variants("content-type") == ["content-type", "contentType", "ContentType"]


def test_paths_alone_may_belong_to_another_api(tmp_path: Path) -> None:
    stripe = 'stripe.get("https://api.stripe.com/v1/customers")["phone"]\n'
    impact = check(tmp_path, {"payments.py": stripe})
    assert not impact.affected
    assert impact.reason == (
        "the repository mentions /v1/customers, but never api.billing.test or billing-sdk, "
        "so those may belong to another API"
    )


def index_of(tmp_path: Path, files: dict[str, str]) -> RepoIndex:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, _ = make_repo(tmp_path, files)
    git = Git.open(repo)
    return RepoIndex.read(git.root, git.tracked_files())


def test_paths_in_code_pick_the_endpoints(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "client.py": 'get(f"/v1/customers/{customer_id}")\nget("/invoices/" + number)\n',
            "README.md": "GET /v1/charges/{id}\n",  # docs never count
        },
    )
    assert index.uses_path("https://a.test/v1/customers/{id}") == 2
    assert index.uses_path("https://a.test/v1/invoices/{id}") == 1  # only its start
    assert index.uses_path("https://a.test/v1/charges/{id}") == 0
    candidates = [
        Candidate("https://a.test/v1/charges/{id}", ("amount",), 2),
        Candidate("https://a.test/v1/invoices/{id}", ("total",), 2),
        Candidate("https://a.test/v1/customers/{id}", ("phone",), 1),
    ]
    assert choose_endpoints(index, candidates) == [2, 1]
    assert choose_endpoints(index, candidates, limit=1) == [2]


def test_tiltmeter_config_is_no_use_of_an_api(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "tiltmeter.yaml": "subscriptions:\n  - openapi: https://a.test/v1/openapi.json\n",
            "app.py": "print('hello')\n",
        },
    )
    assert "tiltmeter.yaml" not in index.texts
    assert not index.mentions("a.test")


def test_a_path_that_names_an_endpoint_is_not_the_start_of_others(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "pulse.ts": "get(`/repos/${owner}/${repo}`)\nget(`/repos/${owner}/${repo}/pulls?x`)\n",
            "billing.py": 'BASE = "/v1/invoices"\nget(BASE + "/" + number)\n',
        },
    )
    urls = [
        "https://a.test/repos/{owner}/{repo}",
        "https://a.test/repos/{owner}/{repo}/pulls",
        "https://a.test/repos/{owner}/{repo}/forks",  # starts like the first, unused
        "https://a.test/v1/invoices/{id}",  # only "/v1/invoices", which names nothing
        "https://a.test/pulls",  # how the code's pulls path ends, but not where it starts
    ]
    assert index.uses([Candidate(url, ()) for url in urls]) == [2, 2, 0, 1, 0]
    assert index.uses_path(urls[2]) == 1  # alone, nothing else explains the path


def test_an_apps_own_routes_are_not_the_apis(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "app/plans/page.tsx": 'redirect("/plans")\nfetch("/account")\n',
            "lib/stripe.ts": 'import Stripe from "stripe"\nconst back = "/account"\n',
        },
    )
    sdk = [SdkName(ecosystem="npm", name="stripe")]
    assert index.api_files([], sdk) == {"lib/stripe.ts"}
    urls = ["https://api.stripe.com/v1/plans", "https://api.stripe.com/v1/account"]
    candidates = [Candidate(url, ()) for url in urls]
    # /plans is only the app's page; /account also appears next to the SDK, so it may be.
    assert index.uses(candidates, api_files=index.api_files([], sdk)) == [0, 2]
    assert index.uses(candidates) == [2, 2]  # without knowing the API, both look used


def test_sdk_imports_say_where_the_api_is_used(tmp_path: Path) -> None:
    files = {
        "package.json": '{"dependencies": {"stripe": "^18.0.0"}}\n',
        "lib/billing/stripe.ts": (
            'import Stripe from "stripe"\nexport const end = (s: any) => s.current_period_end\n'
        ),
        "app/pricing/page.tsx": "export const reason = (x: any) => x.reason\n",
    }
    index = index_of(tmp_path, files)
    sdk = [SdkName(ecosystem="npm", name="stripe")]
    near = fields_near(index, ["api.stripe.com"], sdk, ["current_period_end", "reason"])
    assert near == {"current_period_end"}  # app/pricing is not near the Stripe code


def test_calls_through_an_sdk_name_their_endpoints(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "billing.py": (
                "sub = stripe.Subscription.retrieve(sid)\n"
                "intent = stripe.PaymentIntent.create(amount=1)\n"
                "pulls = await octokit.rest.pulls.list({owner, repo})\n"
                "session = await stripe.checkout.sessions.create({mode})\n"
            ),
        },
    )
    endpoints = [
        ("GET", "https://api.stripe.com/v1/subscriptions/{subscription_exposed_id}", 2),
        ("POST", "https://api.stripe.com/v1/subscriptions/{subscription_exposed_id}", 0),
        ("GET", "https://api.stripe.com/v1/subscriptions", 0),  # list, not retrieve
        ("POST", "https://api.stripe.com/v1/payment_intents", 2),
        ("GET", "https://api.stripe.com/v1/invoices/{invoice}", 0),
        ("GET", "https://api.github.com/repos/{owner}/{repo}/pulls", 2),
        ("GET", "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}", 0),
        ("POST", "https://api.stripe.com/v1/checkout/sessions", 2),
        ("POST", "https://api.stripe.com/v1/financial_connections/sessions", 0),  # not checkout
    ]
    candidates = [Candidate(url, (), method=method) for method, url, _ in endpoints]
    assert index.uses(candidates) == [level for _, _, level in endpoints]


def test_field_names_decide_when_code_goes_through_an_sdk(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "requirements.txt": "billing-sdk==2.0\n",
            "app.py": "import billing_sdk\nprint(billing_sdk.Customer.get(1).phone)\n",
        },
    )
    candidates = [
        Candidate("https://a.test/v1/charges/{id}", ("amount", "id"), 2),
        Candidate("https://a.test/v1/customers/{id}", ("phone", "id"), 1),
        Candidate("https://a.test/v1/refunds/{id}", ("reason",), 2),
    ]
    sdk = [SdkName(ecosystem="pypi", name="billing-sdk")]
    near = fields_near(index, ["a.test"], sdk, ["amount", "id", "phone", "reason"])
    assert near == {"phone"}
    assert choose_endpoints(index, candidates, near=near) == [1]
    # Customer.get(1) is a call through the SDK, so it names the endpoint by itself.
    assert choose_endpoints(index, candidates) == [1]
    # Without any signal, the most severe changes come first.
    assert choose_endpoints(None, candidates) == [0, 2, 1]


def test_impact_lists_the_endpoints_the_repository_uses(tmp_path: Path) -> None:
    data = notice()
    data["endpoints"].insert(
        0,
        {
            "method": "GET",
            "url": "https://api.billing.test/v1/plans/{id}",
            "changes": [{"path": "$.phone", "kind": "field_removed"}],
        },
    )
    impact = check(
        tmp_path, {"client.py": CLIENT, "models.py": MODELS}, endpoints=data["endpoints"]
    )
    assert impact.affected
    assert impact.endpoints == [1]  # /v1/customers/{id} is in the code, /v1/plans is not
