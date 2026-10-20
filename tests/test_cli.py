from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from driftwatch import __version__, cli
from driftwatch.cli import app, exit_code_for, main
from driftwatch.core.models import CheckResult, Severity
from driftwatch.llm.explainer import ClaudeExplainer
from tests.helpers import API, FakeExplainer

URL = f"{API}/v1/items"
runner = CliRunner()


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "driftwatch.yaml"
    path.write_text(
        f"endpoints:\n  - id: items\n    url: {URL}\n    retries: 0\n{extra}", encoding="utf-8"
    )
    return path


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args))


def flat(text: str) -> str:
    """Collapse the line wrapping rich applies at 80 columns."""
    return " ".join(text.split())


def result(severity: Severity | None = None, error: str | None = None) -> CheckResult:
    return CheckResult(endpoint_id="x", checked_at="t", max_severity=severity, error=error)


def test_exit_code_precedence() -> None:
    assert exit_code_for([]) == 0
    assert exit_code_for([result(Severity.INFO)]) == 0
    assert exit_code_for([result(Severity.WARNING)]) == 1
    assert exit_code_for([result(Severity.WARNING), result(error="down")]) == 3
    assert exit_code_for([result(Severity.BREAKING), result(error="down")]) == 2


def test_version() -> None:
    outcome = invoke("--version")
    assert outcome.exit_code == 0
    assert __version__ in outcome.output


def test_init(tmp_path: Path) -> None:
    target = tmp_path / "driftwatch.yaml"
    first = invoke("init", str(target))
    assert first.exit_code == 0
    assert "github_repo" in target.read_text()
    assert not (tmp_path / ".gitignore").exists()  # not a git repo, no .gitignore
    again = invoke("init", str(target))
    assert again.exit_code == 4
    assert "already exists" in again.output
    (tmp_path / ".gitignore").write_text("*.pyc")
    forced = invoke("init", str(target), "--force")
    assert forced.exit_code == 0
    assert (tmp_path / ".gitignore").read_text() == "*.pyc\n.driftwatch/\n"
    invoke("init", str(target), "--force")
    assert (tmp_path / ".gitignore").read_text().count(".driftwatch/") == 1

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    invoke("init", str(repo / "driftwatch.yaml"))
    assert (repo / ".gitignore").read_text() == ".driftwatch/\n"


