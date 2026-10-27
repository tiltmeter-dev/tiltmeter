from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from tests.notices.kit import notice, signed
from tiltmeter.notices.models import NoticeFeed

SCRIPT = Path(__file__).parent.parent / "scripts" / "catalog.py"
DRAFTED = (
    "Drafted by Tiltmeter from Billing's public documentation; not signed or endorsed by Billing."
)


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("catalog_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = load_script()


def draft(path: Path, **overrides: object) -> Path:
    data = notice(summary=f"Customers now return phone_number. {DRAFTED}", **overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_entries_are_added_newest_first_and_replaced_by_id(tmp_path: Path) -> None:
    catalog = tmp_path / "api-changes.json"
    old = draft(tmp_path / "old.json", id="old", published_at="2025-01-01T00:00:00Z")
    new = draft(tmp_path / "new.json", id="new")
    assert script.main([str(old), str(new)], catalog=catalog) == 0
    entries = NoticeFeed.model_validate_json(catalog.read_text(encoding="utf-8")).notices
    assert [entry.id for entry in entries] == ["new", "old"]
    again = draft(tmp_path / "again.json", id="old", published_at="2026-12-01T00:00:00Z")
    assert script.main([str(again)], catalog=catalog) == 0
    entries = NoticeFeed.model_validate_json(catalog.read_text(encoding="utf-8")).notices
    assert [entry.id for entry in entries] == ["old", "new"]  # replaced, and newer now


def test_signed_notices_and_summaries_without_the_drafted_sentence_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "api-changes.json"
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps(notice()), encoding="utf-8")
    assert script.main([str(plain)], catalog=catalog) == 1
    assert "the summary must end with" in capsys.readouterr().err
    sealed = tmp_path / "signed.json"
    sealed.write_text(json.dumps(signed()), encoding="utf-8")
    assert script.main([str(sealed)], catalog=catalog) == 1
    assert "is signed" in capsys.readouterr().err
    assert not catalog.exists()
