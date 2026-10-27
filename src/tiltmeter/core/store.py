"""SQLite history: checks, the changes they found, and the shapes they observed.

Shapes are stored once per distinct content (keyed by fingerprint), so an API
that keeps returning the same structure costs one row no matter how often it is
checked. The baseline of an endpoint is not stored: it is rebuilt on demand by
merging the shapes of its last ``window`` successful checks, which lets old
observations age out naturally.

The same file remembers what was done about each provider change notice, so a
scheduled run never handles one twice, and the last version of each followed spec,
so the next run can tell what changed in it.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from tiltmeter.core.models import (
    Baseline,
    Change,
    ChangeKind,
    CheckResult,
    Explanation,
    NoticeRecord,
    NoticeStatus,
    Severity,
    StoredChange,
)
from tiltmeter.core.shape import Shape, fingerprint_of

SCHEMA_VERSION: Final = 3
DB_FILENAME: Final = "tiltmeter.db"
BUSY_TIMEOUT_SECONDS: Final = 5

_SCHEMA: Final = """
CREATE TABLE shapes (
    fingerprint TEXT PRIMARY KEY,
    body        TEXT NOT NULL
);
CREATE TABLE checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id       TEXT NOT NULL,
    checked_at        TEXT NOT NULL,
    status_code       INTEGER,
    media_type        TEXT,
    latency_ms        REAL,
    shape_fingerprint TEXT REFERENCES shapes(fingerprint),
    first_run         INTEGER NOT NULL DEFAULT 0,
    max_severity      TEXT,
    error             TEXT,
    notes             TEXT NOT NULL DEFAULT '[]',
    explanation       TEXT,
    explanation_model TEXT,
    sample            TEXT
);
CREATE INDEX checks_by_endpoint ON checks(endpoint_id, id);
CREATE TABLE changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id    INTEGER NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    before      TEXT,
    after       TEXT,
    hint        TEXT
);
CREATE INDEX changes_by_check ON changes(check_id);
CREATE INDEX changes_by_endpoint ON changes(endpoint_id, detected_at);
"""

# Added in schema version 3 (version 2 only ever existed in development).
_NOTICES_SCHEMA: Final = """
CREATE TABLE notices (
    provider     TEXT NOT NULL,
    notice_id    TEXT NOT NULL,
    published_at TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (provider, notice_id)
);
"""

_SPECS_SCHEMA: Final = """
CREATE TABLE subscriptions (
    feed       TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);
