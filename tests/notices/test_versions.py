from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.notices.kit import notice
from tests.notices.test_impact import check, removed_search
from tiltmeter.notices.impact import MANIFESTS, RepoIndex
from tiltmeter.notices.models import SdkRelease
from tiltmeter.notices.versions import (
    all_follow,
    allowed,
    api_versions,
    follows,
    parse_version,
    sdk_pins,
)


def test_versions_are_read_by_their_leading_numbers() -> None:
    assert parse_version("12.0.0") == (12, 0, 0)
    assert parse_version("v81.2") == (81, 2)
    assert parse_version("2.0.0-beta.3") == (2, 0, 0)
    assert parse_version("latest") is None


@pytest.mark.parametrize(
    ("spec", "ecosystem", "bounds"),
    [
        ("11.2.0", "pypi", ((11, 2, 0), (11, 2, 1))),
        ("==11.2.0", "pypi", ((11, 2, 0), (11, 2, 1))),
        (">=11,<12", "pypi", ((11,), (12,))),
        ("~=11.2", "pypi", ((11, 2), (12,))),
        ("~=11.2.3", "pypi", ((11, 2, 3), (11, 3))),
        (">=11", "pypi", ((11,), None)),
        ("^17.2.1", "npm", ((17, 2, 1), (18,))),
        ("^0.26.1", "npm", ((0, 26, 1), (0, 27))),
        ("~17.2.1", "npm", ((17, 2, 1), (17, 3))),
        ("17.x", "npm", ((17,), (18,))),
        ("*", "npm", (None, None)),
        ("~> 12.0", "rubygems", ((12, 0), (13,))),
        (">= 11, < 13", "rubygems", ((11,), (13,))),
        ("0.26", "cargo", ((0, 26), (0, 27))),  # a bare version in Cargo.toml is ^0.26
        ("v81.2.0", "go", ((81, 2, 0), (81, 2, 1))),
        ("<=11.2.0", "pypi", (None, (11, 2, 1))),
    ],
)
def test_requirements_are_read_as_bounds(spec: str, ecosystem: str, bounds: object) -> None:
    assert allowed(spec, ecosystem) == bounds  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "spec", ["^17 || ^18", "git+https://github.com/x/y", "workspace:*", "file:../sdk", ""]
)
def test_what_is_no_range_says_nothing(spec: str) -> None:
    assert allowed(spec, "npm") is None
    assert follows(allowed(spec, "npm"), "18.0.0") is None


def test_a_pin_is_older_at_or_past_the_fix_or_unknown() -> None:
    assert follows(allowed("11.2.0", "pypi"), "12.0.0") is False
    assert follows(allowed("12.0.0", "pypi"), "12.0.0") is True
    assert follows(allowed("^18.1.0", "npm"), "18.0.0") is True
    assert follows(allowed("^17.0.0", "npm"), "18.0.0") is False
    assert follows(allowed(">=11", "pypi"), "12.0.0") is None  # 11 or 13: it does not say
    assert follows(allowed(">=11,<13", "pypi"), "12.0.0") is None
    assert follows(allowed("11.2.0", "pypi"), "soon") is None


def sdk(ecosystem: str, name: str, fixed_in: str = "12.0.0") -> SdkRelease:
    return SdkRelease.model_validate({"ecosystem": ecosystem, "name": name, "fixed_in": fixed_in})


def pinned(
    texts: dict[str, str], release: SdkRelease, locks: dict[str, str] | None = None
) -> list[tuple[str, str, bool, bool | None]]:
    pins = sdk_pins(texts, locks or {}, [release], MANIFESTS)
    return [(pin.file, pin.version, pin.locked, pin.follows) for pin in pins]


