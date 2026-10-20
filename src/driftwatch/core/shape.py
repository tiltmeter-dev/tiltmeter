"""Shape: what driftwatch knows about the values seen at every location of a JSON body.

A ``Shape`` is a tree of counters, not a schema. Each node records how many values
it saw and of which JSON types, the shape of each object property (a property's
``count`` is the number of objects that contained it) and the merged shape of
array items. Because it is made of counts, two shapes merge by adding them, which
is how driftwatch builds a baseline out of many past responses: a field is
*required* when it appeared in every object ever observed, and *optional*
otherwise.

String values are tracked only while they look categorical (``active``,
``past_due``, ``USD``...) so that enum-like fields can be recognised. Anything
that looks like free text, an id, a timestamp or a URL switches tracking off for
that node, which also keeps personal data out of the stored baseline.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from driftwatch.core.paths import ArrayItems, Segment

JSON_TYPES: Final = ("object", "array", "string", "integer", "number", "boolean", "null")
_TYPE_RANK: Final = {name: rank for rank, name in enumerate(JSON_TYPES)}

MAX_DEPTH: Final = 32
MAX_ARRAY_ITEMS: Final = 500
MAX_TRACKED_VALUES: Final = 32
MAX_ENUM_VALUES: Final = 16
MIN_ENUM_OCCURRENCES: Final = 10
MAX_VALUE_LENGTH: Final = 32

# lower_snake / kebab / dotted words, or UPPER_SNAKE. Names, emails, ids with
# mixed case, timestamps and URLs never match.
_CATEGORICAL: Final = re.compile(
    r"[a-z][a-z0-9]*(?:[_.\-][a-z0-9]+)*|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*"
)


def json_type(value: object) -> str:
    """The JSON Schema type name of a value produced by ``json.loads``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def type_label(types: Iterable[str]) -> str:
    """Readable union such as ``string | null``. ``integer`` folds into ``number``."""
    names = set(types)
    if "number" in names:
        names.discard("integer")
    return " | ".join(sorted(names, key=_TYPE_RANK.__getitem__)) or "nothing"


def core_types(types: Iterable[str]) -> frozenset[str]:
    """Type set without ``null`` and with ``integer`` folded into ``number``."""
    names = {"number" if name == "integer" else name for name in types}
    names.discard("null")
    return frozenset(names)


