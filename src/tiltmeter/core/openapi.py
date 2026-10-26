"""Response shapes from an OpenAPI document, so two versions of a spec diff like two checks.

Each operation's success response schema becomes a :class:`~tiltmeter.core.shape.Shape`
built as if responses had been observed: required properties appear in every object,
optional ones in only some, enums carry their values often enough to count as enums, and
``nullable`` or a ``null`` type adds ``null``. Two versions then go through the same diff
as two checks, in its ``complete`` mode, because a spec lists every field an API returns.

Named schemas are not copied into every place that refers to them: large specs reuse
them so much (Stripe's "id or expanded object" fields) that the copies grow
exponentially. Instead, a reference stays a leaf, every named schema is flattened once,
and each pair of old and new schemas is compared once. An operation then reports the
changes of every pair it reaches, at the shortest path that reaches it, going through at
most ``expand_depth`` "id or object" fields.

OpenAPI 3.0 and 3.1 are supported, and so is Swagger 2.0. Only local ``$ref`` pointers
are followed (bundle a multi-file spec first). A reference that points at another file, or
at nothing (real specs have typos), stands for an unknown schema: nothing below it is
compared, and :attr:`SpecDiff.unresolved` lists it.
"""

from __future__ import annotations

import heapq
import itertools
import re
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import reduce
from typing import Any, Final
from urllib.parse import unquote

from tiltmeter.core.classify import classify
from tiltmeter.core.diff import annotate_renames, diff_shapes
from tiltmeter.core.models import Change, ChangeKind, RawChange
from tiltmeter.core.paths import ITEMS, ArrayItems, Segment, format_path
from tiltmeter.core.shape import JSON_TYPES, MAX_DEPTH, MIN_ENUM_OCCURRENCES, Shape

HTTP_METHODS: Final = ("get", "post", "put", "patch", "delete")
_ANY: Final = frozenset(JSON_TYPES)
_SCALARS: Final = frozenset({"string", "integer", "number", "boolean", "null"})
_NULL: Final = frozenset({"null"})
_JSON_MEDIA: Final = re.compile(r"application/(?:[\w.\-]+\+)?json", re.IGNORECASE)
_CONSTRAINTS: Final = frozenset(
    {"type", "enum", "const", "properties", "required", "items", "additionalProperties"}
)
_STRUCTURE: Final = _CONSTRAINTS | {"$ref", "allOf", "anyOf", "oneOf"}
_FREE_TEXT: Final = "a value that does not look like an enum member (not shown)"
# Object types are counted twice; a required property appears twice, an optional one once,
# so the baseline rule "required means in every object" applies unchanged.
_SEEN: Final = 2
_SOMETIMES: Final = 1
# Schema nodes and shapes built for one comparison. A hostile or broken spec stops here
# instead of running for hours.
MAX_NODES: Final = 1_000_000
# How deep, in properties, the fields inside a new field are read as places a removed field
# may have moved to, and how many of them one site keeps.
_INSIDE_DEPTH: Final = 3
_INSIDE_LIMIT: Final = 10_000
_STATUS: Final = re.compile(r"[2-5][0-9][0-9]")
_PARAMETER: Final = re.compile(r"\{[^}]*\}")
# Where a change sorts among the others at its path, as the shape diff walks a tree:
# property-level changes, then null, then type, then the children, then new enum values.
_ORDER: Final = {
    ChangeKind.FIELD_ADDED: 0,
    ChangeKind.FIELD_REMOVED: 0,
    ChangeKind.FIELD_NOW_OPTIONAL: 0,
    ChangeKind.NULLABLE_INTRODUCED: 1,
    ChangeKind.TYPE_CHANGED: 2,
    ChangeKind.ENUM_VALUE_ADDED: 9,
}


class OpenAPIError(ValueError):
    """A document that is not an OpenAPI or Swagger description Tiltmeter can read."""


@dataclass(frozen=True, slots=True)
class Operation:
    """One operation and the shape of its success response (named schemas left opaque)."""

    method: str
    path: str
    status: str | None
    shape: Shape | None


@dataclass(frozen=True, slots=True)
class OperationDiff:
    """The changes to one operation's success response between two versions of a spec."""

    method: str
    path: str
    changes: list[Change]
    omitted: int = 0


