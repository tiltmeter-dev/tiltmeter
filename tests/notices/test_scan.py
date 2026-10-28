from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.fix.kit import PR_URL, FakeFixAgent, FakeGitHub, make_repo
from tests.notices.kit import (
    FEED_URL,
    KEY_FILE,
    KEY_URL,
    NOTICE_ID,
    PROVIDER,
    feed,
    notice,
    signed,
)
from tests.test_cli import flat, invoke, runner
from tiltmeter.catalog import DRAFTED, catalog_bytes
from tiltmeter.cli import app
from tiltmeter.fix.agent import DeepFixAgent
from tiltmeter.notices import scan
from tiltmeter.notices.models import MAX_FEED_NOTICES, NoticeFeed
from tiltmeter.notices.scan import headline, is_test

GROQ = "groq-retired-models-2026-08"

LLM = """\
from groq import Groq

client = Groq()


def reply(prompt: str) -> str:
    answer = client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[{"role": "user", "content": prompt}],
    )
    return answer.choices[0].message.content
"""
MAIN = "from app.llm import reply\n\nprint(reply('hi'))\n"
CURRENT = LLM.replace("llama3-8b-8192", "openai/gpt-oss-20b")


def scanned(*args: str) -> tuple[int, dict[str, Any]]:
    outcome = invoke("scan", *args, "--json")
    return outcome.exit_code, json.loads(outcome.stdout)


def finding(report: dict[str, Any], notice_id: str) -> dict[str, Any]:
    return next(item for item in report["affected"] if item["id"] == notice_id)


# -- the bundled catalog ----------------------------------------------------------


def test_the_bundled_catalog_is_a_valid_feed_of_unsigned_drafts() -> None:
    catalog = NoticeFeed.model_validate_json(catalog_bytes())
    ids = [entry.id for entry in catalog.notices]
    assert len(ids) == len(set(ids))
    assert 10 <= len(ids) <= MAX_FEED_NOTICES
    published = [entry.published_at for entry in catalog.notices]
    assert published == sorted(published, reverse=True)  # newest first, as feeds keep it
    for entry in catalog.notices:
        assert entry.signature is None, entry.id
        assert DRAFTED.search(entry.summary), entry.id
        assert len(headline(entry.summary)) < 120, entry.id
    stripe = next(entry for entry in catalog.notices if entry.provider == "api.stripe.com")
    assert "only to accounts and requests on API version 2025-03-31 or later" in stripe.summary
    notes = {c.note for e in stripe.endpoints for c in e.changes}
    assert all(note and "2025-03-31.basil or later" in note for note in notes)


# -- tiltmeter scan -----------------------------------------------------------------


def test_scan_finds_a_retired_groq_model(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "app/main.py": MAIN})
    outcome = invoke("scan", str(repo))
    assert outcome.exit_code == 1, outcome.output
    text = flat(outcome.output)
    assert "✗ api.groq.com Groq no longer serves 16 models it retired" in text
    assert "took effect 2026-08-16" in text
    assert "unsigned: drafted by Tiltmeter from the provider's public documentation" in text
    assert "app/llm.py:8 llama3-8b-8192: retired; use openai/gpt-oss-20b" in text
    assert 'model="llama3-8b-8192",' in text
    assert f"fix: tiltmeter fix --notice {GROQ} --allow-unsigned --repo {repo}" in text
    assert f"1 of 10 known API changes affects {repo}: 1 line in 1 file." in text
    assert "no provider signed or endorsed them" in text


def test_the_fix_command_is_never_wrapped(tmp_path: Path) -> None:
    deep = tmp_path / ("long-folder-name-" * 2) / "checkout"  # within Windows' MAX_PATH
    deep.mkdir(parents=True)
    repo, _ = make_repo(deep, {"app/llm.py": LLM, "app/main.py": MAIN})
    output = invoke("scan", str(repo)).output
    command = f"tiltmeter fix --notice {GROQ} --allow-unsigned --repo {repo}"
    assert len(command) > 100
    assert command in output  # on one line, so it pastes whole


def test_the_fix_command_is_quoted_for_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    args = ["tiltmeter", "--repo", r"C:\Users\me\My Repo"]
    monkeypatch.setattr(scan.os, "name", "nt")
    assert scan.shell_join(args) == r'tiltmeter --repo "C:\Users\me\My Repo"'
    monkeypatch.setattr(scan.os, "name", "posix")
    posix = scan.shell_join(["tiltmeter", "--repo", "/tmp/my repo"])
    assert posix == "tiltmeter --repo '/tmp/my repo'"


