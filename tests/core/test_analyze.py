from __future__ import annotations

from tiltmeter.core.analyze import Rules, analyze
from tiltmeter.core.models import Baseline, ChangeKind, Observation, Severity
from tiltmeter.core.paths import PathMatcher
from tiltmeter.core.shape import Shape

JSON = "application/json"


def ok(body: object, latency: float = 100.0) -> Observation:
    return Observation(
        status_code=200,
        media_type=JSON,
        latency_ms=latency,
        json_ok=True,
        shape=Shape.from_samples([body]),
        samples=1,
    )


def known(body: object, *, status: int = 200, latency: float | None = 100.0) -> Baseline:
    return Baseline(
        shape=Shape.from_samples([body]),
        status_code=status,
        media_type=JSON,
        latency_ms=latency,
        latency_points=5,
        checks=5,
    )


def test_fetch_errors_pass_through() -> None:
    analysis = analyze(known({}), Observation(error="timed out after 10s"))
    assert analysis.error == "timed out after 10s"
    assert analysis.changes == []
    assert analysis.max_severity is None


def test_first_run_records_a_baseline() -> None:
    analysis = analyze(Baseline(), ok({"id": 1}))
    assert analysis.first_run
    assert analysis.changes == []
    assert analysis.error is None


def test_status_drop_is_breaking_and_skips_the_body() -> None:
    analysis = analyze(known({"id": 1}), Observation(status_code=503, media_type="text/html"))
    [change] = analysis.changes
    assert change.kind is ChangeKind.STATUS_CHANGED
    assert (change.before, change.after) == ("200", "503")
    assert analysis.max_severity is Severity.BREAKING
    assert analysis.error is None


def test_unexpected_status_without_history_is_an_error() -> None:
    assert analyze(Baseline(), Observation(status_code=404)).error == "unexpected HTTP status 404"
    persistent = analyze(known({}, status=404), Observation(status_code=404))
    assert persistent.error == "unexpected HTTP status 404"
    assert persistent.changes == []


def test_recovery_is_info_and_the_body_is_compared_again() -> None:
    baseline = Baseline(shape=Shape.from_samples([{"id": 1}]), status_code=404, media_type=JSON)
    analysis = analyze(baseline, ok({"id": 1, "new": True}))
    assert [(c.kind, c.severity) for c in analysis.changes] == [
        (ChangeKind.STATUS_CHANGED, Severity.INFO),
        (ChangeKind.FIELD_ADDED, Severity.INFO),
    ]


def test_body_that_stops_being_json() -> None:
    html = Observation(status_code=200, media_type="text/html", latency_ms=50.0, json_ok=False)
    [change] = analyze(known({"id": 1}), html).changes
    assert change.kind is ChangeKind.CONTENT_TYPE_CHANGED
    assert change.after == "text/html (not valid JSON)"
    assert change.severity is Severity.BREAKING
    assert analyze(Baseline(), html).error == "the response body is not valid JSON"


def test_latency_regression() -> None:
    slow = analyze(known({"id": 1}, latency=100.0), ok({"id": 1}, latency=500.0))
    [change] = slow.changes
    assert change.kind is ChangeKind.LATENCY_REGRESSION
    assert (change.before, change.after) == ("100 ms", "500 ms")
    assert analyze(known({"id": 1}), ok({"id": 1}, latency=280.0)).changes == []
    tiny = analyze(known({"id": 1}, latency=10.0), ok({"id": 1}, latency=100.0))
    assert tiny.changes == []  # 10x slower but only +90 ms
    sparse = Baseline(
        shape=Shape.from_samples([{"id": 1}]), status_code=200, latency_ms=100.0, latency_points=2
    )
    assert analyze(sparse, ok({"id": 1}, latency=900.0)).changes == []
    off = analyze(known({"id": 1}), ok({"id": 1}, latency=900.0), Rules(latency_factor=None))
    assert off.changes == []


def test_rules_are_applied_and_changes_are_sorted_by_severity() -> None:
    baseline = known({"a": 1, "b": 1})
    observation = ok({"b": 1, "c": 1})
    default = analyze(baseline, observation)
    assert [c.kind for c in default.changes] == [ChangeKind.FIELD_REMOVED, ChangeKind.FIELD_ADDED]
    rules = Rules(
        strict=True,
        overrides={ChangeKind.FIELD_REMOVED: Severity.INFO},
        ignore=PathMatcher(["$.zzz"]),
    )
    tuned = analyze(baseline, observation, rules)
    assert [(c.kind, c.severity) for c in tuned.changes] == [
        (ChangeKind.FIELD_ADDED, Severity.WARNING),
        (ChangeKind.FIELD_REMOVED, Severity.INFO),
    ]
    assert tuned.max_severity is Severity.WARNING