@dataclass(frozen=True, slots=True)
class SpecDiff:
    """Every operation whose response changed, the operations that are gone, and the
    references that could not be followed."""

    operations: list[OperationDiff]
    removed: list[str]
    unresolved: list[str] = field(default_factory=list)


# -- schemas as a graph -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Ref:
    """A place that holds one of some named schemas, maybe beside plain values."""

    targets: frozenset[str]
    extras: frozenset[str] = frozenset()

    @property
    def expandable(self) -> bool:
        """An "id or object" field, as Stripe uses for objects you can ask to expand."""
        return "string" in self.extras


@dataclass(frozen=True, slots=True)
class _Node:
    """What a schema allows at one location, with named schemas below it left as refs."""

    types: frozenset[str]
    properties: Mapping[str, tuple[_Child, bool]] = field(default_factory=dict)
    items: _Child | None = None
    enum: frozenset[str] | None = None


type _Child = _Node | _Ref
type _Key = tuple[frozenset[str], frozenset[str]]

_ANYTHING: Final = _Node(_ANY)
# What a reference that cannot be followed stands for. Unlike a schema that allows
# anything, it is compared with nothing, so a typo in a spec reports no changes.
_UNKNOWN: Final = _Node(_ANY)


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_NODES

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise OpenAPIError(f"the response schemas expand to more than {MAX_NODES} nodes")


def _value_type(value: object) -> str:
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
    return "array" if isinstance(value, list) else "object"


def _plain(child: _Child) -> frozenset[str] | None:
    """The types of a schema that is only a plain scalar, such as ``{type: string}``."""
    if isinstance(child, _Node) and child.types <= _SCALARS and child.enum is None:
        return child.types if not child.properties and child.items is None else None
    return None