CREATE TABLE spec_baselines (
    subscription TEXT PRIMARY KEY,
    digest       TEXT NOT NULL,
    version      TEXT,
    fetched_at   TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    body         BLOB NOT NULL
);
CREATE TABLE spec_changes (
    provider     TEXT NOT NULL,
    notice_id    TEXT NOT NULL,
    subscription TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    entry        TEXT NOT NULL,
    PRIMARY KEY (provider, notice_id)
);
"""


@dataclass(frozen=True, slots=True)
class SpecBaseline:
    """The version of a followed spec that the next run compares against."""

    subscription: str
    digest: str
    version: str | None
    fetched_at: str
    generation: int
    body: str
    url: str | None = None  # set only for a baseline read from elsewhere, not stored


@dataclass(frozen=True, slots=True)
class FrozenSpecChange:
    """A change found in a spec, kept as it was found until it is handled."""

    provider: str
    notice_id: str
    subscription: str
    created_at: str
    entry: str


class StoreError(Exception):
    """The history database cannot be used."""


class Store:
    """History of checks for every endpoint, in one SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        in_memory = self.path == ":memory:"
        if not in_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        if not in_memory:
            self._use_wal()
        self._migrate()

    def _use_wal(self) -> None:
        # Switching to WAL takes an exclusive lock, and SQLite reports "database is locked"
        # at once instead of waiting out busy_timeout when another run holds a lock: retry
        # for as long as the busy timeout would have waited.
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while True:
            try:
                self._db.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    @classmethod
    def in_dir(cls, state_dir: Path) -> Store:
        return cls(state_dir / DB_FILENAME)

    def _migrate(self) -> None:
        if self._schema_version() == SCHEMA_VERSION:
            return
        # Runs started together can open the same new file, so the version is read again
        # under the write lock. The statements run one by one in that transaction
        # (executescript would commit first), so a migration applies whole or not at all.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            version = self._schema_version()
            if version > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path} was written by a newer Tiltmeter (schema {version}); "
                    "upgrade Tiltmeter to read it"
                )
            if version < SCHEMA_VERSION:
                script = _SCHEMA if version == 0 else ""
                if version < 2:
                    script += _NOTICES_SCHEMA
                script += _SPECS_SCHEMA
                for statement in script.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- writing --------------------------------------------------------------

    def save_check(
        self, result: CheckResult, *, shape: Shape | None = None, sample: str | None = None
    ) -> int:
        """Record a check, its changes and its observed shape. Returns the check id."""
        fingerprint: str | None = None
        with self._db:
            if shape is not None:
                body = shape.to_json()
                fingerprint = fingerprint_of(body)
                self._db.execute(
                    "INSERT OR IGNORE INTO shapes (fingerprint, body) VALUES (?, ?)",
                    (fingerprint, body),
                )
            cursor = self._db.execute(
                """
                INSERT INTO checks (
                    endpoint_id, checked_at, status_code, media_type, latency_ms,
                    shape_fingerprint, first_run, max_severity, error, notes,
                    explanation, explanation_model, sample
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.endpoint_id,
                    result.checked_at,
                    result.status_code,
                    result.media_type,
                    result.latency_ms,
                    fingerprint,
                    int(result.first_run),
                    result.max_severity.value if result.max_severity else None,
                    result.error,
                    json.dumps(result.notes),
                    result.explanation.model_dump_json() if result.explanation else None,
                    result.explanation_model,
                    sample,
                ),
            )
            check_id = cursor.lastrowid
            if check_id is None:  # pragma: no cover - sqlite always sets it for INSERT
                raise StoreError("could not record the check")
            self._db.executemany(
                """
                INSERT INTO changes (
                    check_id, endpoint_id, detected_at, path, kind, severity, before, after, hint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        check_id,
                        result.endpoint_id,
                        result.checked_at,
                        change.path,
                        change.kind.value,
                        change.severity.value,
                        change.before,
                        change.after,
                        change.hint,
                    )
                    for change in result.changes
                ],
            )
        return check_id

    def update_explanation(self, check_id: int, explanation: Explanation, model: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE checks SET explanation = ?, explanation_model = ? WHERE id = ?",
                (explanation.model_dump_json(), model, check_id),
            )

    def append_notes(self, check_id: int, notes: Sequence[str]) -> None:
        if not notes:
            return
        with self._db:
            row = self._db.execute("SELECT notes FROM checks WHERE id = ?", (check_id,)).fetchone()
            if row is None:
                return
            merged = [*json.loads(row["notes"]), *notes]
            self._db.execute(
                "UPDATE checks SET notes = ? WHERE id = ?", (json.dumps(merged), check_id)
            )

    def prune(self, endpoint_id: str, *, keep: int) -> int:
        """Keep the newest ``keep`` checks of an endpoint. Returns how many were deleted."""
        with self._db:
            cursor = self._db.execute(
                """
                DELETE FROM checks
                WHERE endpoint_id = ? AND id NOT IN (
                    SELECT id FROM checks WHERE endpoint_id = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (endpoint_id, endpoint_id, keep),
            )
            if cursor.rowcount:
                self._db.execute(
                    """
                    DELETE FROM shapes WHERE fingerprint NOT IN (
                        SELECT shape_fingerprint FROM checks WHERE shape_fingerprint IS NOT NULL
                    )
                    """
                )
        return cursor.rowcount

    def record_notice(
        self,
        provider: str,
        notice_id: str,
        *,
        published_at: str,
        status: NoticeStatus,
        detail: str | None,
        at: str,
    ) -> NoticeRecord:
        """Remember what was done about a notice. Recording it again counts an attempt."""
        with self._db:
            self._db.execute(
                """
                INSERT INTO notices (
                    provider, notice_id, published_at, status, detail, attempts, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (provider, notice_id) DO UPDATE SET
                    published_at = excluded.published_at,
                    status = excluded.status,
                    detail = excluded.detail,
                    attempts = notices.attempts + 1,
                    updated_at = excluded.updated_at
                """,
                (provider, notice_id, published_at, status, detail, at),
            )
        record = self.notice_record(provider, notice_id)
        if record is None:  # pragma: no cover - the row was just written
            raise StoreError("could not record the notice")
        return record

    def remember_subscription(self, feed: str, at: str) -> str:
        """When the feed was first followed, recording ``at`` the first time."""
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO subscriptions (feed, first_seen) VALUES (?, ?)", (feed, at)
            )
        return self.first_seen(feed) or at

    def save_spec_baseline(
        self, subscription: str, *, digest: str, version: str | None, fetched_at: str, body: str
    ) -> None:
        with self._db:
            self._save_spec_baseline(subscription, digest, version, fetched_at, body)

    def freeze_spec_change(
        self,
        subscription: str,
        *,
        provider: str,
        notice_id: str,
        entry: str,
        digest: str,
        version: str | None,
        fetched_at: str,
        body: str,
    ) -> None:
        """Keep a change found in a spec and move the baseline past it, together."""
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO spec_changes (
                    provider, notice_id, subscription, created_at, entry
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (provider, notice_id, subscription, fetched_at, entry),
            )
            self._save_spec_baseline(subscription, digest, version, fetched_at, body)

    def _save_spec_baseline(
        self, subscription: str, digest: str, version: str | None, fetched_at: str, body: str
    ) -> None:
        self._db.execute(
            """
            INSERT INTO spec_baselines (
                subscription, digest, version, fetched_at, generation, body
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT (subscription) DO UPDATE SET
                digest = excluded.digest,
                version = excluded.version,
                fetched_at = excluded.fetched_at,
                generation = spec_baselines.generation + 1,
                body = excluded.body
            """,
            (subscription, digest, version, fetched_at, zlib.compress(body.encode("utf-8"))),
        )

    # -- reading --------------------------------------------------------------

    def load_baseline(
        self, endpoint_id: str, *, window: int, before_check_id: int | None = None
    ) -> Baseline:
        """Merge the shapes of the last ``window`` successful checks into a baseline.

        ``before_check_id`` rebuilds the baseline as it was right before that check.
        """
        bound = before_check_id if before_check_id is not None else -1
        rows = self._db.execute(
            """
            SELECT c.media_type, c.latency_ms, c.shape_fingerprint, s.body
            FROM checks c JOIN shapes s ON s.fingerprint = c.shape_fingerprint
            WHERE c.endpoint_id = ? AND (? < 0 OR c.id < ?)
            ORDER BY c.id DESC LIMIT ?
            """,
            (endpoint_id, bound, bound, window),
        ).fetchall()
        parsed: dict[str, Shape] = {}
        shapes: list[Shape] = []
        for row in rows:
            fingerprint = row["shape_fingerprint"]
            if fingerprint not in parsed:
                parsed[fingerprint] = Shape.from_json(row["body"])
            shapes.append(parsed[fingerprint])
        status_row = self._db.execute(
            """
            SELECT status_code FROM checks
            WHERE endpoint_id = ? AND status_code IS NOT NULL AND (? < 0 OR id < ?)
            ORDER BY id DESC LIMIT 1
            """,
            (endpoint_id, bound, bound),
        ).fetchone()
        latencies = [float(row["latency_ms"]) for row in rows if row["latency_ms"] is not None]
        return Baseline(
            shape=Shape.merge_all(shapes),
            status_code=int(status_row["status_code"]) if status_row else None,
            media_type=rows[0]["media_type"] if rows else None,
            latency_ms=statistics.median(latencies) if latencies else None,
            latency_points=len(latencies),
            checks=len(rows),
        )

    def check_shape(self, check_id: int) -> Shape | None:
        """The shape observed by one check, when it got a JSON response."""
        row = self._db.execute(
            """
            SELECT s.body FROM checks c JOIN shapes s ON s.fingerprint = c.shape_fingerprint
            WHERE c.id = ?
            """,
            (check_id,),
        ).fetchone()
        return Shape.from_json(row["body"]) if row else None

    def get_check(self, check_id: int) -> CheckResult | None:
        row = self._db.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
        return self._results([row])[0] if row else None

    def latest_check(self, endpoint_id: str) -> CheckResult | None:
        row = self._db.execute(
            "SELECT * FROM checks WHERE endpoint_id = ? ORDER BY id DESC LIMIT 1", (endpoint_id,)
        ).fetchone()
        return self._results([row])[0] if row else None

    def list_checks(self, endpoint_id: str | None = None, *, limit: int = 20) -> list[CheckResult]:
        """Most recent checks first."""
        if endpoint_id is None:
            rows = self._db.execute(
                "SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM checks WHERE endpoint_id = ? ORDER BY id DESC LIMIT ?",
                (endpoint_id, limit),
            ).fetchall()
        return self._results(rows)

    def list_changes(
        self,
        endpoint_id: str | None = None,
        *,
        since: str | None = None,
        min_severity: Severity | None = None,
        limit: int = 100,
    ) -> list[StoredChange]:
        """Most recent changes first, optionally filtered."""
        clauses: list[str] = []
        params: list[Any] = []
        if endpoint_id is not None:
            clauses.append("endpoint_id = ?")
            params.append(endpoint_id)
        if since is not None:
            clauses.append("detected_at >= ?")
            params.append(since)
        if min_severity is not None:
            allowed = [s.value for s in Severity if s.at_least(min_severity)]
            clauses.append(f"severity IN ({', '.join('?' for _ in allowed)})")
            params.extend(allowed)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.execute(
            f"SELECT * FROM changes {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        ).fetchall()
        return [
            StoredChange(
                id=row["id"],
                check_id=row["check_id"],
                endpoint_id=row["endpoint_id"],
                detected_at=row["detected_at"],
                path=row["path"],
                kind=ChangeKind(row["kind"]),
                severity=Severity(row["severity"]),
                before=row["before"],
                after=row["after"],
                hint=row["hint"],
            )
            for row in rows
        ]

    def notice_record(self, provider: str, notice_id: str) -> NoticeRecord | None:
        row = self._db.execute(
            "SELECT * FROM notices WHERE provider = ? AND notice_id = ?", (provider, notice_id)
        ).fetchone()
        return _notice_record(row) if row else None

    def first_seen(self, feed: str) -> str | None:
        row = self._db.execute(
            "SELECT first_seen FROM subscriptions WHERE feed = ?", (feed,)
        ).fetchone()
        return str(row["first_seen"]) if row else None

    def spec_baseline(self, subscription: str) -> SpecBaseline | None:
        row = self._db.execute(
            "SELECT * FROM spec_baselines WHERE subscription = ?", (subscription,)
        ).fetchone()
        if row is None:
            return None
        return SpecBaseline(
            subscription=row["subscription"],
            digest=row["digest"],
            version=row["version"],
            fetched_at=row["fetched_at"],
            generation=int(row["generation"]),
            body=zlib.decompress(row["body"]).decode("utf-8"),
        )

    def spec_changes(self, subscription: str) -> list[FrozenSpecChange]:
        """The changes found in a spec, oldest first, handled or not."""
        rows = self._db.execute(
            "SELECT * FROM spec_changes WHERE subscription = ? ORDER BY created_at, rowid",
            (subscription,),
        )
        return [
            FrozenSpecChange(
                provider=row["provider"],
                notice_id=row["notice_id"],
                subscription=row["subscription"],
                created_at=row["created_at"],
                entry=row["entry"],
            )
            for row in rows
        ]

    def notice_records(self) -> list[NoticeRecord]:
        """Every notice handled so far, most recently updated first."""
        rows = self._db.execute("SELECT * FROM notices ORDER BY updated_at DESC, rowid DESC")
        return [_notice_record(row) for row in rows]

    def endpoint_ids(self) -> list[str]:
        """Every endpoint that has history, alphabetically."""
        rows = self._db.execute("SELECT DISTINCT endpoint_id FROM checks ORDER BY endpoint_id")
        return [row["endpoint_id"] for row in rows]

    def _results(self, rows: Iterable[sqlite3.Row]) -> list[CheckResult]:
        rows = list(rows)
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ", ".join("?" for _ in ids)
        changes: dict[int, list[Change]] = {check_id: [] for check_id in ids}
        for row in self._db.execute(
            f"SELECT * FROM changes WHERE check_id IN ({placeholders}) ORDER BY id", ids
        ):
            changes[row["check_id"]].append(
                Change(
                    path=row["path"],
                    kind=ChangeKind(row["kind"]),
                    severity=Severity(row["severity"]),
                    before=row["before"],
                    after=row["after"],
                    hint=row["hint"],
                )
            )
        results = []
        for row in rows:
            explanation = row["explanation"]
            results.append(
                CheckResult(
                    endpoint_id=row["endpoint_id"],
                    check_id=row["id"],
                    checked_at=row["checked_at"],
                    status_code=row["status_code"],
                    media_type=row["media_type"],
                    latency_ms=row["latency_ms"],
                    first_run=bool(row["first_run"]),
                    changes=changes[row["id"]],
                    max_severity=Severity(row["max_severity"]) if row["max_severity"] else None,
                    explanation=Explanation.model_validate_json(explanation)
                    if explanation
                    else None,
                    explanation_model=row["explanation_model"],
                    error=row["error"],
                    notes=json.loads(row["notes"]),
                )
            )
        return results


def _notice_record(row: sqlite3.Row) -> NoticeRecord:
    return NoticeRecord(
        provider=row["provider"],
        notice_id=row["notice_id"],
        published_at=row["published_at"],
        status=row["status"],
        detail=row["detail"],
        attempts=row["attempts"],
        updated_at=row["updated_at"],
    )