def test_python_manifests_and_locks() -> None:
    stripe = sdk("pypi", "stripe")
    assert pinned({"requirements.txt": "requests\nStripe==11.2.0  # billing\n"}, stripe) == [
        ("requirements.txt", "==11.2.0", False, False)
    ]
    assert pinned({"requirements/prod.txt": "stripe[async]>=12.1\n"}, stripe) == []
    assert pinned({"prod-requirements.txt": "stripe[async]>=12.1\n"}, stripe) == [
        ("prod-requirements.txt", ">=12.1", False, True)
    ]
    pyproject = '[project]\ndependencies = [\n  "httpx>=0.27",\n  "stripe>=11,<12",\n]\n'
    assert pinned({"pyproject.toml": pyproject}, stripe) == [
        ("pyproject.toml", ">=11,<12", False, False)
    ]
    poetry = (
        '[tool.poetry.dependencies]\nstripe = "^11.0"\nother = {version = "^1", extras = ["x"]}\n'
    )
    assert pinned({"pyproject.toml": poetry}, stripe) == [("pyproject.toml", "^11.0", False, False)]
    # A lock file says what is installed: it wins over the range next to it.
    lock = '[[package]]\nname = "stripe"\nversion = "12.3.0"\nsource = { registry = "x" }\n'
    assert pinned({"pyproject.toml": pyproject}, stripe, {"uv.lock": lock}) == [
        ("uv.lock", "12.3.0", True, True)
    ]
    dashed = sdk("pypi", "slack-sdk", "3.19.0")
    assert pinned({"requirements.txt": "slack_sdk==3.18.0\n"}, dashed) == [
        ("requirements.txt", "==3.18.0", False, False)
    ]
    assert pinned({"requirements.txt": "stripe-extras==1.0\nmystripe==2.0\n"}, stripe) == []


def test_node_manifests_and_locks() -> None:
    stripe = sdk("npm", "stripe", "18.0.0")
    manifest = json.dumps({"dependencies": {"stripe": "^17.4.0", "@slack/web-api": "^7.0.0"}})
    assert pinned({"web/package.json": manifest}, stripe) == [
        ("web/package.json", "^17.4.0", False, False)
    ]
    scoped = sdk("npm", "@slack/web-api", "6.7.0")
    assert pinned({"package.json": manifest}, scoped) == [("package.json", "^7.0.0", False, True)]
    lock = json.dumps({"packages": {"node_modules/stripe": {"version": "18.1.0"}}})
    assert pinned({"package.json": manifest}, stripe, {"package-lock.json": lock}) == [
        ("package-lock.json", "18.1.0", True, True)
    ]
    yarn = (
        'stripe@^17.4.0:\n  version "17.7.0"\n  resolved "https://x"\n\n'
        '"@slack/web-api@^7":\n  version "7.1.0"\n'
    )
    assert pinned({}, stripe, {"yarn.lock": yarn}) == [("yarn.lock", "17.7.0", True, False)]
    assert pinned({}, scoped, {"yarn.lock": yarn}) == [("yarn.lock", "7.1.0", True, True)]
    pnpm = (
        "importers:\n  .:\n    dependencies:\n      stripe:\n        specifier: ^17.4.0\n"
        "        version: 17.6.0\npackages:\n  stripe@17.6.0:\n    resolution: {}\n"
    )
    assert pinned({}, stripe, {"pnpm-lock.yaml": pnpm}) == [
        ("pnpm-lock.yaml", "17.6.0", True, False)
    ]
    assert pinned({"package.json": "{not json"}, stripe) == []