class _Catalog:
    """One document: its schemas as nodes, and every named schema flattened once."""

    def __init__(self, document: Mapping[str, Any], budget: _Budget) -> None:
        self.document = document
        self._budget = budget
        self._children: dict[int, _Child] = {}
        self._flat: dict[frozenset[str], _Node] = {}
        self._building: set[frozenset[str]] = set()
        self._depth = 0
        self.unresolved: set[str] = set()

    def resolve(self, ref: str) -> Any:
        """The value a local JSON pointer such as ``#/components/schemas/Customer`` names,
        or None when it names nothing in the document."""
        if not ref.startswith("#"):
            self.unresolved.add(ref)
            return None
        node: Any = self.document
        for token in ref[1:].split("/")[1:]:
            name = unquote(token).replace("~1", "/").replace("~0", "~")
            if isinstance(node, Mapping) and name in node:
                node = node[name]
            elif (
                isinstance(node, list)
                and name.isascii()
                and name.isdigit()
                and int(name) < len(node)
            ):
                node = node[int(name)]
            else:
                self.unresolved.add(ref)
                return None
        return node

    def target(self, ref: str) -> str:
        """Where a reference ends, through named schemas that only refer on."""
        seen: set[str] = set()
        while ref not in seen:
            seen.add(ref)
            schema = self.resolve(ref)
            if not isinstance(schema, Mapping):
                break
            onward = schema.get("$ref")
            if not isinstance(onward, str) or schema.keys() & (_STRUCTURE - {"$ref"}):
                break
            ref = onward
        return ref

    # -- building -----------------------------------------------------------------

    def child(self, schema: Any) -> _Child:
        """The node or ref a schema of the document stands for, built once."""
        if not isinstance(schema, Mapping):
            return _ANYTHING
        key = id(schema)  # schemas are the document's own objects, alive as long as it is
        found = self._children.get(key)
        if found is None:
            if self._depth > MAX_DEPTH:
                return _ANYTHING
            self._depth += 1
            try:
                found = self._build(schema)
            finally:
                self._depth -= 1
            self._children[key] = found
        return found

    def _build(self, schema: Mapping[str, Any]) -> _Child:
        self._budget.spend()
        nullable = schema.get("nullable") is True or schema.get("x-nullable") is True
        null = _NULL if nullable else frozenset()
        structure = schema.keys() & _STRUCTURE
        ref = schema.get("$ref")
        if structure == {"$ref"} and isinstance(ref, str):
            return _Ref(frozenset({self.target(ref)}), null)
        for key in ("allOf", "anyOf", "oneOf"):
            subschemas = schema.get(key)
            if structure == {key} and isinstance(subschemas, list) and subschemas:
                only = self.child(subschemas[0]) if len(subschemas) == 1 else None
                if key != "allOf" and only is None:
                    only = self._union(subschemas)
                if isinstance(only, _Ref):
                    return _Ref(only.targets, only.extras | null)
        parts: list[_Node] = []
        if isinstance(ref, str):
            parts.append(self.expand(_Ref(frozenset({self.target(ref)}))))
        subschemas = schema.get("allOf")
        if isinstance(subschemas, list):
            parts += [self.node(self.child(sub)) for sub in subschemas]
        for key in ("oneOf", "anyOf"):
            alternatives = schema.get(key)
            if isinstance(alternatives, list) and alternatives:
                parts.append(self.node(self._union(alternatives)))
        own = self._own(schema)
        if own is not None:
            parts.append(own)
        node = reduce(self.all_of, parts) if parts else _ANYTHING
        return replace(node, types=node.types | null) if nullable else node

    def _union(self, alternatives: Sequence[Any]) -> _Child:
        children = [self.child(alternative) for alternative in alternatives]
        refs = [child for child in children if isinstance(child, _Ref)]
        plains = [types for child in children if (types := _plain(child)) is not None]
        if refs and len(refs) + len(plains) == len(children):
            targets = frozenset().union(*(ref.targets for ref in refs))
            extras = frozenset().union(*(ref.extras for ref in refs), *plains)
            # A free string beside a string enum takes any string: no enum to compare.
            if not ("string" in extras and self._scalar(targets)):
                return _Ref(targets, extras)
        return reduce(self.any_of, [self.node(child) for child in children])

    def _own(self, schema: Mapping[str, Any]) -> _Node | None:
        """What the schema's own keywords allow, or None when it has none."""
        if not schema.keys() & _CONSTRAINTS:
            return None
        declared = schema.get("type")
        types: set[str] | None = None
        if isinstance(declared, str):
            types = {declared} if declared in _ANY else set(_ANY)
        elif isinstance(declared, list):
            types = {name for name in declared if name in _ANY} or set(_ANY)
        values = schema.get("enum")
        if "const" in schema:
            values = [schema["const"]]
        enum: frozenset[str] | None = None
        if isinstance(values, list):
            enum = frozenset(value for value in values if isinstance(value, str)) or None
            if types is None:
                types = {_value_type(value) for value in values}
            if None in values:
                types.add("null")
        properties = schema.get("properties")
        required = schema.get("required")
        required_names = (
            {name for name in required if isinstance(name, str)}
            if isinstance(required, list)
            else set()
        )
        if types is None:
            if (
                isinstance(properties, Mapping)
                or required_names
                or "additionalProperties" in schema
            ):
                types = {"object"}
            elif "items" in schema:
                types = {"array"}
            else:
                types = set(_ANY)
        children: dict[str, tuple[_Child, bool]] = {}
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                if isinstance(name, str):
                    children[name] = (self.child(sub), name in required_names)
        for name in required_names - children.keys():
            children[name] = (_ANYTHING, True)  # declared elsewhere, as in allOf parts
        items = schema.get("items")
        return _Node(
            frozenset(types),
            children,
            self.child(items) if isinstance(items, Mapping) else None,
            enum if enum is not None and "string" in types else None,
        )

    def _scalar(self, targets: frozenset[str]) -> bool:
        """Whether every target is a plain scalar schema, read without flattening it."""
        for target in targets:
            schema = self.resolve(target)
            if not isinstance(schema, Mapping) or schema.keys() & (_STRUCTURE - _CONSTRAINTS):
                return False
            if schema.keys() & {"properties", "items", "additionalProperties"}:
                return False
            declared = schema.get("type")
            values = schema.get("enum")
            if isinstance(declared, str):
                types = {declared}
            elif isinstance(declared, list):
                types = set(declared)
            elif isinstance(values, list):
                types = {_value_type(value) for value in values}
            else:
                return False
            if not types <= _SCALARS:
                return False
        return True

    # -- named schemas ------------------------------------------------------------

    def flat(self, targets: frozenset[str]) -> _Node:
        """The named schemas, any one of them, as one node whose children stay refs."""
        found = self._flat.get(targets)
        if found is not None:
            return found
        if targets in self._building:
            return _ANYTHING  # a schema made of itself, through allOf: stop where it repeats
        self._building.add(targets)
        # Depth counts inline nesting within one named schema, so a schema reads the same
        # whichever operation reaches it first.
        depth, self._depth = self._depth, 0
        try:
            nodes = [self._named(target) for target in sorted(targets)]
            found = (
                _UNKNOWN if any(node is _UNKNOWN for node in nodes) else reduce(self.any_of, nodes)
            )
        finally:
            self._building.discard(targets)
            self._depth = depth
        self._flat[targets] = found
        return found

    def _named(self, target: str) -> _Node:
        schema = self.resolve(target)
        return _UNKNOWN if schema is None else self.node(self.child(schema))

    def expand(self, ref: _Ref) -> _Node:
        """A ref one level deep: its schemas' fields, beside its plain alternatives."""
        node = self.flat(ref.targets)
        if not ref.extras or node is _UNKNOWN:
            return node
        enum = None if "string" in ref.extras else node.enum
        return replace(node, types=node.types | ref.extras, enum=enum)

    def node(self, child: _Child) -> _Node:
        return self.expand(child) if isinstance(child, _Ref) else child

    def root_types(self, ref: _Ref) -> frozenset[str]:
        return self.flat(ref.targets).types | ref.extras

    # -- combining ----------------------------------------------------------------

    def all_of(self, a: _Node, b: _Node) -> _Node:
        """A value that must satisfy both: types intersect, properties add up."""
        properties = dict(a.properties)
        for name, (child, required) in b.properties.items():
            if name in properties:
                other, other_required = properties[name]
                properties[name] = (self._all_children(other, child), required or other_required)
            else:
                properties[name] = (child, required)
        if a.items is not None and b.items is not None:
            items: _Child | None = self._all_children(a.items, b.items)
        else:
            items = a.items if a.items is not None else b.items
        if a.enum is not None and b.enum is not None:
            enum: frozenset[str] | None = a.enum & b.enum
        else:
            enum = a.enum if a.enum is not None else b.enum
        # Contradictory parts would allow nothing at all; keep both rather than invent a change.
        return _Node((a.types & b.types) or (a.types | b.types), properties, items, enum)

    def any_of(self, a: _Node, b: _Node) -> _Node:
        """A value that satisfies either: types add up, a property is required only in both."""
        properties: dict[str, tuple[_Child, bool]] = {}
        for name in sorted(a.properties.keys() | b.properties.keys()):
            mine, theirs = a.properties.get(name), b.properties.get(name)
            if mine is not None and theirs is not None:
                properties[name] = (self._any_children(mine[0], theirs[0]), mine[1] and theirs[1])
            elif mine is not None:
                properties[name] = (mine[0], mine[1] and "object" not in b.types)
            elif theirs is not None:
                properties[name] = (theirs[0], theirs[1] and "object" not in a.types)
        if a.items is not None and b.items is not None:
            items: _Child | None = self._any_children(a.items, b.items)
        else:
            items = a.items if a.items is not None else b.items
        if a.enum is not None and b.enum is not None:
            enum: frozenset[str] | None = a.enum | b.enum
        elif "string" not in a.types:
            enum = b.enum
        elif "string" not in b.types:
            enum = a.enum
        else:
            enum = None  # one alternative takes any string
        return _Node(a.types | b.types, properties, items, enum)

    def _all_children(self, a: _Child, b: _Child) -> _Child:
        # A property only listed as required in one part takes its schema from the other.
        if b is _ANYTHING or b is _UNKNOWN:
            return a
        if a is _ANYTHING or a is _UNKNOWN:
            return b
        if isinstance(a, _Ref) and isinstance(b, _Ref) and a.targets == b.targets:
            return _Ref(a.targets, a.extras & b.extras)
        return self.all_of(self.node(a), self.node(b))

    def _any_children(self, a: _Child, b: _Child) -> _Child:
        if isinstance(a, _Ref) and isinstance(b, _Ref):
            return _Ref(a.targets | b.targets, a.extras | b.extras)
        return self.any_of(self.node(a), self.node(b))

    # -- shapes -------------------------------------------------------------------

    def shape(self, child: _Child, count: int = _SEEN) -> Shape:
        """The shape of a schema on its own, with named schemas below it left opaque."""
        self._budget.spend()
        if isinstance(child, _Ref):
            return _placeholder(self.root_types(child), count)
        shape = _bare(child, count)
        if "object" in child.types:
            shape.properties = {
                name: self.shape(sub, _SEEN if required else _SOMETIMES)
                for name, (sub, required) in sorted(child.properties.items())
            }
        if "array" in child.types and child.items is not None:
            shape.items = self.shape(child.items)
        return shape