@respx.mock
def test_check_exit_codes_and_output(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    route = respx.get(URL).respond(200, json={"id": 1, "phone": "555"})
    first = invoke("check", "-c", str(config))
    assert first.exit_code == 0, first.output
    assert "baseline recorded" in first.output

    route.respond(200, json={"id": 1, "phone_number": "555"})
    drifted = invoke("check", "-c", str(config))
    assert drifted.exit_code == 2
    assert "BREAKING" in drifted.output
    assert "$.phone" in drifted.output
    assert "possibly renamed to $.phone_number" in flat(drifted.output)
    assert "set ANTHROPIC_API_KEY to explain drift" in flat(drifted.output)

    as_json = invoke("check", "-c", str(config), "--json", "--no-llm")
    assert as_json.exit_code == 0
    [payload] = json.loads(as_json.output)
    assert payload["outcome"] == "clean"

    route.respond(503)
    failed = invoke("check", "-c", str(config), "-e", "items")
    assert failed.exit_code == 2  # 200 -> 503 is a breaking status change
    route.respond(503)
    still_failing = invoke("check", "-c", str(config))
    assert still_failing.exit_code == 3
    assert "unexpected HTTP status 503" in flat(still_failing.output)


@respx.mock
def test_warning_drift_exits_1(tmp_path: Path) -> None:
    config = write_config(tmp_path, "    severity_overrides: {field_removed: warning}\n")
    route = respx.get(URL).respond(200, json={"id": 1, "gone": True})
    invoke("check", "-c", str(config))
    route.respond(200, json={"id": 1})
    outcome = invoke("check", "-c", str(config), "--no-llm")
    assert outcome.exit_code == 1
    assert "WARNING" in outcome.output


def test_config_problems_exit_4(tmp_path: Path) -> None:
    missing = invoke("check", "-c", str(tmp_path / "nope.yaml"))
    assert missing.exit_code == 4
    assert "not found" in missing.output
    config = write_config(tmp_path)
    unknown = invoke("check", "-c", str(config), "-e", "ghost")
    assert unknown.exit_code == 4
    assert "unknown endpoint" in unknown.output


@respx.mock
def test_history_show_and_schema(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    empty = invoke("history", "-c", str(config))
    assert empty.exit_code == 0
    assert "No checks yet" in empty.output
    no_schema = invoke("schema", "items", "-c", str(config))
    assert no_schema.exit_code == 3

    respx.get(URL).respond(200, json={"id": 1, "tags": ["a"]})
    invoke("check", "-c", str(config))
    invoke("check", "-c", str(config))

    table = invoke("history", "-c", str(config))
    assert table.exit_code == 0
    assert "#2" in table.output
    assert "baseline recorded" in table.output
    listed = json.loads(invoke("history", "items", "-c", str(config), "--json", "-n", "1").output)
    assert [item["check_id"] for item in listed] == [2]

    shown = invoke("show", "1", "-c", str(config))
    assert shown.exit_code == 0
    assert "#1" in shown.output
    assert json.loads(invoke("show", "2", "-c", str(config), "--json").output)["check_id"] == 2
    assert invoke("show", "99", "-c", str(config)).exit_code == 4

    schema = invoke("schema", "items", "-c", str(config))
    assert schema.exit_code == 0
    document = json.loads(schema.output)
    assert document["properties"]["tags"]["items"] == {"type": "string"}
    assert invoke("schema", "ghost", "-c", str(config)).exit_code == 4


@respx.mock
def test_explain_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_config(tmp_path)
    route = respx.get(URL).respond(200, json={"id": 1, "phone": "555"})
    invoke("check", "-c", str(config))
    route.respond(200, json={"id": 1, "phone_number": "555"})
    invoke("check", "-c", str(config), "--no-llm")

    without_key = invoke("explain", "2", "-c", str(config))
    assert without_key.exit_code == 4
    assert "ANTHROPIC_API_KEY" in without_key.output

    fake = FakeExplainer()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(ClaudeExplainer, "from_config", classmethod(lambda cls, _: fake))
    explained = invoke("explain", "2", "-c", str(config))
    assert explained.exit_code == 0, explained.output
    assert "Read phone_number instead." in flat(explained.output)
    as_json = json.loads(invoke("explain", "2", "-c", str(config), "--json").output)
    assert as_json["explanation"]["confidence"] == "high"

    fake.error = RuntimeError("authentication failed")
    broken = invoke("explain", "2", "-c", str(config))
    assert broken.exit_code == 3
    assert "explanation failed: authentication failed" in flat(broken.output)


@respx.mock
def test_watch_runs_rounds_until_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path)
    respx.get(URL).respond(200, json={"id": 1})
    rounds: list[float] = []

    async def pause(seconds: float) -> None:
        rounds.append(seconds)
        if len(rounds) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_pause", pause)
    outcome = invoke("watch", "-c", str(config), "--interval", "5", "--json")
    assert outcome.exit_code == 0
    assert rounds == [5.0, 5.0]
    lines = [json.loads(line) for line in outcome.stdout.splitlines() if line.startswith("{")]
    assert [line["outcome"] for line in lines] == ["baseline", "clean"]


def test_main_maps_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as bad_flag:
        main(["check", "--bogus"])
    assert bad_flag.value.code == 4
    with pytest.raises(SystemExit) as no_args:
        main([])
    assert no_args.value.code == 0
    with pytest.raises(SystemExit) as version:
        main(["--version"])
    assert version.value.code == 0
    with pytest.raises(SystemExit) as missing:
        main(["check", "-c", str(tmp_path / "missing.yaml")])
    assert missing.value.code == 4
    assert "not found" in capsys.readouterr().err
