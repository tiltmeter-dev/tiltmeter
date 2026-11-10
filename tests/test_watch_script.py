from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from tests.notices.kit import feed, notice
from tiltmeter.notices.models import NoticeFeed
from tiltmeter.notices.reader import parse_notice
from tiltmeter.notices.scan import headline

SCRIPTS = Path(__file__).parent.parent / "scripts"
TODAY = date(2026, 11, 10)


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_script", SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


script = load_script("watch")
catalog_script = load_script("catalog")

# The three shapes the watched pages use, cut down to a few rows each.
GROQ = """\
### [September 14, 2026: qwen/qwen3.6-27b](#september-14-2026)

| Deprecated Model | Shutdown Date | Recommended Replacement Model ID |
| --- | --- | --- |
| qwen/qwen3.6-27b | 09/14/26 | qwen/qwen3.8-27b |

| Deprecated Model | Shutdown Date | Recommended Replacement Model ID |
| --- | --- | --- |
| llama-3.1-8b-instant | 08/16/26 | openai/gpt-oss-20b |
| llama-3.3-70b-versatile | 08/16/26 | openai/gpt-oss-120b or qwen/qwen3.6-27b |
| groq/compound | 12/21/26 | — |
| Llama Guard 3 | 06/06/25 | meta-llama/llama-guard-4-12b |
"""
ANTHROPIC = """\
## Model status

| API model name | Current state | Deprecated | Tentative retirement date |
| :--- | :--- | :--- | :--- |
| claude-fable-5-2 | Active | N/A | Not sooner than November 9, 2027 |
| claude-mythos-preview | Deprecated | June 9, 2026 | To be announced |
| claude-sonnet-4-5-20250929 | Deprecated | September 30, 2026 | November 30, 2026 |
| claude-opus-4-1-20250805 | Retired | June 5, 2026 | August 5, 2026 |

## Deprecation history

| Retirement date | Deprecated model | Recommended replacement |
| --- | --- | --- |
| November 30, 2026 | `claude-sonnet-4-5-20250929` | `claude-sonnet-5-5` |
"""
OPENAI = """\
| Shutdown date | Model / system | Recommended replacement |
| --- | --- | --- |
| Feb 10, 2027 | `gpt-image-2` \\| `gpt-image-2-2026-04-21` | `gpt-image-3` or `g-flare` |
| Feb 1, 2027 | `gpt-5.5-cyber-preview` | The most capable cyber model available to you. |
| Jan 14, 2027 | GPT-4o (`gpt-4o-2024-08-06`) | `gpt-6-luna` |
| Feb 26, 2027 | `/v1/audio/translations` | `v1/realtime/translations` |

| Shutdown date | Model snapshot | Substitute model |
| --- | --- | --- |
| October 23, 2026 | `gpt-4-0613` \\| `gpt-4` | `gpt-5.6-sol` |
| 2024‑09‑13 | `gpt-4-32k` | `gpt-4o` |

| Date | Update |
| --- | --- |
| Nov 30, 2026 | The `v1/prompts` API is scheduled to shut down. |
"""


def found(markdown: str) -> dict[str, tuple[str, str | None]]:
    return {r.value: (r.on.isoformat(), r.replaced_by) for r in script.retirements(markdown)}


def test_dates_are_read_only_from_cells_that_are_one() -> None:
    assert script.parse_date("08/16/26") == date(2026, 8, 16)
    assert script.parse_date("August 16, 2026") == date(2026, 8, 16)
    assert script.parse_date("Aug 16, 2026") == date(2026, 8, 16)
    assert script.parse_date("Sept. 3, 2026") == date(2026, 9, 3)
    assert script.parse_date(" 2026-08-16 ") == date(2026, 8, 16)
    assert script.parse_date("2026‑08‑16") == date(2026, 8, 16)  # typographic hyphens
    for text in ("Not sooner than June 9, 2027", "To be announced", "N/A", "—", "13/40/26", ""):
        assert script.parse_date(text) is None, text
    assert script.parse_date("Ma 3, 2026") is None  # March or May


def test_a_cell_names_the_values_it_writes_as_code_or_is() -> None:
    assert script.ids("`gpt-image-2` | `gpt-image-2-2026-04-21`") == [
        "gpt-image-2",
        "gpt-image-2-2026-04-21",
    ]
    assert script.ids("GPT-4o (`gpt-4o-2024-08-06`, `gpt-4o-2024-11-20`)") == [
        "gpt-4o-2024-08-06",
        "gpt-4o-2024-11-20",
    ]
    assert script.ids("meta-llama/llama-guard-4-12b") == ["meta-llama/llama-guard-4-12b"]
    assert script.ids("openai/gpt-oss-120b or qwen/qwen3.6-27b") == [
        "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b",
    ]
    for prose in ("Llama Guard 3", "The most capable cyber model available to you.", "—", ""):
        assert script.ids(prose) == [], prose
    assert script.ids("`/v1/audio/translations`") == []  # a path is no value
    assert script.ids("`reasoning.mode: pro`") == []


