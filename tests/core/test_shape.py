from __future__ import annotations

import json

import pytest

from tiltmeter.core.paths import ITEMS
from tiltmeter.core.shape import (
    MAX_TRACKED_VALUES,
    Shape,
    core_types,
    json_type,
    to_json_schema,
    type_label,
)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (None, "null"),
        (True, "boolean"),
        (0, "integer"),
        (1.5, "number"),
        ("x", "string"),
        ([], "array"),
        ({}, "object"),
    ],
)
def test_json_type(value: object, kind: str) -> None:
    assert json_type(value) == kind


def test_json_type_rejects_non_json() -> None:
    with pytest.raises(TypeError):
        json_type({1, 2})


def test_labels() -> None:
    assert type_label(["null", "string"]) == "string | null"
    assert type_label(["integer", "number"]) == "number"
    assert type_label([]) == "nothing"
    assert core_types(["integer", "null"]) == frozenset({"number"})


def test_counts_types_and_presence() -> None:
    shape = Shape.from_samples(
        [
            {"id": 1, "name": "Ada", "tags": ["a"]},
            {"id": 2, "name": None, "tags": []},
            {"id": 3, "tags": ["b", "c"]},
        ]
    )
    assert shape.count == 3
    assert shape.types == {"object": 3}
    assert shape.is_required("id")
    assert shape.is_required("tags")
    assert not shape.is_required("name")
    assert not shape.is_required("missing")
    assert shape.properties["name"].types == {"string": 1, "null": 1}
    tags = shape.properties["tags"]
    assert tags.items is not None
    assert tags.items.count == 3
    assert shape.field_types() == {"id": "integer", "name": "string | null?", "tags": "array"}


def test_empty_arrays_have_no_item_shape_and_root_arrays_work() -> None:
    assert Shape.from_samples([[]]).items is None
    root = Shape.from_samples([[{"id": 1}, {"id": 2}]])
    assert root.types == {"array": 1}
    assert root.items is not None
    assert root.items.object_count == 2
    assert root.at([ITEMS, "id"]) is not None
    assert root.at(["nope"]) is None
    assert root.at([ITEMS, "id", "deeper"]) is None


def test_depth_and_item_limits() -> None:
    deep: dict[str, object] = {}
    node = deep
    for _ in range(10):
        child: dict[str, object] = {}
        node["n"] = child
        node = child
    shape = Shape.from_samples([deep], max_depth=3)
    assert shape.at(["n", "n", "n"]) is not None
    assert shape.at(["n", "n", "n"]).properties == {}  # type: ignore[union-attr]
    wide = Shape.from_samples([list(range(1000))], max_items=500)
    assert wide.items is not None
    assert wide.items.count == 500


def test_categorical_string_tracking() -> None:
    shape = Shape.from_samples(["active", "past_due", "ACTIVE", "in-progress", "v1.2"])
    assert shape.categorical
    assert shape.values == {"active": 1, "past_due": 1, "ACTIVE": 1, "in-progress": 1, "v1.2": 1}
    for free_text in ["John", "ada@example.com", "2026-10-20T00:00:00Z", "a b", "x" * 40]:
        node = Shape.from_samples(["active", free_text])
        assert not node.categorical, free_text
        assert node.values is None


def test_value_tracking_stops_after_too_many_distinct_values() -> None:
    shape = Shape.from_samples([f"v{i}" for i in range(MAX_TRACKED_VALUES + 1)])
    assert shape.categorical
    assert shape.values is None
    assert shape.enum_values() is None


def test_enum_detection_needs_repetition() -> None:
    assert Shape.from_samples(["a", "b"]).enum_values() is None  # too few observations
    assert Shape.from_samples(["only"] * 20).enum_values() is None  # a single value
    many = Shape.from_samples(["active", "inactive"] * 5)
    assert many.enum_values() == ["active", "inactive"]


def test_merge_adds_observations() -> None:
    first = Shape.from_samples([{"id": 1, "status": "active", "list": [1]}])
    second = Shape.from_samples([{"id": 2, "status": "inactive", "extra": True, "list": []}])
    merged = first.merge(second)
    assert merged.count == 2
    assert merged.is_required("id")
    assert not merged.is_required("extra")
    assert merged.properties["status"].values == {"active": 1, "inactive": 1}
    assert merged.properties["list"].items is not None
    # inputs are untouched
    assert first.count == 1
    assert "extra" not in first.properties
    flipped = second.merge(first)
    assert flipped.to_json() == merged.to_json()


def test_merge_turns_off_tracking_when_one_side_is_free_text() -> None:
    merged = Shape.from_samples(["active"]).merge(Shape.from_samples(["Free text"]))
    assert not merged.categorical
    assert merged.values is None
    overflow = Shape.from_samples([f"a{i}" for i in range(20)]).merge(
        Shape.from_samples([f"b{i}" for i in range(20)])
    )
    assert overflow.categorical
    assert overflow.values is None


def test_merge_all() -> None:
    assert Shape.merge_all([]) is None
    shapes = [Shape.from_samples([{"a": i}]) for i in range(3)]
    merged = Shape.merge_all(shapes)
    assert merged is not None
    assert merged.count == 3
    assert shapes[0].count == 1


def test_serialization_is_canonical_and_round_trips() -> None:
    a = Shape.from_samples([{"b": 1, "a": ["x", "y"], "c": {"d": None}}])
    b = Shape.from_samples([{"c": {"d": None}, "a": ["x", "y"], "b": 1}])
    assert a.to_json() == b.to_json()
    assert a.fingerprint() == b.fingerprint()
    restored = Shape.from_json(a.to_json())
    assert restored.to_json() == a.to_json()
    free = Shape.from_samples(["Some Text"])
    assert Shape.from_json(free.to_json()).categorical is False
    overflowing = Shape.from_samples([f"v{i}" for i in range(40)])
    data = json.loads(overflowing.to_json())
    assert data["values"] is None
    assert Shape.from_json(overflowing.to_json()).values is None


def test_json_schema_export() -> None:
    shape = Shape.from_samples(
        [
            {"id": 1, "price": 1.5, "status": "active", "tags": ["a"], "note": None},
            {"id": 2, "price": 2, "status": "inactive", "tags": []},
        ]
        * 5
    )
    schema = to_json_schema(shape, title="items")
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["title"] == "items"
    assert schema["type"] == "object"
    assert schema["required"] == ["id", "price", "status", "tags"]
    props = schema["properties"]
    assert props["price"]["type"] == "number"  # integer folded into number
    assert props["note"]["type"] == "null"
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["status"]["x-observed-values"] == ["active", "inactive"]
    assert "10 observed" in schema["description"]
    assert to_json_schema(Shape())["description"].startswith("Inferred")