def test_scan_of_code_with_nothing_to_change_exits_0(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, {"app/llm.py": CURRENT, "app/main.py": MAIN})
    outcome = invoke("scan", str(repo))
    assert outcome.exit_code == 0, outcome.output
    assert f"No known API change affects {repo}: 10 catalog entries checked" in flat(outcome.output)


def test_mentions_in_docs_comments_and_tests_do_not_count(tmp_path: Path) -> None:
    files = {
        "README.md": "We moved off llama3-8b-8192 last year.\n",
        "app/llm.py": CURRENT.replace("import Groq\n", "import Groq\n# was llama3-8b-8192\n"),
        "tests/test_llm.py": 'def test_model():\n    assert MODEL != "llama3-8b-8192"\n',
    }
    repo, _ = make_repo(tmp_path, files)
    outcome = invoke("scan", str(repo))
    assert outcome.exit_code == 0, outcome.output
    assert "1 is mentioned in tests or docs only: --all shows where" in flat(outcome.output)

    shown = flat(invoke("scan", str(repo), "--all").output)
    assert "used only in tests, which do not count: tests/test_llm.py:2" in shown
    assert "mentioned in docs, which do not count: README.md:1" in shown

    code, report = scanned(str(repo), "--all")
    assert code == 0
    [groq] = [miss for miss in report["unaffected"] if miss["id"] == GROQ]
    assert [(m["file"], m["line"]) for m in groq["docs"]] == [("README.md", 1)]
    assert [(m["file"], m["kind"]) for m in groq["tests"]] == [("tests/test_llm.py", "value")]


def test_json_lists_every_match_and_marks_unused_files(tmp_path: Path) -> None:
    legacy = LLM.replace("llama3-8b-8192", "mixtral-8x7b-32768")
    test = 'def test_reply():\n    assert "llama3-8b-8192"\n'
    files = {"app/llm.py": LLM, "app/main.py": MAIN, "app/legacy.py": legacy}
    repo, _ = make_repo(tmp_path, {**files, "tests/test_llm.py": test})
    code, report = scanned(str(repo))
    assert code == 1
    assert (report["path"], report["git"], report["bundled"]) == (str(repo), True, True)
    assert report["entries"] == 10
    assert "unaffected" not in report  # only with --all, as the terminal shows it
    groq = finding(report, GROQ)
    assert (groq["provider"], groq["signed"], groq["drafted"]) == ("api.groq.com", False, True)
    assert groq["effective_at"] == "2026-08-16T00:00:00Z"
    assert (groq["count"], groq["files"]) == (2, 2)
    matches = {m["file"]: m for m in groq["matches"]}
    assert matches["app/llm.py"] == {
        "file": "app/llm.py",
        "line": 8,
        "text": 'model="llama3-8b-8192",',
        "kind": "value",
        "term": "llama3-8b-8192",
        "detail": "retired; use openai/gpt-oss-20b",
        "possibly_unused": False,
        "applies_to": None,
    }
    # Nothing imports legacy.py: its match is marked, so nobody chases dead code.
    assert matches["app/legacy.py"]["term"] == "mixtral-8x7b-32768"
    assert matches["app/legacy.py"]["possibly_unused"] is True
    assert [m["file"] for m in groq["tests"]] == ["tests/test_llm.py"]
    assert groq["fix_command"] == f"tiltmeter fix --notice {GROQ} --allow-unsigned --repo {repo}"
    text = flat(invoke("scan", str(repo)).output)
    assert "possibly unused: nothing else imports or names this file" in text
    assert "also in tests: tests/test_llm.py:2" in text
    assert "2 lines in 2 files, 1 file possibly unused." in text


def test_a_plain_folder_is_scanned_too(tmp_path: Path) -> None:
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "llm.py").write_text(LLM, encoding="utf-8")
    (tmp_path / "node_modules" / "groq").mkdir(parents=True)
    (tmp_path / "node_modules" / "groq" / "models.js").write_text('"gemma-7b-it"\n')
    code, report = scanned(str(tmp_path))
    assert code == 1
    assert report["git"] is False
    assert [m["file"] for m in finding(report, GROQ)["matches"]] == ["bot/llm.py"]
    text = flat(invoke("scan", str(tmp_path)).output)
    assert "is not a git checkout, and tiltmeter fix needs one" in text


