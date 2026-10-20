from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from driftwatch.core.analyze import analyze
from driftwatch.core.diff import diff_shapes
from driftwatch.core.models import Baseline, ChangeKind, Observation
from driftwatch.core.paths import PathMatcher
from driftwatch.core.shape import Shape

PAIRS = Path(__file__).parent.parent / "fixtures" / "pairs"


def load(case: str, name: str) -> Any:
    return json.loads((PAIRS / case / name).read_text())


@pytest.mark.parametrize("case", sorted(p.name for p in PAIRS.iterdir() if p.is_dir()))
def test_golden_pairs(case: str) -> None:
    baseline = Baseline(
        shape=Shape.from_samples([load(case, "before.json")]),
        status_code=200,
        media_type="application/json",
    )
    observation = Observation(
        status_code=200,
        media_type="application/json",
        latency_ms=10.0,
        json_ok=True,
        shape=Shape.from_samples([load(case, "after.json")]),
        samples=1,
    )
    analysis = analyze(baseline, observation)
    got = [
        {"path": c.path, "kind": c.kind.value, "severity": c.severity.value, "hint": c.hint}
        for c in analysis.changes
    ]
    expected = [{"hint": None, **item} for item in load(case, "expected.json")]
    assert got == expected
    assert analysis.error is None
    assert not analysis.first_run


def kinds(baseline: Shape, observed: Shape, **kwargs: Any) -> list[tuple[str, ChangeKind]]:
    return [(c.path, c.kind) for c in diff_shapes(baseline, observed, **kwargs)]


def test_absent_optional_field_is_not_a_change() -> None:
    baseline = Shape.from_samples([{"id": 1, "nick": "a"}, {"id": 2}])
    assert kinds(baseline, Shape.from_samples([{"id": 3}])) == []


def test_null_that_was_seen_before_is_not_a_change() -> None:
    baseline = Shape.from_samples([{"x": "a"}, {"x": None}])
    assert kinds(baseline, Shape.from_samples([{"x": None}])) == []
    assert kinds(baseline, Shape.from_samples([{"x": "b"}])) == []


def test_type_change_and_null_reported_together() -> None:
    baseline = Shape.from_samples([{"x": "a"}])
    observed = Shape.from_samples([{"x": 1}, {"x": None}])
    assert kinds(baseline, observed) == [
        ("$.x", ChangeKind.NULLABLE_INTRODUCED),
        ("$.x", ChangeKind.TYPE_CHANGED),
    ]


def test_new_subtree_is_reported_once() -> None:
    baseline = Shape.from_samples([{"id": 1}])
    observed = Shape.from_samples([{"id": 1, "meta": {"a": 1, "b": {"c": 2}}}])
    assert kinds(baseline, observed) == [("$.meta", ChangeKind.FIELD_ADDED)]


def test_ignore_patterns_skip_subtrees() -> None:
    baseline = Shape.from_samples([{"id": 1, "metadata": {"a": 1}, "rows": [{"updated_at": "x"}]}])
    observed = Shape.from_samples([{"id": "1", "metadata": {"b": 2}, "rows": [{}]}])
    assert kinds(baseline, observed) == [
        ("$.id", ChangeKind.TYPE_CHANGED),
        ("$.metadata.a", ChangeKind.FIELD_REMOVED),
        ("$.metadata.b", ChangeKind.FIELD_ADDED),
        ("$.rows[*].updated_at", ChangeKind.FIELD_REMOVED),
    ]
    ignore = PathMatcher(["$.metadata", "$..updated_at"])
    assert kinds(baseline, observed, ignore=ignore) == [("$.id", ChangeKind.TYPE_CHANGED)]
    assert kinds(baseline, observed, ignore=PathMatcher(["$"])) == []


def enum_baseline() -> Shape:
    return Shape.from_samples([{"s": v} for v in ["open", "closed"] * 6])