def _bare(node: _Node, count: int) -> Shape:
    """A node's own types and enum values, without its children."""
    shape = Shape(count=count, types=dict.fromkeys(node.types, _SEEN))
    if "string" in node.types:
        if node.enum:
            shape.values = dict.fromkeys(sorted(node.enum), MIN_ENUM_OCCURRENCES)
        else:
            shape.values, shape.categorical = None, False
    return shape


def _placeholder(types: frozenset[str], count: int) -> Shape:
    """Where a named schema sits: its types only, so the diff never descends into it."""
    return Shape(count=count, types=dict.fromkeys(types, _SEEN), values=None, categorical=False)


# -- comparing two documents ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Edge:
    """Where, below a compared location, a pair of named schemas sits."""

    segments: tuple[Segment, ...]
    key: _Key
    expandable: bool


@dataclass(frozen=True, slots=True)
class _Local:
    """The changes found at one location, the pairs of named schemas below it, and the
    new schema of each field added there."""

    changes: tuple[RawChange, ...]
    edges: tuple[_Edge, ...]
    added: tuple[tuple[tuple[Segment, ...], _Child], ...] = ()


type _Inside = Mapping[str, tuple[tuple[tuple[Segment, ...], frozenset[str]], ...]]


class _Comparison:
    """Two versions of a spec. Each pair of named schemas is compared once."""

    def __init__(self, old: _Catalog, new: _Catalog, budget: _Budget) -> None:
        self.old = old
        self.new = new
        self._budget = budget
        self._pairs: dict[_Key, _Local] = {}
        self._inside: dict[_Key, _Inside] = {}

    def pair(self, key: _Key) -> _Local:
        local = self._pairs.get(key)
        if local is None:
            local = self.site(self.old.flat(key[0]), self.new.flat(key[1]))
            self._pairs[key] = local
        return local

    def site(self, old: _Child, new: _Child) -> _Local:
        """Compare two schemas, stopping at pairs of named schemas."""
        edges: list[_Edge] = []
        enums: list[RawChange] = []
        fresh: dict[tuple[Segment, ...], _Child] = {}
        before, after = self._both(old, new, (), edges, _SEEN, _SEEN, enums, fresh)
        raw = [*diff_shapes(before, after, complete=True, renames=False), *enums]
        added = tuple(
            (change.segments, fresh[change.segments])
            for change in raw
            if change.kind is ChangeKind.FIELD_ADDED and change.segments in fresh
        )
        return _Local(tuple(_spec_wording(change) for change in raw), tuple(edges), added)

    def _both(
        self,
        a: _Child,
        b: _Child,
        segments: tuple[Segment, ...],
        edges: list[_Edge],
        count_a: int,
        count_b: int,
        enums: list[RawChange],
        fresh: dict[tuple[Segment, ...], _Child],
    ) -> tuple[Shape, Shape]:
        self._budget.spend()
        if isinstance(a, _Ref) and isinstance(b, _Ref):
            # The pair reports its own root changes; here only the plain alternatives count.
            roots = self.old.flat(a.targets).types | self.new.flat(b.targets).types
            edges.append(_Edge(segments, (a.targets, b.targets), a.expandable or b.expandable))
            return _placeholder(a.extras | roots, count_a), _placeholder(b.extras | roots, count_b)
        node_a, node_b = self.old.node(a), self.new.node(b)
        if node_a is _UNKNOWN or node_b is _UNKNOWN:
            # One side could not be read: report nothing here rather than guess.
            types = node_b.types if node_a is _UNKNOWN else node_a.types
            return _placeholder(types, count_a), _placeholder(types, count_b)
        before, after = _bare(node_a, count_a), _bare(node_b, count_b)
        if node_a.enum is not None and len(node_a.enum) == 1 and "string" in node_b.types:
            # The shape diff needs two values to see an enum; a spec says so with one.
            added = sorted(node_b.enum - node_a.enum) if node_b.enum is not None else None
            if added is None or added:
                enums.append(
                    RawChange(
                        format_path(segments),
                        ChangeKind.ENUM_VALUE_ADDED,
                        before=next(iter(node_a.enum)),
                        after=", ".join(added) if added else "any string",
                        segments=segments,
                        types=node_b.types,
                    )
                )
        objects = ("object" in node_a.types, "object" in node_b.types)
        for name in sorted(node_a.properties.keys() | node_b.properties.keys()):
            mine = node_a.properties.get(name) if objects[0] else None
            theirs = node_b.properties.get(name) if objects[1] else None
            count_mine = _SEEN if mine and mine[1] else _SOMETIMES
            count_theirs = _SEEN if theirs and theirs[1] else _SOMETIMES
            child_segments = (*segments, name)
            if mine is not None and theirs is not None:
                before.properties[name], after.properties[name] = self._both(
                    mine[0],
                    theirs[0],
                    child_segments,
                    edges,
                    count_mine,
                    count_theirs,
                    enums,
                    fresh,
                )
            elif mine is not None:
                before.properties[name] = self._alone(self.old, mine[0], count_mine)
            elif theirs is not None:
                after.properties[name] = self._alone(self.new, theirs[0], count_theirs)
                fresh[child_segments] = theirs[0]
        items_a = node_a.items if "array" in node_a.types else None
        items_b = node_b.items if "array" in node_b.types else None
        if items_a is not None and items_b is not None:
            before.items, after.items = self._both(
                items_a, items_b, (*segments, ITEMS), edges, _SEEN, _SEEN, enums, fresh
            )
        elif items_a is not None:
            before.items = self._alone(self.old, items_a, _SEEN)
        elif items_b is not None:
            after.items = self._alone(self.new, items_b, _SEEN)
        return before, after

    def _alone(self, catalog: _Catalog, child: _Child, count: int) -> Shape:
        """A location on one side only: its types are all the diff needs."""
        self._budget.spend()
        if isinstance(child, _Ref):
            return _placeholder(catalog.root_types(child), count)
        return _bare(child, count)

    def inside(self, local: _Local, key: _Key | None) -> _Inside:
        """The fields within each field added at a site, by name, a few properties deep.

        They are never reported, only offered as places a removed field moved to. An "id or
        object" field counts as one field and is not opened: by default it holds an id.
        """
        found = self._inside.get(key) if key is not None else None
        if found is not None:
            return found
        fields: defaultdict[str, list[tuple[tuple[Segment, ...], frozenset[str]]]]
        fields = defaultdict(list)
        queue: deque[tuple[_Child, tuple[Segment, ...], int, frozenset[frozenset[str]]]]
        queue = deque(
            (child, segments, _INSIDE_DEPTH, frozenset()) for segments, child in local.added
        )
        kept = 0
        while queue and kept < _INSIDE_LIMIT:  # breadth first, so a cut keeps the nearest
            child, segments, depth, seen = queue.popleft()
            if isinstance(child, _Ref):
                if child.expandable or child.targets in seen:
                    continue
                seen |= {child.targets}
            node = self.new.node(child)
            if "object" in node.types and depth:
                for name, (sub, _) in sorted(node.properties.items()):
                    inner = (*segments, name)
                    types = self.new.root_types(sub) if isinstance(sub, _Ref) else sub.types
                    fields[name].append((inner, types))
                    kept += 1
                    queue.append((sub, inner, depth - 1, seen))
            if "array" in node.types and node.items is not None:
                queue.append((node.items, (*segments, ITEMS), depth, seen))
        found = {name: tuple(entries) for name, entries in fields.items()}
        if key is not None:
            self._inside[key] = found
        return found

    def emit(self, old: _Child, new: _Child, *, expand_depth: int | None) -> list[RawChange]:
        """Every change an operation's response reaches, once, at its shortest path."""
        root = self.site(old, new)
        found: dict[tuple[str, ChangeKind, str | None, str | None], RawChange] = {}
        queue: list[tuple[int, int, str, int, tuple[Segment, ...], _Key]] = []
        tiebreak = itertools.count()

        def add(prefix: tuple[Segment, ...], changes: Sequence[RawChange]) -> None:
            for change in changes:
                segments = (*prefix, *change.segments)
                path = format_path(segments)
                key = (path, change.kind, change.before, change.after)
                if key not in found:
                    found[key] = replace(change, path=path, segments=segments, hint=None)

        def push(prefix: tuple[Segment, ...], hops: int, edges: Sequence[_Edge]) -> None:
            for edge in edges:
                segments = (*prefix, *edge.segments)
                total = hops + int(edge.expandable)
                if len(segments) > MAX_DEPTH or (expand_depth is not None and total > expand_depth):
                    continue
                entry = (total, len(segments), format_path(segments), next(tiebreak))
                heapq.heappush(queue, (*entry, segments, edge.key))

        add((), root.changes)
        push((), 0, root.edges)
        grown: list[tuple[tuple[Segment, ...], _Local, _Key | None]] = []
        if root.added:
            grown.append(((), root, None))
        reached: set[_Key] = set()
        while queue:
            hops, _, _, _, segments, key = heapq.heappop(queue)
            if key in reached:
                continue
            reached.add(key)
            local = self.pair(key)
            add(segments, local.changes)
            push(segments, hops, local.edges)
            if local.added:
                grown.append((segments, local, key))
        changes = sorted(found.values(), key=_traversal)
        return annotate_renames(changes, self._moved_into(changes, grown))

    def _moved_into(
        self,
        changes: Sequence[RawChange],
        grown: Sequence[tuple[tuple[Segment, ...], _Local, _Key | None]],
    ) -> list[RawChange]:
        """The fields inside added fields that share a name with a removed field."""
        names = {
            name
            for change in changes
            if change.kind is ChangeKind.FIELD_REMOVED
            and change.segments
            and isinstance(name := change.segments[-1], str)
        }
        if not names:
            return []
        found: dict[str, RawChange] = {}
        for prefix, local, key in grown:
            fields = self.inside(local, key)
            for name in sorted(names & fields.keys()):
                for inner, types in fields[name]:
                    segments = (*prefix, *inner)
                    path = format_path(segments)
                    found.setdefault(
                        path,
                        RawChange(path, ChangeKind.FIELD_ADDED, segments=segments, types=types),
                    )
        return list(found.values())