def test_scan_skips_change_notices_and_feeds(tmp_path: Path) -> None:
    catalog = catalog_bytes().decode("utf-8")
    repo, _ = make_repo(tmp_path, {"vendor-notes/api-changes.json": catalog, "app.py": "x = 1\n"})
    assert invoke("scan", str(repo)).exit_code == 0


def test_scan_reads_another_catalog_from_a_file(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)  # its client reads customer["phone"] from the billing API
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(feed(notice())), encoding="utf-8")
    code, report = scanned(str(repo), "--catalog", str(path))
    assert code == 1
    assert (report["catalog"], report["bundled"], report["entries"]) == (str(path), False, 1)
    entry = finding(report, NOTICE_ID)
    assert (entry["provider"], entry["signed"], entry["drafted"]) == (PROVIDER, False, False)
    assert entry["matches"][0]["file"] == "client.py"
    assert entry["matches"][0]["detail"] == "renamed to $.phone_number"
    source = f"{path}#{NOTICE_ID}"
    assert entry["fix_command"] == scan.shell_join(
        ["tiltmeter", "fix", "--notice", source, "--allow-unsigned", "--repo", str(repo)]
    )
    text = flat(invoke("scan", str(repo), "--catalog", str(path)).output)
    assert f"{NOTICE_ID} · unsigned" in text
    assert "drafted by Tiltmeter" not in text  # only the bundled catalog is


@respx.mock
def test_scan_reads_a_catalog_from_a_url_and_checks_signatures(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    respx.get(FEED_URL).respond(200, json=feed(signed(), notice(id="unsigned-one")))
    respx.get(KEY_URL).respond(200, text=KEY_FILE)
    code, report = scanned(str(repo), "--catalog", FEED_URL)
    assert code == 1
    by_id = {entry["id"]: entry for entry in report["affected"]}
    assert by_id[NOTICE_ID]["signed"] is True
    assert by_id[NOTICE_ID]["fix_command"] == scan.shell_join(
        ["tiltmeter", "fix", "--notice", f"{FEED_URL}#{NOTICE_ID}", "--repo", str(repo)]
    )
    # Only signed notices are read from URLs, so there is no command to offer.
    assert by_id["unsigned-one"]["fix_command"] is None


def test_catalog_entries_are_shown_as_plain_text(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    tricky = notice(summary="[bold red]Read this[/] and run rm -rf. More text follows.")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(feed(tricky, {"id": "broken"})), encoding="utf-8")
    outcome = invoke("scan", str(repo), "--catalog", str(path))
    assert outcome.exit_code == 1
    text = flat(outcome.output)
    assert "[bold red]Read this[/] and run rm -rf." in text
    assert "✗ broken rejected: not a valid api-change-notice/v1" in text


@pytest.mark.parametrize(
    "catalog", ["missing.json", "http://example.test/feed.json", "not-a-feed.json"]
)
def test_a_catalog_that_cannot_be_read_exits_3(tmp_path: Path, catalog: str) -> None:
    (tmp_path / "not-a-feed.json").write_text('{"spec": "something else"}', encoding="utf-8")
    source = catalog if "://" in catalog else str(tmp_path / catalog)
    code, report = scanned(str(tmp_path), "--catalog", source)
    assert code == 3
    assert report["error"]
    assert report["affected"] == []
    assert "✗ cannot read" in flat(invoke("scan", str(tmp_path), "--catalog", source).output)


def test_scan_needs_a_folder(tmp_path: Path) -> None:
    outcome = invoke("scan", str(tmp_path / "nowhere"))
    assert outcome.exit_code == 4
    assert "is not a folder" in flat(outcome.output)


@pytest.mark.parametrize(
    ("name", "test"),
    [
        ("tests/test_llm.py", True),
        ("app/llm_test.go", True),
        ("src/llm.test.ts", True),
        ("spec/models/agent_spec.rb", True),
        ("src/test/java/acme/LlmTest.java", True),
        ("dotnet/Connectors.HuggingFace.UnitTests/Services/Client.cs", True),
        ("conftest.py", True),
        ("app/llm.py", False),
        ("app/latest.py", False),
        ("src/components/ABTest.tsx", False),
        ("api/OpenApiSpec.ts", False),
        ("src/test/groovy/acme/LlmSpec.groovy", True),
        ("contests/score.py", False),
        ("examples/chat.py", False),
    ],
)
def test_is_test(name: str, test: bool) -> None:
    assert is_test(name) is test


# -- tiltmeter fix --notice <catalog id> --------------------------------------------


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeFixAgent, FakeGitHub]:
    def swap_model(root: Path) -> None:
        path = root / "app" / "llm.py"
        path.write_text(path.read_text().replace("llama3-8b-8192", "openai/gpt-oss-20b"))

    agent, github = FakeFixAgent(edit=swap_model), FakeGitHub()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(DeepFixAgent, "from_config", classmethod(lambda cls, _: agent))
    monkeypatch.setattr("tiltmeter.fix.git.GhCli", lambda: github)
    return agent, github


