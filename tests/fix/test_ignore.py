from __future__ import annotations

import pytest
import wcmatch.glob as wcglob

from tiltmeter.config import DEFAULT_FIX_IGNORE
from tiltmeter.fix.ignore import deny_globs, is_ignored

DEFAULTS = list(DEFAULT_FIX_IGNORE)


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        (".env", True),
        ("config/.env.local", True),
        ("deploy/tls.pem", True),
        ("secrets.yaml", True),
        ("app/secrets/db.txt", True),
        (".tiltmeter/tiltmeter.db", True),
        (".git/config", True),
        ("client.py", False),
        ("docs/environment.md", False),
        ("src/keys.py", False),
    ],
)
def test_name_patterns_match_at_any_depth(path: str, ignored: bool) -> None:
    assert is_ignored(path, DEFAULTS) is ignored


def test_anchored_patterns_match_from_the_root() -> None:
    assert is_ignored("infra/prod/vars.tf", ["infra/prod"])
    assert is_ignored("infra/prod/vars.tf", ["/infra/*/vars.tf"])
    assert not is_ignored("app/infra/prod/vars.tf", ["infra/prod"])


@pytest.mark.parametrize(
    "path",
    ["/.env", "/config/.env.local", "/deploy/tls.pem", "/app/secrets/db.txt", "/.git/HEAD"],
)
def test_deny_globs_block_the_same_files_for_the_agent(path: str) -> None:
    flags = wcglob.BRACE | wcglob.GLOBSTAR
    assert any(wcglob.globmatch(path, glob, flags=flags) for glob in deny_globs(DEFAULTS))
    assert not any(
        wcglob.globmatch("/client.py", glob, flags=flags) for glob in deny_globs(DEFAULTS)
    )