def _traversal(change: RawChange) -> tuple[tuple[Any, ...], ...]:
    """Sort key that puts changes in the order the shape diff walks a tree."""
    steps = tuple((4,) if isinstance(s, ArrayItems) else (3, s) for s in change.segments)
    return (*steps, (_ORDER.get(change.kind, 5),))


# -- documents ------------------------------------------------------------------


def _version(document: Mapping[str, Any]) -> int:
    openapi = document.get("openapi")
    if isinstance(openapi, str) and openapi.startswith("3."):
        return 3
    if str(document.get("swagger")) == "2.0":
        return 2
    raise OpenAPIError("expected an OpenAPI 3 or Swagger 2.0 document")


def base_url(document: Mapping[str, Any]) -> str | None:
    """The https base URL the document declares, when it names a concrete one."""
    if _version(document) == 2:
        host = document.get("host")
        schemes = document.get("schemes") or ["https"]
        base = document.get("basePath") or ""
        if isinstance(host, str) and "https" in schemes and isinstance(base, str):
            return f"https://{host}{base}".rstrip("/")
        return None
    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], Mapping):
        url = servers[0].get("url")
        if isinstance(url, str) and url.startswith("https://") and "{" not in url:
            return url.rstrip("/")
    return None


def _same_route(path: str) -> str:
    """A path template with its parameters unnamed: /v1/customers/{id} and /{customer} match."""
    return _PARAMETER.sub("{}", path)