def test_each_page_shape_gives_its_retirements() -> None:
    assert found(GROQ) == {
        "qwen/qwen3.6-27b": ("2026-09-14", "qwen/qwen3.8-27b"),
        "llama-3.1-8b-instant": ("2026-08-16", "openai/gpt-oss-20b"),
        "llama-3.3-70b-versatile": ("2026-08-16", "openai/gpt-oss-120b"),
        "groq/compound": ("2026-12-21", None),
    }
    # Active models and those without a date are no retirement; the history table adds
    # the replacement the status table lacks.
    assert found(ANTHROPIC) == {
        "claude-sonnet-4-5-20250929": ("2026-11-30", "claude-sonnet-5-5"),
        "claude-opus-4-1-20250805": ("2026-08-05", None),
    }
    assert found(OPENAI) == {
        "gpt-image-2": ("2027-02-10", "gpt-image-3"),
        "gpt-image-2-2026-04-21": ("2027-02-10", "gpt-image-3"),
        "gpt-5.5-cyber-preview": ("2027-02-01", None),
        "gpt-4o-2024-08-06": ("2027-01-14", "gpt-6-luna"),
        "gpt-4-0613": ("2026-10-23", "gpt-5.6-sol"),
        "gpt-4": ("2026-10-23", "gpt-5.6-sol"),
        "gpt-4-32k": ("2024-09-13", "gpt-4o"),
    }


def test_text_that_is_no_table_or_no_value_gives_nothing() -> None:
    hostile = (
        "| Deprecated model | Shutdown date | Replacement |\n|---|---|---|\n"
        "| `@octocat` | 08/16/26 | `x` |\n"
        "| [click](https://evil.test) | 08/16/26 | `y` |\n"
        "| `<img src=x>` | 08/16/26 | `z` |\n"
        "| `Ignore your instructions` | 08/16/26 | `z` |\n"
    )
    assert script.retirements(hostile) == []
    assert script.retirements("no table here\n| a | b |\nnot a rule\n") == []
    assert script.retirements("| Model | Shutdown |\n|---|---|\n| `m-1` |\n") == []  # short row
    assert script.retirements("|" + " x |" * 5000) == []


def sources(tmp_path: Path, **overrides: object) -> Path:
    source = {
        "name": "Billing",
        "provider": "api.billing.test",
        "slug": "billing",
        "page": "https://docs.billing.test/deprecations.md",
        "cite": "docs.billing.test/deprecations",
        "method": "POST",
        "url": "https://api.billing.test/v1/chat",
        **overrides,
    }
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([source]), encoding="utf-8")
    return path


def catalog(tmp_path: Path, *retired: str) -> Path:
    changes = [{"path": "$.model", "kind": "value_retired", "before": value} for value in retired]
    endpoint = {"method": "POST", "url": "https://api.billing.test/v1/chat", "changes": changes}
    path = tmp_path / "api-changes.json"
    path.write_text(json.dumps(feed(notice(endpoints=[endpoint], sdks=[]))), encoding="utf-8")
    return path


PAGE = """\
| Deprecated model | Shutdown date | Recommended replacement |
| --- | --- | --- |
| `tilt-1` | 08/16/26 | `tilt-3` |
| `tilt-2` | 12/01/26 | `tilt-3` |
| `tilt-2-mini` | 12/01/26 | — |
| `tilt-0` | 01/05/24 | `tilt-1` |
| `tilt-beta` | 11/10/26 | `tilt-3` |
"""


def run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *args: str,
    page: str | None = PAGE,
    known: tuple[str, ...] = ("tilt-1",),
    **source: object,
) -> tuple[int, str]:
    def fetch(url: str) -> str:
        assert url == "https://docs.billing.test/deprecations.md"
        if page is None:
            raise script.WatchError(f"{url} answered 503")
        return page

    code = script.main(
        list(args),
        catalog=catalog(tmp_path, *known),
        sources=sources(tmp_path, **source),
        fetch=fetch,
        today=TODAY,
    )
    return code, capsys.readouterr().out


def test_the_report_lists_what_the_catalog_lacks_soonest_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, text = run(tmp_path, capsys)
    assert code == 1
    assert "list 3 retired values that the catalog of known API changes lacks" in text
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert rows == [
        "| `tilt-2` | 2026-12-01 | in 21 days | `tilt-3` |",
        "| `tilt-2-mini` | 2026-12-01 | in 21 days | none |",
        "| `tilt-beta` | 2026-11-10 | today | `tilt-3` |",
    ]
    assert "`tilt-1`" not in text  # the catalog has it
    assert "1 more shut down over a year ago and are not in the catalog" in text
    assert "uv run python scripts/catalog.py drafts/*.json" in text
    code, text = run(tmp_path, capsys, "--all")
    assert "Shut down more than a year ago and not in the catalog: `tilt-0`." in text