def test_other_ecosystems() -> None:
    go = sdk("go", "github.com/stripe/stripe-go", "82.0.0")
    assert pinned({"go.mod": "require (\n\tgithub.com/stripe/stripe-go/v81 v81.2.0\n)\n"}, go) == [
        ("go.mod", "v81.2.0", False, False)
    ]
    ruby = sdk("rubygems", "stripe", "13.0.0")
    assert pinned({"Gemfile": "gem 'rails'\ngem 'stripe', '~> 12.0'\n"}, ruby) == [
        ("Gemfile", "~> 12.0", False, False)
    ]
    assert pinned({"Gemfile": "gem 'stripe', '>= 11', '< 14'\n"}, ruby) == [
        ("Gemfile", ">= 11, < 14", False, None)
    ]
    gem_lock = "GEM\n  specs:\n    stripe (13.2.0)\n    stripe-rails (2.0)\n"
    assert pinned({"Gemfile": "gem 'stripe'\n"}, ruby, {"Gemfile.lock": gem_lock}) == [
        ("Gemfile.lock", "13.2.0", True, True)
    ]
    java = sdk("maven", "com.stripe:stripe-java", "29.0.0")
    pom = (
        "<dependency>\n<groupId>com.stripe</groupId>\n"
        "<artifactId>stripe-java</artifactId>\n<version>28.1.0</version>\n</dependency>"
    )
    assert pinned({"pom.xml": pom}, java) == [("pom.xml", "28.1.0", False, False)]
    gradle = "dependencies {\n  implementation 'com.stripe:stripe-java:29.1.0'\n}\n"
    assert pinned({"build.gradle": gradle}, java) == [("build.gradle", "29.1.0", False, True)]
    dotnet = sdk("nuget", "Stripe.net", "48.0.0")
    assert pinned(
        {"App.csproj": '<PackageReference Include="stripe.net" Version="47.4.0" />'}, dotnet
    ) == [("App.csproj", "47.4.0", False, False)]
    php = sdk("packagist", "stripe/stripe-php", "17.0.0")
    assert pinned(
        {"composer.json": json.dumps({"require": {"stripe/stripe-php": "^16.2"}})}, php
    ) == [("composer.json", "^16.2", False, False)]
    composer = json.dumps({"packages": [{"name": "stripe/stripe-php", "version": "v17.1.0"}]})
    assert pinned({}, php, {"composer.lock": composer}) == [
        ("composer.lock", "v17.1.0", True, True)
    ]
    rust = sdk("cargo", "async-stripe", "0.40.0")
    assert pinned(
        {"Cargo.toml": '[dependencies]\nasync-stripe = { version = "0.39", features = ["x"] }\n'},
        rust,
    ) == [("Cargo.toml", "0.39", False, False)]


def test_every_pin_must_follow_for_the_sdk_to_count_as_current() -> None:
    old = sdk_pins({"requirements.txt": "stripe==11.2.0\n"}, {}, [sdk("pypi", "stripe")], MANIFESTS)
    new = sdk_pins({"requirements.txt": "stripe==12.2.0\n"}, {}, [sdk("pypi", "stripe")], MANIFESTS)
    assert not all_follow(old, ["stripe"])
    assert all_follow(new, ["stripe"])
    assert not all_follow([*old, *new], ["stripe"])  # two services, one of them behind
    assert not all_follow(new, ["stripe", "other"])  # an SDK nothing pins is not known
    assert not all_follow([], [])


def test_dated_api_versions_are_found_where_the_api_is_named() -> None:
    texts = {
        "lib/stripe.ts": (
            'export const stripe = new Stripe(key, {\n  apiVersion: "2024-06-20",\n});\n'
        ),
        "gh.py": 'HEADERS = {"X-GitHub-Api-Version": "2022-11-28", "Accept": "x"}\n',
        "claude.sh": "curl -H 'anthropic-version: 2023-06-01' https://api.anthropic.com/v1/messages\n",
        "shop.rb": (
            'ShopifyAPI::Context.setup(api_version: "2024-10")\n'
            'Stripe.api_version = "2025-03-31.basil"\n'
        ),
        "k8s.yaml": "apiVersion: v1\nkind: Pod\n",
        "other.py": 'API_VERSION = "2020-01-01"\n',
    }
    found = api_versions(texts, ["lib/stripe.ts", "gh.py", "claude.sh", "shop.rb", "k8s.yaml"])
    assert [(pin.version, pin.file, pin.line) for pin in found] == [
        ("2023-06-01", "claude.sh", 1),
        ("2022-11-28", "gh.py", 1),
        ("2024-06-20", "lib/stripe.ts", 2),
        ("2024-10", "shop.rb", 1),
        ("2025-03-31.basil", "shop.rb", 2),
    ]