def _responses(catalog: _Catalog) -> dict[tuple[str, str], tuple[str | None, _Child | None]]:
    """Every operation's success status and response schema, keyed by method and path."""
    document = catalog.document
    version = _version(document)
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise OpenAPIError("the document has no paths")
    found: dict[tuple[str, str], tuple[str | None, _Child | None]] = {}
    for path, item in paths.items():
        if isinstance(item, Mapping) and isinstance(item.get("$ref"), str):
            item = catalog.resolve(item["$ref"])
        if not isinstance(path, str) or not isinstance(item, Mapping):
            continue
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, Mapping):
                continue
            status, schema = _success_response(catalog, operation, version)
            found[(method.upper(), path)] = (
                status,
                None if schema is None else catalog.child(schema),
            )
    return found


def operations(document: Mapping[str, Any]) -> dict[tuple[str, str], Operation]:
    """Every operation in the document, keyed by method and path template."""
    catalog = _Catalog(document, _Budget())
    return {
        key: Operation(key[0], key[1], status, None if child is None else catalog.shape(child))
        for key, (status, child) in _responses(catalog).items()
    }


def _success_response(
    catalog: _Catalog, operation: Mapping[str, Any], version: int
) -> tuple[str | None, Any]:
    """The first 2xx response with a JSON body, or the first 2xx response at all."""
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return None, None
    by_code = {str(code).upper(): response for code, response in responses.items()}
    codes = sorted(code for code in by_code if _STATUS.fullmatch(code) and code.startswith("2"))
    codes += [code for code in ("2XX",) if code in by_code]
    for code in codes:
        response = by_code[code]
        if isinstance(response, Mapping) and isinstance(response.get("$ref"), str):
            found = catalog.resolve(response["$ref"])
            if found is None:
                return code, response  # read as a schema: unknown, like the reference
            response = found
        schema = _json_schema(response, version) if isinstance(response, Mapping) else None
        if schema is not None:
            return code, schema
    return (codes[0], None) if codes else (None, None)


