from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.helpers import explanation
from tiltmeter.core.models import Change, ChangeKind, CheckResult, Severity
from tiltmeter.core.shape import Shape
from tiltmeter.core.store import Store, StoreError


def result(
    endpoint: str = "items", at: str = "2026-10-20T12:00:00Z", **extra: object
) -> CheckResult:
    data: dict[str, object] = {
        "endpoint_id": endpoint,
        "checked_at": at,
        "status_code": 200,
        "media_type": "application/json",
        "latency_ms": 100.0,
    }
    data.update(extra)
    return CheckResult.model_validate(data)


def change(kind: ChangeKind, severity: Severity, path: str = "$.a") -> Change:
    return Change(path=path, kind=kind, severity=severity, before="string", after=None)


def test_round_trip(store: Store) -> None:
    saved = result(
        changes=[change(ChangeKind.FIELD_REMOVED, Severity.BREAKING, "$.phone")],
        max_severity=Severity.BREAKING,
        explanation=explanation(),
        explanation_model="fake-model",
        notes=["one"],
    )
    check_id = store.save_check(saved, shape=Shape.from_samples([{"a": 1}]))
    loaded = store.get_check(check_id)
    assert loaded is not None
    assert loaded.check_id == check_id
    assert loaded.model_dump(exclude={"check_id"}) == saved.model_dump(exclude={"check_id"})
    assert store.get_check(999) is None
    assert store.check_shape(check_id) is not None
    assert store.latest_check("items") == loaded
    assert store.latest_check("other") is None


def test_identical_shapes_are_stored_once(store: Store) -> None:
    shape = Shape.from_samples([{"a": 1}])
    for minute in range(5):
        store.save_check(result(at=f"2026-10-20T12:0{minute}:00Z"), shape=shape)
    count = store._db.execute("SELECT COUNT(*) FROM shapes").fetchone()[0]
    assert count == 1


def test_baseline_merges_the_recent_window(store: Store) -> None:
    store.save_check(result(latency_ms=50.0), shape=Shape.from_samples([{"old": 1}]))
    for latency in (100.0, 200.0, 300.0):
        store.save_check(result(latency_ms=latency), shape=Shape.from_samples([{"a": 1}]))
    baseline = store.load_baseline("items", window=3)
    assert baseline.checks == 3
    assert baseline.shape is not None
    assert "old" not in baseline.shape.properties  # aged out of the window
    assert baseline.shape.count == 3
    assert baseline.latency_ms == 200.0
    assert baseline.latency_points == 3
    assert baseline.media_type == "application/json"
    assert baseline.status_code == 200
    wide = store.load_baseline("items", window=10)
    assert wide.shape is not None
    assert not wide.shape.is_required("old")


def test_baseline_status_comes_from_the_latest_response(store: Store) -> None:
    store.save_check(result(), shape=Shape.from_samples([{"a": 1}]))
    store.save_check(result(status_code=503, media_type="text/html"))
    store.save_check(result(status_code=None, error="timed out"))
    baseline = store.load_baseline("items", window=20)
    assert baseline.status_code == 503
    assert baseline.checks == 1
    assert store.load_baseline("unknown", window=20) == type(baseline)()


def test_baseline_before_a_given_check(store: Store) -> None:
    first = store.save_check(result(), shape=Shape.from_samples([{"a": 1}]))
    second = store.save_check(result(), shape=Shape.from_samples([{"b": 1}]))
    before_second = store.load_baseline("items", window=20, before_check_id=second)
    assert before_second.shape is not None
    assert set(before_second.shape.properties) == {"a"}
    assert store.load_baseline("items", window=20, before_check_id=first).shape is None


def test_list_changes_filters(store: Store) -> None:
    store.save_check(
        result(at="2026-10-01T00:00:00Z", changes=[change(ChangeKind.FIELD_ADDED, Severity.INFO)])
    )
    store.save_check(
        result(
            at="2026-10-10T00:00:00Z",
            changes=[
                change(ChangeKind.FIELD_REMOVED, Severity.BREAKING, "$.x"),
                change(ChangeKind.ENUM_VALUE_ADDED, Severity.WARNING, "$.y"),
            ],
        )
    )
    store.save_check(result("other", changes=[change(ChangeKind.TYPE_CHANGED, Severity.BREAKING)]))
    assert [c.path for c in store.list_changes("items")] == ["$.y", "$.x", "$.a"]
    assert [c.path for c in store.list_changes("items", since="2026-10-05T00:00:00Z")] == [
        "$.y",
        "$.x",
    ]
    serious = store.list_changes("items", min_severity=Severity.BREAKING)
    assert [c.path for c in serious] == ["$.x"]
    assert serious[0].endpoint_id == "items"
    assert serious[0].message == "Field $.x (string) is no longer returned."
    assert len(store.list_changes(limit=2)) == 2
    assert len(store.list_changes()) == 4
    assert store.endpoint_ids() == ["items", "other"]


def test_list_checks(store: Store) -> None:
    for minute in range(3):
        store.save_check(result(at=f"2026-10-20T12:0{minute}:00Z"))
    store.save_check(result("other"))
    assert [c.checked_at[-5:-1] for c in store.list_checks("items", limit=2)] == ["2:00", "1:00"]
    assert len(store.list_checks()) == 4


def test_prune_keeps_the_newest_and_cleans_up(store: Store) -> None:
    for i in range(5):
        store.save_check(
            result(changes=[change(ChangeKind.FIELD_ADDED, Severity.INFO)]),
            shape=Shape.from_samples([{f"f{i}": 1}]),
        )
    store.save_check(result("other"), shape=Shape.from_samples([{"keep": 1}]))
    assert store.prune("items", keep=2) == 3
    assert len(store.list_checks("items")) == 2
    assert len(store.list_changes("items")) == 2
    shapes = store._db.execute("SELECT COUNT(*) FROM shapes").fetchone()[0]
    assert shapes == 3  # two for items, one for other
    assert store.prune("items", keep=2) == 0


def test_notes_and_explanations_can_be_added_later(store: Store) -> None:
    check_id = store.save_check(result())
    store.append_notes(check_id, ["slack failed"])
    store.append_notes(check_id, [])
    store.append_notes(12345, ["ignored"])
    store.update_explanation(check_id, explanation(summary="later"), "fake-model")
    loaded = store.get_check(check_id)
    assert loaded is not None
    assert loaded.notes == ["slack failed"]
    assert loaded.explanation is not None
    assert loaded.explanation.summary == "later"
    assert loaded.explanation_model == "fake-model"


def test_file_store_creates_its_folder_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "state" / "nested" / "tiltmeter.db"
    with Store(path) as db:
        db.save_check(result())
    with Store(path) as db:
        assert len(db.list_checks()) == 1
    assert Store.in_dir(tmp_path / "x").path.endswith("tiltmeter.db")


def test_refuses_a_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "tiltmeter.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(StoreError, match="newer Tiltmeter"):
        Store(path)