# -- what the search does with them ----------------------------------------------------


def removed_with_sdk() -> dict[str, object]:
    """Jira's removed search, with an SDK whose release 3.0.0 no longer calls it."""
    release = {"ecosystem": "pypi", "name": "billing-sdk", "fixed_in": "3.0.0"}
    return {**removed_search(), "sdks": [release]}


def test_an_sdk_that_already_follows_a_removed_endpoint_is_not_affected(tmp_path: Path) -> None:
    code = 'import billing_sdk\n\nfound = billing_sdk.Client().issues()\nprint(found["total"])\n'
    behind = {"requirements.txt": "billing-sdk==2.9.0\n", "app.py": code}
    impact = check(tmp_path / "behind", behind, **removed_with_sdk())
    assert impact.affected  # the SDK may still call the endpoint: upgrade it
    assert [(p.name, p.version, p.follows) for p in impact.sdk_pins] == [
        ("billing-sdk", "==2.9.0", False)
    ]

    current = {"requirements.txt": "billing-sdk==3.1.0\n", "app.py": code}
    impact = check(tmp_path / "current", current, **removed_with_sdk())
    assert not impact.affected
    assert impact.reason == (
        "the repository never calls GET /rest/api/3/search, which the notice removes, and it "
        "depends on billing-sdk ==3.1.0, which already follows the change"
    )
    assert impact.sdk_pins[0].follows is True

    # On the new SDK, a call the code makes itself still counts.
    direct = 'get(f"https://{site}.billing.test/rest/api/3/search?jql={q}")\n'
    impact = check(tmp_path / "direct", {**current, "jira.py": direct}, **removed_with_sdk())
    assert impact.affected
    # A range that may or may not include the fix settles nothing: affected, as before.
    loose = {"requirements.txt": "billing-sdk>=2\n", "app.py": code}
    assert check(tmp_path / "loose", loose, **removed_with_sdk()).affected


def test_pins_and_api_versions_come_with_the_impact(tmp_path: Path) -> None:
    files = {
        "requirements.txt": "billing-sdk>=2,<3\n",
        "uv.lock": '[[package]]\nname = "billing-sdk"\nversion = "2.4.1"\n',
        "billing/client.py": (
            'HEADERS = {"Billing-Version": "2024-06-20"}\n'
            'r = get("https://api.billing.test/v1/customers/1", headers=HEADERS)\n'
            'print(r["phone"])\n'
        ),
    }
    impact = check(tmp_path, files)
    assert impact.affected
    [pin] = impact.sdk_pins
    assert (pin.file, pin.version, pin.locked, pin.fixed_in, pin.follows) == (
        "uv.lock",
        "2.4.1",
        True,
        "3.0.0",
        False,
    )
    assert [(v.version, v.file, v.line) for v in impact.api_versions] == [
        ("2024-06-20", "billing/client.py", 1)
    ]
    model_notice = notice()
    assert model_notice["sdks"][0]["fixed_in"] == "3.0.0"


