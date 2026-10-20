"""SQLite history: checks, the changes they found, and the shapes they observed.

Shapes are stored once per distinct content (keyed by fingerprint), so an API
that keeps returning the same structure costs one row no matter how often it is
checked. The baseline of an endpoint is not stored: it is rebuilt on demand by
merging the shapes of its last ``window`` successful checks, which lets old
observations age out naturally.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from driftwatch.core.models import (
    Baseline,
    Change,
    ChangeKind,
    CheckResult,
    Explanation,
    Severity,
    StoredChange,
)
from driftwatch.core.shape import Shape, fingerprint_of

SCHEMA_VERSION: Final = 1
DB_FILENAME: Final = "driftwatch.db"

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
        self._db.execute("PRAGMA busy_timeout = 5000")
        if not in_memory:
            self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    @classmethod
    def in_dir(cls, state_dir: Path) -> Store:
        return cls(state_dir / DB_FILENAME)

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written by a newer driftwatch (schema {version}); "
                "upgrade driftwatch to read it"
            )
        if version == 0:
            with self._db:
                self._db.executescript(_SCHEMA)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
