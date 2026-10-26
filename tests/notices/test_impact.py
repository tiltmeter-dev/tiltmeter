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


def test_installed_code_and_agent_copies_are_not_the_repositorys(tmp_path: Path) -> None:
    index = index_of(
        tmp_path,
        {
            "src/billing.ts": "stripe.subscriptions.retrieve(id)\n",
            "node_modules/stripe/index.js": "a.subscriptions.retrieve(x)\n",
            ".claude/worktrees/agent-1/src/billing.ts": "stripe.subscriptions.retrieve(id)\n",
            "docs/.claude/notes.md": "kept: not a worktree copy\n",
        },
    )
    assert set(index.texts) == {"src/billing.ts", "docs/.claude/notes.md"}


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
                "const sub = await getStripe().invoices.retrieve(id)\n"
            ),
        },
    )
    endpoints = [
        ("GET", "https://api.stripe.com/v1/subscriptions/{subscription_exposed_id}", 2),
        ("POST", "https://api.stripe.com/v1/subscriptions/{subscription_exposed_id}", 0),
        ("GET", "https://api.stripe.com/v1/subscriptions", 0),  # list, not retrieve
        ("POST", "https://api.stripe.com/v1/payment_intents", 2),
        ("GET", "https://api.stripe.com/v1/invoices/{invoice}", 2),  # through getStripe()
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


def retired_model(**change: object) -> dict[str, object]:
    """A notice that retires a model id on the billing API's completions endpoint."""
    return {
        "endpoints": [
            {
                "method": "POST",
                "url": "https://api.billing.test/v1/completions",
                "changes": [
                    {
                        "path": "$.model",
                        "kind": "value_retired",
                        "before": "billing-3-5-20240620",
                        "replaced_by": "billing-4",
                        **change,
                    }
                ],
            }
        ],
        "sdks": [],
    }


def test_a_retired_value_in_code_names_the_api_on_its_own(tmp_path: Path) -> None:
    code = 'llm = Chat(model="billing-3-5-20240620", temperature=0)\n'
    impact = check(tmp_path / "used", {"agent/llm.py": code}, **retired_model())
    assert impact.affected
    assert impact.files == ["agent/llm.py"]
    assert impact.anchors == ["billing-3-5-20240620"]
    assert impact.fields == []  # model is what requests send, not a field code reads


@pytest.mark.parametrize(
    "spelling",
    [
        'model = "bedrock.billing-3-5-20240620-v1:0"',  # another platform's id
        'model = "billing-3-5-20240620-v2"',  # a longer id
        'model = "billing-3-5-20240620@2024"',
    ],
)
def test_a_retired_value_counts_only_whole(tmp_path: Path, spelling: str) -> None:
    impact = check(tmp_path, {"llm.py": spelling + "\n"}, **retired_model())
    assert not impact.affected


def test_calling_the_api_with_a_current_value_is_not_affected(tmp_path: Path) -> None:
    code = 'URL = "https://api.billing.test/v1/completions"\npost(URL, model="billing-4")\n'
    impact = check(tmp_path, {"llm.py": code}, **retired_model())
    assert not impact.affected
    assert impact.reason == (
        "the repository mentions api.billing.test, /v1/completions and /completions, but "
        "never billing-3-5-20240620"
    )


def test_a_retired_value_in_the_docs_is_no_use(tmp_path: Path) -> None:
    impact = check(tmp_path, {"README.md": "Uses billing-3-5-20240620.\n"}, **retired_model())
    assert not impact.affected


def removed_search(moved: bool = True) -> dict[str, object]:
    change: dict[str, object] = {"path": "$", "kind": "endpoint_removed"}
    if moved:
        change["moved_to"] = {
            "method": "GET",
            "url": "https://your-domain.billing.test/rest/api/3/search/jql",
        }
    return {
        "provider": "billing.test",
        "endpoints": [
            {
                "method": "GET",
                "url": "https://your-domain.billing.test/rest/api/3/search",
                "changes": [change, {"path": "$.total", "kind": "field_removed"}],
            }
        ],
        "sdks": [],
    }


def test_a_removed_endpoint_counts_where_code_calls_its_whole_path(tmp_path: Path) -> None:
    old = 'r = get(f"https://{site}.billing.test/rest/api/3/search?jql={q}")\nprint(r["total"])\n'
    impact = check(tmp_path / "old", {"jira.py": old}, **removed_search())
    assert impact.affected
    # The provider's domain is the API's name when every customer has a host of its own.
    assert impact.anchors == ["billing.test", "/rest/api/3/search"]
    new = old.replace("/search?", "/search/jql?")
    migrated = check(tmp_path / "new", {"jira.py": new}, **removed_search())
    assert not migrated.affected
    assert migrated.reason == (
        "the repository never calls GET /rest/api/3/search, which the notice removes"
    )


def test_a_quoted_path_without_its_leading_slash_is_the_path(tmp_path: Path) -> None:
    joined = 'REST_SEARCH = "rest/api/3/search"\nclient.get(base + REST_SEARCH)["total"]\n'
    impact = check(tmp_path / "joined", {"jira.py": joined}, **removed_search())
    assert impact.affected
    assert impact.anchors == ["/rest/api/3/search"]
    # A key alone is no path: "customers" is not /customers.
    index = index_of(tmp_path / "key", {"app.py": 'data["customers"]\n'})
    assert index.uses([Candidate("https://api.billing.test/v1/customers", ())]) == [0]


def test_a_removed_endpoint_behind_a_configured_base_url_is_still_called(
    tmp_path: Path,
) -> None:
    gone = {
        "endpoints": [
            {
                "method": "GET",
                "url": "https://api.billing.test/v1/artists/{id}/top-tracks",
                "changes": [{"path": "$", "kind": "endpoint_removed"}],
            },
            {
                "method": "GET",
                "url": "https://api.billing.test/v1/markets",
                "changes": [{"path": "$", "kind": "endpoint_removed"}],
            },
        ]
    }
    gone["endpoints"].insert(
        0,
        {
            "method": "GET",
            "url": "https://api.billing.test/v1/artists/{id}",
            "changes": [{"path": "$.followers", "kind": "field_removed"}],
        },
    )
    files = {
        "api/artists.ts": "get(`${config.baseURL}/artists/${id}/top-tracks`)\n"
        "get(`https://api.billing.test/v1/artists/${id}`).followers\n",
        "api/markets.ts": "get(`${config.baseURL}/markets`)\n",
        "config.ts": 'export const baseURL = "https://api.billing.test/v1"\n',
    }
    impact = check(tmp_path, files, **gone)
    assert impact.affected
    # /markets may be the app's own route, so only the longer path counts away from the host,
    # and a call of an endpoint that is gone comes before the others.
    assert impact.endpoints == [1, 0]