def test_lock_files_are_read_only_for_versions(tmp_path: Path) -> None:
    for name, text in {
        "uv.lock": "x" * 10,
        "sub/poetry.lock": "y",
        "node_modules/a/yarn.lock": "z",
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    names = ["uv.lock", "sub/poetry.lock", "node_modules/a/yarn.lock", "missing/uv.lock"]
    index = RepoIndex.read(tmp_path, names)
    assert index.texts == {}  # still not searched for anchors or fields
    assert dict(index.locks) == {"uv.lock": "x" * 10, "sub/poetry.lock": "y"}


def test_pipfile_and_yarn_berry_locks() -> None:
    stripe = sdk("pypi", "stripe")
    pipfile = json.dumps(
        {
            "default": {"stripe": {"version": "==12.1.0", "hashes": ["sha256:x"]}},
            "develop": {"Stripe-Mock": {"version": "==0.1"}, "requests": {"version": "==2.0"}},
        }
    )
    assert pinned({}, stripe, {"Pipfile.lock": pipfile}) == [("Pipfile.lock", "12.1.0", True, True)]
    assert pinned({}, stripe, {"Pipfile.lock": '{"default": []}'}) == []
    berry = (
        '"stripe@npm:^17.4.0":\n  version: 17.7.0\n  resolution: "stripe@npm:17.7.0"\n\n'
        '"stripe-extra@npm:^1":\n  version: 1.0.0\n'
    )
    node = sdk("npm", "stripe", "18.0.0")
    assert pinned({}, node, {"yarn.lock": berry}) == [("yarn.lock", "17.7.0", True, False)]
    # A line that only looks like an entry opens nothing.
    assert pinned({}, node, {"yarn.lock": "stripe@^17 is what we use\n  version 99.0.0\n"}) == []


def test_files_written_to_be_slow_or_deep_are_read_in_time() -> None:
    """A hunt reads other people's repositories: no manifest or lock file may stall it."""
    size = 1_000_000

    def fill(unit: str, length: int = size) -> str:
        return (unit * (length // len(unit) + 1))[:length]

    manifests = {
        ("pypi", "stripe", "requirements.txt"): [fill("stripe["), "stripe" + " " * size + "x"],
        ("pypi", "stripe", "pyproject.toml"): [fill("stripe = {" + "a" * 500 + "\n")],
        ("rubygems", "stripe", "Gemfile"): ["gem" + " " * size + "x", fill("gem 'stripe' ,")],
        ("maven", "com.stripe:stripe-java", "pom.xml"): [
            "<artifactId>stripe-java</artifactId>" + " " * size
        ],
        ("npm", "stripe", "package.json"): ["[" * size, '{"a":' * 100_000 + "1" + "}" * 100_000],
    }
    locks = {
        ("pypi", "stripe", "Pipfile.lock"): [fill('"stripe": {', 4 * size), "[" * size],
        ("pypi", "stripe", "uv.lock"): [fill('name = "stripe"\nversion = "', 4 * size)],
        ("npm", "stripe", "yarn.lock"): ["stripe@x:\n" + fill(" " * 200 + "a\n", 4 * size)],
        ("npm", "stripe", "pnpm-lock.yaml"): [fill(" " * 3000 + "\n", 4 * size)],
        ("npm", "stripe", "package-lock.json"): ["[" * size],
    }
    started = time.perf_counter()
    for (ecosystem, name, file), texts in manifests.items():
        for text in texts:
            assert len(sdk_pins({file: text}, {}, [sdk(ecosystem, name)], MANIFESTS)) <= 20
    for (ecosystem, name, file), texts in locks.items():
        for text in texts:
            assert len(sdk_pins({}, {file: text}, [sdk(ecosystem, name)], MANIFESTS)) <= 20
    slow_lines = fill("x-a-b-c-d-version" * 100 + "\n") + fill("api-version" + " " * 1900 + "\n")
    assert api_versions({"a.py": slow_lines}, ["a.py"]) == []
    assert time.perf_counter() - started < 20


def test_a_pin_is_only_ever_a_version() -> None:
    stripe = sdk("pypi", "stripe")
    # Words that pass for a requirement but hold no number are not one.
    lock = '[[package]]\nname = "stripe"\nversion = "name = "\n'
    assert pinned({}, stripe, {"uv.lock": lock}) == []
    assert pinned({"requirements.txt": "stripe==*\n"}, stripe) == []
    many = "".join(f"stripe=={n}.0.0\n" for n in range(40))
    assert len(sdk_pins({"requirements.txt": many}, {}, [stripe], MANIFESTS)) == 8  # MAX_PINS