def _json_schema(response: Mapping[str, Any], version: int) -> Any:
    if version == 2:
        return response.get("schema")
    content = response.get("content")
    if not isinstance(content, Mapping):
        return None
    for media, entry in content.items():
        kind = str(media).split(";", 1)[0].strip()
        if _JSON_MEDIA.fullmatch(kind) and isinstance(entry, Mapping) and "schema" in entry:
            return entry["schema"]
    return None


# -- diff -------------------------------------------------------------------------


def _spec_wording(change: RawChange) -> RawChange:
    """Say what the spec declares, where the diff speaks of observed responses."""
    if change.kind is ChangeKind.ENUM_VALUE_ADDED and change.after == _FREE_TEXT:
        return replace(change, after="any string")
    if change.kind is ChangeKind.FIELD_NOW_OPTIONAL:
        return replace(change, before="required", after="optional")
    return change


def diff_operations(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    expand_depth: int | None = 1,
    max_changes: int | None = None,
) -> SpecDiff:
    """What changed in every operation's success response from ``old`` to ``new``.

    ``expand_depth`` caps how many "id or object" fields a path may go through (None for
    no cap), and ``max_changes`` how many changes one operation reports.
    """
    budget = _Budget()
    old_catalog, new_catalog = _Catalog(old, budget), _Catalog(new, budget)
    before, after = _responses(old_catalog), _responses(new_catalog)
    routes = {(method, _same_route(path)): (method, path) for method, path in after}
    comparison = _Comparison(old_catalog, new_catalog, budget)
    found: list[OperationDiff] = []
    removed: list[str] = []
    for (method, old_path), (old_status, old_child) in before.items():
        match = routes.get((method, _same_route(old_path)))
        if match is None:
            removed.append(f"{method} {old_path}")
            continue
        path = match[1]
        new_status, new_child = after[match]
        raw: list[RawChange] = []
        old_code, new_code = old_status or "", new_status or ""
        if _STATUS.fullmatch(old_code) and _STATUS.fullmatch(new_code) and old_code != new_code:
            raw.append(RawChange("$", ChangeKind.STATUS_CHANGED, before=old_code, after=new_code))
        if old_child is not None and new_child is None:
            raw.append(
                RawChange(
                    "$",
                    ChangeKind.CONTENT_TYPE_CHANGED,
                    before="application/json",
                    after="no JSON body",
                )
            )
        elif old_child is not None and new_child is not None:
            raw += comparison.emit(old_child, new_child, expand_depth=expand_depth)
        omitted = 0
        if max_changes is not None and len(raw) > max_changes:
            omitted, raw = len(raw) - max_changes, raw[:max_changes]
        if raw:
            found.append(OperationDiff(method, path, [classify(c) for c in raw], omitted))
    return SpecDiff(found, removed, sorted(old_catalog.unresolved | new_catalog.unresolved))
