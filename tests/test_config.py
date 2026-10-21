from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.helpers import make_config, make_endpoint
from tiltmeter.config import (
    EXAMPLE_CONFIG,
    Config,
    EndpointConfig,
    load_config,
    resolve_state_dir,
)
from tiltmeter.core.models import ChangeKind, Severity
from tiltmeter.errors import ConfigError, UnknownEndpointError

EXAMPLES = Path(__file__).parent.parent / "examples"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "tiltmeter.yaml"
    path.write_text(text)
    return path


def test_example_config_is_valid(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, EXAMPLE_CONFIG))
    assert config.endpoint_ids == ["github_repo"]
    assert config.llm.model == "claude-haiku-5-5"
    assert config.notify == []
    assert config.fix.model == "claude-sonnet-5-5"


def test_fix_defaults_and_overrides(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            "fix:\n  model: claude-opus-5-5\n  branch_prefix: bots/api\n  ignore: [vendor]\n"
            "endpoints:\n  - {id: a, url: https://x.test}\n",
        )
    )
    assert config.fix.model == "claude-opus-5-5"
    assert config.fix.branch_prefix == "bots/api"
    assert config.fix.ignore == ["vendor"]
    assert config.fix.effort == "medium"
    assert config.fix.agent == "deepagents"
    defaults = make_config().fix
    assert defaults.ignore == [".env*", "*.pem", "*.key", "secrets*", ".tiltmeter"]
    assert defaults.max_model_calls == 40


@pytest.mark.skipif(not EXAMPLES.exists(), reason="examples folder not present")
def test_shipped_examples_are_valid() -> None:
    config = load_config(EXAMPLES / "tiltmeter.yaml")
    assert len(config.endpoints) >= 2


def test_defaults_and_coercion() -> None:
    endpoint = EndpointConfig.model_validate(
        {
            "id": "a",
            "url": "https://x.test/v1?key=${KEY}#frag",
            "method": "post",
            "params": {"limit": 3, "flag": True, "empty": None},
            "headers": {"X-Num": 7},
        }
    )
    assert endpoint.method == "POST"
    assert endpoint.params == {"limit": "3", "flag": "True", "empty": ""}
    assert endpoint.headers == {"X-Num": "7"}
    assert endpoint.display_url() == "https://x.test/v1"
    assert endpoint.url.endswith("${KEY}#frag")  # placeholders are kept, not expanded
    assert (endpoint.sample_size, endpoint.baseline_window, endpoint.latency_factor) == (1, 20, 3.0)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("endpoints: []", "endpoints"),
        ("endpoints:\n  - {id: a, url: ftp://x}", "http://"),
        ("endpoints:\n  - {id: 'bad id', url: https://x.test}", "endpoints.0.id"),
        ("endpoints:\n  - {id: a, url: https://x.test, ignore_paths: ['data']}", "start with"),
        ("endpoints:\n  - {id: a, url: https://x.test, surprise: 1}", "surprise"),
        (
            "endpoints:\n  - {id: a, url: https://x.test}\n  - {id: a, url: https://y.test}",
            "duplicate",
        ),
        (
            "endpoints:\n  - {id: a, url: https://x.test}\nnotify:\n  - {type: pager, url: x}",
            "notify.0",
        ),
        ("version: 2\nendpoints:\n  - {id: a, url: https://x.test}", "version"),
        (
            "endpoints:\n  - {id: a, url: https://x.test, severity_overrides: {nope: info}}",
            "severity_overrides",
        ),
        ("fix: {branch_prefix: 'a..b'}\nendpoints:\n  - {id: a, url: https://x.test}", "fix"),
        ("fix: {branch_prefix: '/x'}\nendpoints:\n  - {id: a, url: https://x.test}", "fix"),
        ("fix: {ignore: ['../up']}\nendpoints:\n  - {id: a, url: https://x.test}", "inside"),
        ("fix: {max_model_calls: 0}\nendpoints:\n  - {id: a, url: https://x.test}", "fix"),
        ("fix: {agent: codex}\nendpoints:\n  - {id: a, url: https://x.test}", "fix.agent"),
    ],
)
def test_invalid_configs_have_readable_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match="invalid configuration") as caught:
        load_config(write(tmp_path, text))
    assert message in str(caught.value)


def test_file_level_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write(tmp_path, "endpoints: [unclosed"))
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- just\n- a list\n"))


def test_select_and_lookup() -> None:
    config = make_config(make_endpoint(id="a"), make_endpoint(id="b"), make_endpoint(id="c"))
    assert [e.id for e in config.select()] == ["a", "b", "c"]
    assert [e.id for e in config.select(["c", "a", "c"])] == ["c", "a"]
    with pytest.raises(UnknownEndpointError, match="configured endpoints: a, b, c"):
        config.endpoint("zzz")


def test_severity_override_precedence() -> None:
    endpoint = make_endpoint(severity_overrides={"field_added": "breaking"})
    config = make_config(
        endpoint,
        severity_overrides={"field_added": "warning", "field_removed": "warning"},
    )
    assert config.overrides_for(config.endpoints[0]) == {
        ChangeKind.FIELD_ADDED: Severity.BREAKING,
        ChangeKind.FIELD_REMOVED: Severity.WARNING,
    }


def test_state_dir_resolution(tmp_path: Path) -> None:
    config_path = write(tmp_path, yaml.safe_dump({"endpoints": [{"id": "a", "url": "https://x"}]}))
    config = load_config(config_path)
    assert resolve_state_dir(config, config_path) == tmp_path.resolve() / ".tiltmeter"
    absolute = Config.model_validate({**config.model_dump(), "state_dir": str(tmp_path / "abs")})
    assert resolve_state_dir(absolute, config_path) == tmp_path / "abs"
    home = Config.model_validate({**config.model_dump(), "state_dir": "~/dw"})
    assert resolve_state_dir(home, config_path) == Path("~/dw").expanduser()