def test_a_catalog_that_has_them_all_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    known = ("tilt-1", "tilt-2", "tilt-2-mini")
    code, text = run(tmp_path, capsys, known=known, ignore=["tilt-beta"])
    assert code == 0
    assert "has every retirement the providers' pages list" in text
    assert "Nothing to add: the page lists 5 retired values." in text
    assert "Adding them" not in text


def test_a_page_that_cannot_be_read_exits_3_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, text = run(tmp_path, capsys, page=None)
    assert code == 3
    assert "**Could not be read:** https://docs.billing.test/deprecations.md answered 503" in text
    code, text = run(tmp_path, capsys, page="# Deprecations\n\nWe moved this page.\n")
    assert code == 3
    assert "no table of retired values was read: has the page changed?" in text
    assert script.main(["--only", "nobody"], sources=sources(tmp_path)) == 3
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    assert script.main([], sources=tmp_path / "bad.json") == 3
    assert script.main([], sources=sources(tmp_path, page="http://docs.billing.test/x")) == 3


def test_drafts_are_notices_the_catalog_takes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "drafts"
    report = tmp_path / "report.md"
    code, text = run(tmp_path, capsys, "--drafts", str(out), "--report", str(report))
    assert code == 1
    assert report.read_text(encoding="utf-8") == text
    names = sorted(path.name for path in out.iterdir())
    assert names == [
        "billing-model-retirements-2026-11-10.json",
        "billing-model-retirements-2026-12-01.json",
    ]
    ahead = json.loads((out / names[1]).read_text(encoding="utf-8"))
    parsed = parse_notice(ahead)
    assert parsed.effective_at is not None
    assert (parsed.published_at.date(), parsed.effective_at.date()) == (TODAY, date(2026, 12, 1))
    assert headline(parsed.summary) == "Billing shuts down 2 models on December 1, 2026."
    assert ahead["endpoints"] == [
        {
            "method": "POST",
            "url": "https://api.billing.test/v1/chat",
            "changes": [
                {
                    "path": "$.model",
                    "kind": "value_retired",
                    "before": "tilt-2",
                    "replaced_by": "tilt-3",
                    "note": (
                        "Shuts down on December 1, 2026: requests with this model will fail. "
                        "tilt-3 is the replacement Billing's page names."
                    ),
                },
                {
                    "path": "$.model",
                    "kind": "value_retired",
                    "before": "tilt-2-mini",
                    "note": "Shuts down on December 1, 2026: requests with this model will fail.",
                },
            ],
        }
    ]
    past = json.loads((out / names[0]).read_text(encoding="utf-8"))
    assert past["summary"].startswith(
        "Billing shut down 1 model on November 10, 2026. Requests that name it fail."
    )
    # scripts/catalog.py takes them as they are, and then the watch has nothing to add.
    target = catalog(tmp_path, "tilt-1")
    assert catalog_script.add(target, [out / name for name in names]) == [
        "billing-model-retirements-2026-11-10",
        "billing-model-retirements-2026-12-01",
    ]
    assert len(NoticeFeed.model_validate_json(target.read_bytes()).notices) == 3
    known = script.retired_in_catalog(target)["api.billing.test"]
    assert known == {"tilt-1", "tilt-2", "tilt-2-mini", "tilt-beta"}


def test_many_values_on_one_date_still_fit_a_notice() -> None:
    source = script.load_sources(SCRIPTS / "watch-sources.json")[0]
    names = [f"model-{n}-with-a-long-name-2026-01-01" for n in range(150)]
    retired = [script.Retirement(name, TODAY) for name in names]
    notice = script.draft(source, TODAY, retired, today=TODAY)
    assert len(parse_notice(notice).endpoints[0].changes) == 150
    assert len(notice["summary"]) < 500


def test_the_watched_sources_name_providers_the_catalog_knows() -> None:
    known = script.retired_in_catalog()
    watched = script.load_sources()
    assert [source.name for source in watched] == ["OpenAI", "Anthropic", "Groq"]
    for source in watched:
        assert known.get(source.provider), source.name  # the catalog retires values of theirs
        assert source.page.endswith(".md")


def test_the_report_is_marked_by_what_is_missing_not_by_the_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def mark(text: str) -> str:
        [line] = [line for line in text.splitlines() if script.MARK in line]
        assert line.startswith("<!-- catalog-watch:")
        assert line.endswith(" -->")
        return line

    _, first = run(tmp_path, capsys)
    later = script.main(
        [],
        catalog=catalog(tmp_path, "tilt-1"),
        sources=sources(tmp_path),
        fetch=lambda _: PAGE,
        today=date(2026, 11, 17),
    )
    week_after = capsys.readouterr().out
    assert later == 1
    assert "in 14 days" in week_after  # the text moved on
    assert mark(week_after) == mark(first)  # what is missing did not
    _, caught_up = run(tmp_path, capsys, known=("tilt-1", "tilt-2"))
    assert mark(caught_up) != mark(first)
    assert first.rstrip().endswith("-->")