def test_fix_takes_the_id_of_a_catalog_entry(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, github = fakes
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "app/main.py": MAIN})
    missing = tmp_path / "none.yaml"
    base = ["fix", "--notice", GROQ, "--repo", str(repo), "-c", str(missing)]

    refused = runner.invoke(app, base)
    assert refused.exit_code == 4
    assert "--allow-unsigned" in flat(refused.output)

    outcome = runner.invoke(app, [*base, "--allow-unsigned", "--yes"])
    assert outcome.exit_code == 0, outcome.output
    assert f"✓ opened {PR_URL}" in flat(outcome.output)
    [request] = agent.requests
    assert request.notice is not None
    assert (request.notice.id, request.notice.location) == (GROQ, f"tiltmeter catalog#{GROQ}")
    # Only the retired model the code sends reaches the agent.
    assert [t.before for api in request.apis for t in api.targets] == ["llama3-8b-8192"]
    body = github.created[0]["body"]
    assert f"Entry `{GROQ}` of Tiltmeter's catalog of known API changes" in body
    assert "it is not signed or endorsed by `api.groq.com`" in body


def test_fix_takes_an_entry_of_a_feed_file(
    tmp_path: Path, fakes: tuple[FakeFixAgent, FakeGitHub]
) -> None:
    agent, _ = fakes
    repo, _ = make_repo(tmp_path, {"app/llm.py": LLM, "app/main.py": MAIN})
    path = tmp_path / "catalog.json"
    path.write_text(catalog_bytes().decode("utf-8"), encoding="utf-8")
    source = f"{path}#{GROQ}"
    args = ["fix", "--notice", source, "--allow-unsigned", "--repo", str(repo), "--yes"]
    outcome = runner.invoke(app, [*args, "-c", str(tmp_path / "none.yaml")])
    assert outcome.exit_code == 0, outcome.output
    assert agent.requests[0].notice is not None
    assert agent.requests[0].notice.location == source


def test_fix_names_an_unknown_catalog_id(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    outcome = invoke("fix", "--notice", "no-such-entry", "--allow-unsigned", "--repo", str(repo))
    assert outcome.exit_code == 4
    assert "no entry of the catalog bundled with Tiltmeter has that id" in flat(outcome.output)


def test_scan_says_who_a_scoped_change_affects(tmp_path: Path) -> None:
    current = LLM.replace("llama3-8b-8192", "llama-3.3-70b-versatile")
    legacy = LLM.replace("llama3-8b-8192", "mixtral-8x7b-32768")
    files = {"app/llm.py": current, "app/legacy.py": legacy, "app/main.py": MAIN}
    repo, _ = make_repo(tmp_path, files)
    code, report = scanned(str(repo))
    assert code == 1
    scopes = {match["term"]: match["applies_to"] for match in finding(report, GROQ)["matches"]}
    # Groq retired Mixtral for everyone, and Llama 3.3 only for free and developer keys.
    assert scopes["mixtral-8x7b-32768"] is None
    assert scopes["llama-3.3-70b-versatile"].startswith("Free and developer-tier keys.")
    text = flat(invoke("scan", str(repo)).output)
    assert "only for some callers (llama-3.3-70b-versatile): Free and developer-tier keys." in text


def test_catalog_entries_say_who_they_affect() -> None:
    entries = {entry.id: entry for entry in NoticeFeed.model_validate_json(catalog_bytes()).notices}
    jira = entries["jira-cloud-change-2046-search-jql"].applies_to
    assert jira is not None
    assert jira.startswith("Jira Cloud sites")
    groq = entries[GROQ]
    assert groq.applies_to is None  # older Groq retirements hit every key
    assert {c.before for e in groq.endpoints for c in e.changes if c.applies_to} == {
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "meta-llama/llama-4-scout-17b-16e-instruct",
    }