def fingerprint_of(serialized: str) -> str:
    """Content address of a serialized shape."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Shape:
    """Accumulated observations for one location in a JSON body."""

    count: int = 0
    types: dict[str, int] = field(default_factory=dict)
    properties: dict[str, Shape] = field(default_factory=dict)
    items: Shape | None = None
    values: dict[str, int] | None = field(default_factory=dict)
    categorical: bool = True

    # -- building -------------------------------------------------------------

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[object],
        *,
        max_depth: int = MAX_DEPTH,
        max_items: int = MAX_ARRAY_ITEMS,
    ) -> Shape:
        """Infer a shape from parsed JSON bodies."""
        shape = cls()
        for sample in samples:
            shape.add(sample, max_depth=max_depth, max_items=max_items)
        return shape

    def add(
        self, value: object, *, max_depth: int = MAX_DEPTH, max_items: int = MAX_ARRAY_ITEMS
    ) -> None:
        """Record one more value observed at this location."""
        self._add(value, 0, max_depth, max_items)

    def _add(self, value: object, depth: int, max_depth: int, max_items: int) -> None:
        kind = json_type(value)
        self.count += 1
        self.types[kind] = self.types.get(kind, 0) + 1
        if isinstance(value, str):
            self._track(value)
            return
        if depth >= max_depth:
            return
        if isinstance(value, dict):
            for key, child_value in value.items():
                child = self.properties.get(key)
                if child is None:
                    child = self.properties[key] = Shape()
                child._add(child_value, depth + 1, max_depth, max_items)
        elif isinstance(value, list) and value:
            if self.items is None:
                self.items = Shape()
            for item in value[:max_items]:
                self.items._add(item, depth + 1, max_depth, max_items)

    def _track(self, value: str) -> None:
        if not self.categorical:
            return
        if len(value) > MAX_VALUE_LENGTH or not _CATEGORICAL.fullmatch(value):
            self.categorical = False
            self.values = None
            return
        if self.values is None:
            return
        self.values[value] = self.values.get(value, 0) + 1
        if len(self.values) > MAX_TRACKED_VALUES:
            self.values = None

    # -- queries --------------------------------------------------------------

    @property
    def object_count(self) -> int:
        """How many of the observed values were objects."""
        return self.types.get("object", 0)

    @property
    def type_set(self) -> frozenset[str]:
        return frozenset(self.types)

    @property
    def label(self) -> str:
        return type_label(self.types)

    def is_required(self, name: str) -> bool:
        """True when the property appeared in every object observed here."""
        child = self.properties.get(name)
        return child is not None and self.object_count > 0 and child.count >= self.object_count

    def enum_values(self) -> list[str] | None:
        """The known values when this looks like an enum, else ``None``.

        It takes at least two distinct categorical values, each seen several times
        on average, before a field is treated as an enum.
        """
        if not self.categorical or self.values is None:
            return None
        distinct = len(self.values)
        if not 2 <= distinct <= MAX_ENUM_VALUES:
            return None
        if sum(self.values.values()) < max(MIN_ENUM_OCCURRENCES, 3 * distinct):
            return None
        return sorted(self.values)

    def at(self, segments: Sequence[Segment]) -> Shape | None:
        """The node at a path below this one, if it was ever observed."""
        node: Shape | None = self
        for segment in segments:
            if node is None:
                return None
            node = node.items if isinstance(segment, ArrayItems) else node.properties.get(segment)
        return node

    def field_types(self, limit: int = 80) -> dict[str, str]:
        """Property name to type label; ``?`` marks properties that are not always present."""
        return {
            name: self.properties[name].label + ("" if self.is_required(name) else "?")
            for name in sorted(self.properties)[:limit]
        }

    # -- combining ------------------------------------------------------------

    def copy(self) -> Shape:
        """Deep copy."""
        return Shape(
            count=self.count,
            types=dict(self.types),
            properties={name: child.copy() for name, child in self.properties.items()},
            items=self.items.copy() if self.items is not None else None,
            values=dict(self.values) if self.values is not None else None,
            categorical=self.categorical,
        )

    def merge(self, other: Shape) -> Shape:
        """A new shape holding the observations of both."""
        categorical = self.categorical and other.categorical
        values: dict[str, int] | None = None
        if categorical and self.values is not None and other.values is not None:
            values = dict(self.values)
            for value, seen in other.values.items():
                values[value] = values.get(value, 0) + seen
            if len(values) > MAX_TRACKED_VALUES:
                values = None
        properties: dict[str, Shape] = {}
        for name in [*self.properties, *(n for n in other.properties if n not in self.properties)]:
            mine, theirs = self.properties.get(name), other.properties.get(name)
            if mine is not None and theirs is not None:
                properties[name] = mine.merge(theirs)
            elif mine is not None:
                properties[name] = mine.copy()
            elif theirs is not None:
                properties[name] = theirs.copy()
        if self.items is not None and other.items is not None:
            items: Shape | None = self.items.merge(other.items)
        else:
            source = self.items if self.items is not None else other.items
            items = source.copy() if source is not None else None
        types = dict(self.types)
        for kind, seen in other.types.items():
            types[kind] = types.get(kind, 0) + seen
        return Shape(
            count=self.count + other.count,
            types=types,
            properties=properties,
            items=items,
            values=values,
            categorical=categorical,
        )

    @staticmethod
    def merge_all(shapes: Iterable[Shape]) -> Shape | None:
        """Merge any number of shapes; ``None`` when there are none."""
        merged: Shape | None = None
        for shape in shapes:
            merged = shape.copy() if merged is None else merged.merge(shape)
        return merged

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"count": self.count, "types": dict(sorted(self.types.items()))}
        if self.properties:
            data["properties"] = {
                name: child.to_dict() for name, child in sorted(self.properties.items())
            }
        if self.items is not None:
            data["items"] = self.items.to_dict()
        if not self.categorical:
            data["categorical"] = False
        elif self.values is None:
            data["values"] = None
        elif self.values:
            data["values"] = dict(sorted(self.values.items()))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Shape:
        categorical = bool(data.get("categorical", True))
        values: dict[str, int] | None
        if not categorical or ("values" in data and data["values"] is None):
            values = None
        else:
            values = {str(k): int(v) for k, v in (data.get("values") or {}).items()}
        items = data.get("items")
        return cls(
            count=int(data["count"]),
            types={str(k): int(v) for k, v in data["types"].items()},
            properties={
                str(name): cls.from_dict(child)
                for name, child in (data.get("properties") or {}).items()
            },
            items=cls.from_dict(items) if items is not None else None,
            values=values,
            categorical=categorical,
        )

    def to_json(self) -> str:
        """Canonical, compact JSON: equal shapes serialize to equal text."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Shape:
        return cls.from_dict(json.loads(text))

    def fingerprint(self) -> str:
        return fingerprint_of(self.to_json())


def to_json_schema(shape: Shape, *, title: str | None = None) -> dict[str, Any]:
    """Export a shape as a JSON Schema (draft 2020-12) document.

    Enum-like fields carry their observed values under ``x-observed-values``
    rather than ``enum``: they are what driftwatch has seen, not a promise.
    """
    document: dict[str, Any] = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if title:
        document["title"] = title
    document["description"] = f"Inferred by driftwatch from {shape.count} observed response(s)."
    document.update(_schema_node(shape))
    return document


def _schema_node(shape: Shape) -> dict[str, Any]:
    names = sorted(shape.types, key=_TYPE_RANK.__getitem__)
    if "number" in names and "integer" in names:
        names.remove("integer")
    node: dict[str, Any] = {}
    if names:
        node["type"] = names[0] if len(names) == 1 else names
    if shape.properties:
        node["properties"] = {
            name: _schema_node(child) for name, child in sorted(shape.properties.items())
        }
        required = [name for name in sorted(shape.properties) if shape.is_required(name)]
        if required:
            node["required"] = required
    if shape.items is not None:
        node["items"] = _schema_node(shape.items)
    observed = shape.enum_values()
    if observed:
        node["x-observed-values"] = observed
    return node
