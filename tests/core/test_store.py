from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.helpers import explanation
from tiltmeter.core.models import Change, ChangeKind, CheckResult, Severity
from tiltmeter.core.shape import Shape
from tiltmeter.core.store import _NOTICES_SCHEMA, _SCHEMA, SCHEMA_VERSION, Store, StoreError


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


def test_runs_that_open_a_new_history_at_once_all_get_it(tmp_path: Path) -> None:
    path = tmp_path / "tiltmeter.db"
    start = threading.Barrier(8)
    errors: list[Exception] = []

    def open_store() -> None:
        start.wait()
        try:
            Store(path).close()
        except Exception as error:  # collected for the assertion below
            errors.append(error)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with Store(path) as db:
        assert db._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


class _LockedTwice:
    """Answers "database is locked" to the first two statements, as SQLite can when
    another run holds a lock while this one switches to WAL."""

    def __init__(self, error: str = "database is locked") -> None:
        self.error, self.calls = error, 0

    def execute(self, statement: str) -> None:
        self.calls += 1
        if self.calls <= 2:
            raise sqlite3.OperationalError(self.error)


def test_switching_to_wal_retries_while_another_run_holds_a_lock(tmp_path: Path) -> None:
    db = Store(tmp_path / "tiltmeter.db")
    real, locked = db._db, _LockedTwice()
    db._db = locked  # type: ignore[assignment]
    db._use_wal()
    assert locked.calls == 3
    db._db = _LockedTwice("disk I/O error")  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db._use_wal()  # only a lock is worth waiting for
    real.close()


def test_a_migration_that_fails_leaves_the_history_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "tiltmeter.db"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA + "CREATE TABLE spec_changes (x TEXT);")
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    with pytest.raises(sqlite3.OperationalError, match="spec_changes already exists"):
        Store(path)
    connection = sqlite3.connect(path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection.close()
    assert "notices" not in tables
    assert "subscriptions" not in tables


def test_notices_are_recorded_once_and_count_attempts(store: Store) -> None:
    assert store.notice_record("api.billing.test", "n1") is None
    first = store.record_notice(
        "api.billing.test",
        "n1",
        published_at="2026-11-03T12:00:00Z",
        status="failed",
        detail="overloaded",
        at="2026-11-03T13:00:00Z",
    )
    assert (first.status, first.attempts, first.done) == ("failed", 1, False)
    again = store.record_notice(
        "api.billing.test",
        "n1",
        published_at="2026-11-03T12:00:00Z",
        status="opened",
        detail="https://github.test/pr/2",
        at="2026-11-03T19:00:00Z",
    )
    assert (again.status, again.attempts, again.done) == ("opened", 2, True)
    assert again.detail == "https://github.test/pr/2"
    store.record_notice(
        "other.test", "n1", published_at="t", status="unaffected", detail=None, at="2026-11-04"
    )
    assert [r.provider for r in store.notice_records()] == ["other.test", "api.billing.test"]


def test_failed_notices_stop_after_three_attempts(store: Store) -> None:
    for _ in range(3):
        record = store.record_notice(
            "p.test", "n", published_at="t", status="blocked", detail=None, at="t"
        )
    assert (record.attempts, record.done) == (3, True)


def test_a_version_1_history_gains_the_new_tables(tmp_path: Path) -> None:
    path = tmp_path / "tiltmeter.db"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO checks (endpoint_id, checked_at, first_run, notes) VALUES ('a', 't', 1, '[]')"
    )
    connection.commit()
    connection.close()
    with Store(path) as db:
        assert len(db.list_checks()) == 1
        db.record_notice("p.test", "n", published_at="t", status="opened", detail=None, at="t")
        version = db._db.execute("PRAGMA user_version").fetchone()[0]
        assert db.remember_subscription("https://a.test/f", "t") == "t"
    assert version == SCHEMA_VERSION == 3


def test_a_version_2_development_history_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "tiltmeter.db"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA + _NOTICES_SCHEMA)
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    with Store(path) as db:
        db.save_spec_baseline("u", digest="d", version="1", fetched_at="t", body="{}")
        assert db.spec_baseline("u") is not None


def test_subscriptions_remember_when_they_started(store: Store) -> None:
    assert store.first_seen("https://a.test/feed") is None
    assert store.remember_subscription("https://a.test/feed", "2026-11-01T00:00:00Z") == (
        "2026-11-01T00:00:00Z"
    )
    assert store.remember_subscription("https://a.test/feed", "2026-12-01T00:00:00Z") == (
        "2026-11-01T00:00:00Z"
    )


def test_spec_baselines_and_frozen_changes(store: Store) -> None:
    url = "https://a.test/openapi.json"
    assert store.spec_baseline(url) is None
    body = json.dumps({"openapi": "3.1.0", "paths": {"/x": {}}} | {"pad": "x" * 10_000})
    store.save_spec_baseline(url, digest="d1", version="1.0", fetched_at="t1", body=body)
    first = store.spec_baseline(url)
    assert first is not None
    assert (first.digest, first.version, first.generation, first.body) == ("d1", "1.0", 1, body)
    stored = store._db.execute("SELECT length(body) FROM spec_baselines").fetchone()[0]
    assert stored < len(body) / 10  # compressed
    store.freeze_spec_change(
        url,
        provider="a.test",
        notice_id="spec-1",
        entry='{"id": "spec-1"}',
        digest="d2",
        version="1.1",
        fetched_at="t2",
        body="{}",
    )
    second = store.spec_baseline(url)
    assert second is not None
    assert (second.digest, second.generation) == ("d2", 2)
    [change] = store.spec_changes(url)
    assert (change.notice_id, change.entry, change.created_at) == (
        "spec-1",
        '{"id": "spec-1"}',
        "t2",
    )
    assert store.spec_changes("https://other.test/openapi.json") == []