def test_enum_value_added_lists_new_values() -> None:
    [change] = diff_shapes(enum_baseline(), Shape.from_samples([{"s": "merged"}, {"s": "open"}]))
    assert change.kind is ChangeKind.ENUM_VALUE_ADDED
    assert change.before == "closed, open"
    assert change.after == "merged"


def test_enum_value_that_looks_like_free_text_is_reported_without_the_value() -> None:
    [change] = diff_shapes(enum_baseline(), Shape.from_samples([{"s": "Waiting For Review"}]))
    assert change.kind is ChangeKind.ENUM_VALUE_ADDED
    assert "Waiting" not in (change.after or "")


def test_enum_flooded_with_values() -> None:
    observed = Shape.from_samples([{"s": f"v{i}"} for i in range(40)])
    [change] = diff_shapes(enum_baseline(), observed)
    assert "more than" in (change.after or "")


def test_many_new_enum_values_are_truncated() -> None:
    observed = Shape.from_samples([{"s": f"new{i}"} for i in range(12)])
    [change] = diff_shapes(enum_baseline(), observed)
    assert change.after is not None
    assert change.after.endswith("(+4 more)")


def test_known_enum_values_and_non_string_values_are_quiet() -> None:
    assert diff_shapes(enum_baseline(), Shape.from_samples([{"s": "open"}])) == []
    [change] = diff_shapes(enum_baseline(), Shape.from_samples([{"s": 3}]))
    assert change.kind is ChangeKind.TYPE_CHANGED


def test_rename_hints_pair_similar_names() -> None:
    baseline = Shape.from_samples([{"first_name": "a", "last_name": "b", "id": 1}])
    observed = Shape.from_samples([{"firstName": "a", "lastName": "b", "id": 1}])
    hints = {c.path: c.hint for c in diff_shapes(baseline, observed)}
    assert hints == {
        "$.first_name": "possibly renamed to $.firstName",
        "$.last_name": "possibly renamed to $.lastName",
        "$.firstName": "possibly replaces $.first_name",
        "$.lastName": "possibly replaces $.last_name",
    }


def test_rename_hint_for_a_lone_pair_even_with_different_names() -> None:
    baseline = Shape.from_samples([{"id": 1, "tel": "x"}])
    observed = Shape.from_samples([{"id": 1, "contact": "x"}])
    hints = [c.hint for c in diff_shapes(baseline, observed)]
    assert hints == ["possibly replaces $.tel", "possibly renamed to $.contact"]


def test_no_rename_hint_across_types_parents_or_ambiguous_names() -> None:
    different_type = diff_shapes(
        Shape.from_samples([{"count": 1}]), Shape.from_samples([{"total": "1"}])
    )
    assert all(c.hint is None for c in different_type)
    different_parent = diff_shapes(
        Shape.from_samples([{"a": {"x": 1}, "b": {}}]),
        Shape.from_samples([{"a": {}, "b": {"x": 1}}]),
    )
    assert all(c.hint is None for c in different_parent)
    ambiguous = diff_shapes(
        Shape.from_samples([{"alpha": 1, "beta": 2}]),
        Shape.from_samples([{"gamma": 1, "delta": 2}]),
    )
    assert all(c.hint is None for c in ambiguous)


def test_integer_observed_against_number_baseline_is_fine() -> None:
    assert kinds(Shape.from_samples([{"x": 1.5}]), Shape.from_samples([{"x": 2}])) == []


def test_rename_hints_for_suffixed_names_under_one_parent() -> None:
    baseline = Shape.from_samples([{"phone": "1", "email": "a", "id": 1}])
    observed = Shape.from_samples([{"phone_number": "1", "email_address": "a", "id": 1}])
    hints = {c.path: c.hint for c in diff_shapes(baseline, observed) if c.hint}
    assert hints["$.phone"] == "possibly renamed to $.phone_number"
    assert hints["$.email"] == "possibly renamed to $.email_address"
