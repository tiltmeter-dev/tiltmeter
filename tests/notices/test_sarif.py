from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.fix.kit import make_repo
from tests.notices.kit import feed, notice
from tests.notices.test_scan import GROQ, LLM, MAIN, dated
from tests.test_cli import invoke
from tiltmeter import __version__
from tiltmeter.cli import EXIT_USAGE
from tiltmeter.notices.sarif import plain


def scanned(tmp_path: Path, *args: str) -> tuple[int, dict[str, Any]]:
    """The exit code of a scan with ``--sarif`` and the log it wrote."""
    out = tmp_path / "out.sarif"
    outcome = invoke("scan", *args, "--sarif", str(out))
    return outcome.exit_code, json.loads(out.read_text(encoding="utf-8"))


def catalog(tmp_path: Path, *entries: dict[str, Any]) -> str:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(feed(*entries)), encoding="utf-8")
    return str(path)


def test_a_scan_is_written_as_sarif(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "main.py": MAIN})
    code, log = scanned(tmp_path, str(repo))
    assert code == 1  # the exit code is the scan's, with or without the file
    assert (log["version"], log["$schema"]) == (
        "2.1.0",
        "https://json.schemastore.org/sarif-2.1.0.json",
    )
    [run] = log["runs"]
    driver = run["tool"]["driver"]
    assert (driver["name"], driver["version"]) == ("Tiltmeter", __version__)
    assert driver["informationUri"] == "https://github.com/tiltmeter-dev/tiltmeter"
    assert run["invocations"] == [{"executionSuccessful": True}]
    [rule] = driver["rules"]
    assert rule["id"] == f"api.groq.com/{GROQ}"
    assert rule["defaultConfiguration"] == {"level": "error"}
    assert rule["properties"]["tags"] == ["api-change", "api.groq.com"]
    assert rule["properties"]["signed"] is False
    assert rule["help"]["text"].count("not signed or endorsed by Groq") == 1
    assert f"To fix it: tiltmeter fix --notice {GROQ} --allow-unsigned" in rule["help"]["text"]
    [result] = run["results"]
    assert (result["ruleId"], result["ruleIndex"], result["level"]) == (rule["id"], 0, "error")
    assert result["locations"] == [
        {
            "physicalLocation": {
                "artifactLocation": {"uri": "app/llm.py", "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": 8},
            }
        }
    ]
    assert result["message"]["text"].startswith("llama3-8b-8192: retired; use openai/gpt-oss-20b. ")
    assert result["message"]["text"].endswith(" In effect since 2026-08-16.")
    assert result["properties"] == {"kind": "value", "term": "llama3-8b-8192"}


def test_the_terminal_and_json_output_stay_as_they_were(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "main.py": MAIN})
    out = tmp_path / "out.sarif"
    plain_run = invoke("scan", str(repo))
    with_file = invoke("scan", str(repo), "--sarif", str(out))
    assert with_file.output == plain_run.output
    as_json = invoke("scan", str(repo), "--json", "--sarif", str(out))
    assert json.loads(as_json.stdout)["affected"][0]["id"] == GROQ
    assert out.read_text(encoding="utf-8").endswith("}\n")


def test_paths_start_at_the_repository_root(tmp_path: Path) -> None:
    # Code scanning looks for app/llm.py, wherever in the repository the scan started.
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "main.py": MAIN})
    _, log = scanned(tmp_path, str(repo / "app"))
    [result] = log["runs"][0]["results"]
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "app/llm.py"
    # Outside git there is no root but the folder itself.
    plain_folder = tmp_path / "plain"
    (plain_folder / "src").mkdir(parents=True)
    (plain_folder / "src" / "llm.py").write_text(LLM, encoding="utf-8")
    _, log = scanned(tmp_path, str(plain_folder / "src"))
    [result] = log["runs"][0]["results"]
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "llm.py"


def test_a_change_ahead_is_a_warning_and_one_too_far_a_note(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    entries = catalog(
        tmp_path, dated("shipped", "2021-01-01T00:00:00Z"), dated("far", "2099-06-01T00:00:00Z")
    )
    code, log = scanned(tmp_path, str(repo), "--catalog", entries)
    assert code == 1
    levels = {rule["id"]: rule["defaultConfiguration"]["level"] for rule in rules(log)}
    assert levels == {"api.billing.test/shipped": "error", "api.billing.test/far": "warning"}
    ahead = [r for r in log["runs"][0]["results"] if r["ruleId"].endswith("/far")]
    assert ahead
    assert all(result["level"] == "warning" for result in ahead)
    assert " Takes effect on 2099-06-01, in " in ahead[0]["message"]["text"]
    # Further away than --within allows: listed, and does not fail the job.
    only_far = catalog(tmp_path, dated("far", "2099-06-01T00:00:00Z"))
    code, log = scanned(tmp_path, str(repo), "--catalog", only_far, "--within", "30")
    assert code == 0
    assert {result["level"] for result in log["runs"][0]["results"]} == {"note"}


def rules(log: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = log["runs"][0]["tool"]["driver"]["rules"]
    return rules


def test_a_notices_text_cannot_make_links(tmp_path: Path) -> None:
    # A SARIF message is plain text in which [text](target) is a link.
    repo, _ = make_repo(tmp_path)
    crafted = notice(summary="Renamed. See [the guide](https://evil.example) \\ now.")
    code, log = scanned(tmp_path, str(repo), "--catalog", catalog(tmp_path, crafted))
    assert code == 1
    [rule] = rules(log)
    escaped = "See \\[the guide\\](https://evil.example) \\\\ now."
    assert escaped in rule["fullDescription"]["text"]
    assert escaped in rule["help"]["text"]
    assert "Not signed by the provider." in rule["help"]["text"]
    assert "[the guide]" not in json.dumps(log).replace("\\\\[", "").replace("\\\\]", "")
    assert plain("a [b](c) \\ d") == "a \\[b\\](c) \\\\ d"


def test_nothing_found_is_a_run_without_results(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app.py": "print('hello')\n"})
    code, log = scanned(tmp_path, str(repo))
    assert code == 0
    [run] = log["runs"]
    assert (run["results"], run["tool"]["driver"]["rules"]) == ([], [])
    assert run["invocations"] == [{"executionSuccessful": True}]


def test_a_catalog_that_cannot_be_read_is_a_failed_run(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    code, log = scanned(tmp_path, str(repo), "--catalog", str(tmp_path / "missing.json"))
    assert code == 3
    [run] = log["runs"]
    assert run["results"] == []
    [invocation] = run["invocations"]
    assert invocation["executionSuccessful"] is False
    [problem] = invocation["toolExecutionNotifications"]
    assert problem["level"] == "error"
    assert "missing.json" in problem["message"]["text"]


def test_a_file_that_cannot_be_written_is_an_error(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app.py": "print('hello')\n"})
    outcome = invoke("scan", str(repo), "--sarif", str(tmp_path / "no" / "such" / "out.sarif"))
    assert outcome.exit_code == EXIT_USAGE
    assert "cannot write" in outcome.output
