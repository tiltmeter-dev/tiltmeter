from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from tiltmeter.core.openapi import OpenAPIError, base_url, diff_operations, operations

SPECS = Path(__file__).parent.parent / "fixtures" / "specs"


def load(case: str, name: str) -> Any:
    return yaml.safe_load((SPECS / case / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", sorted(p.name for p in SPECS.iterdir() if p.is_dir()))
def test_golden_specs(case: str) -> None:
    """Each folder holds two versions of a spec and the changes between them."""
    old, new = load(case, "old.yaml"), load(case, "new.yaml")
    diff = diff_operations(old, new)
    got = {
        "base_url": base_url(new),
        "operations": [
            {
                "method": operation.method,
                "path": operation.path,
                "changes": [
                    {
                        key: value
                        for key, value in {
                            "path": c.path,
                            "kind": c.kind.value,
                            "severity": c.severity.value,
                            "before": c.before,
                            "after": c.after,
                            "hint": c.hint,
                        }.items()
                        if value is not None
                    }
                    for c in operation.changes
                ],
            }
            for operation in diff.operations
        ],
        "removed": diff.removed,
        **({"unresolved": diff.unresolved} if diff.unresolved else {}),
    }
    assert got == json.loads((SPECS / case / "expected.json").read_text(encoding="utf-8"))


def spec(schema: Any, *, responses: Any = None) -> dict[str, Any]:
    content = {"application/json": {"schema": schema}}
    return {
        "openapi": "3.1.0",
        "paths": {"/x": {"get": {"responses": responses or {"200": {"content": content}}}}},
        "components": {"schemas": {"Node": {"type": "object"}}},
    }


def test_identical_specs_have_no_changes() -> None:
    document = load("rename_and_retype", "old.yaml")
    assert diff_operations(document, document).operations == []


def test_the_success_response_is_the_first_2xx_with_json() -> None:
    responses = {
        "default": {"content": {"application/json": {"schema": {"type": "string"}}}},
        "204": {"description": "nothing"},
        "201": {"content": {"application/problem+json": {"schema": {"type": "object"}}}},
    }
    [operation] = operations(spec({}, responses=responses)).values()
    assert operation.status == "201"
    assert operation.shape is not None
    assert operation.shape.label == "object"
    [no_body] = operations(spec({}, responses={"204": {"description": "gone"}})).values()
    assert (no_body.status, no_body.shape) == ("204", None)


def test_a_response_that_stops_being_json_is_reported() -> None:
    old = spec({"type": "object"})
    new = spec({}, responses={"200": {"content": {"text/html": {"schema": {}}}}})
    [operation] = diff_operations(old, new).operations
    assert [(c.kind.value, c.after) for c in operation.changes] == [
        ("content_type_changed", "no JSON body")
    ]


def components(**schemas: Any) -> dict[str, Any]:
    """A document whose GET /x returns the first schema given."""
    first = next(iter(schemas))
    document = spec({"$ref": f"#/components/schemas/{first}"})
    document["components"]["schemas"] = schemas
    return document


def changes(old: dict[str, Any], new: dict[str, Any], **options: Any) -> list[tuple[str, str]]:
    return [
        (c.path, c.kind.value)
        for operation in diff_operations(old, new, **options).operations
        for c in operation.changes
    ]


def test_schemas_that_contain_themselves_are_compared_once() -> None:
    children = {"type": "array", "items": {"$ref": "#/components/schemas/Tree"}}
    old = components(Tree={"type": "object", "properties": {"children": children}})
    new = components(
        Tree={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}, "children": children},
        }
    )
    assert changes(old, new) == [("$.id", "field_added")]  # not again under $.children[*]
    [operation] = operations(new).values()
    assert operation.shape is not None
    assert operation.shape.label == "object"  # named schemas stay opaque on their own


def test_expanded_objects_are_followed_one_level_by_default() -> None:
    old, new = load("expandable_fields", "old.yaml"), load("expandable_fields", "new.yaml")
    assert ("$.a.a.total", "type_changed") not in changes(old, new)
    assert ("$.a.a.total", "type_changed") in changes(old, new, expand_depth=None)
    flat = changes(old, new, expand_depth=0)
    assert ("$.status", "enum_value_added") in flat
    assert ("$.a.name", "field_removed") not in flat


def test_a_reference_and_the_same_schema_inline_are_the_same() -> None:
    item = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
    by_reference = components(Item=item)
    inline = spec(item)
    assert changes(by_reference, inline) == []
    assert changes(inline, by_reference) == []


def test_renaming_a_named_schema_changes_nothing() -> None:
    customer = {"type": "object", "properties": {"id": {"type": "string"}}}
    assert changes(components(Customer=customer), components(Client=customer)) == []


@pytest.mark.parametrize("wrapper", ["anyOf", "allOf", "oneOf"])
def test_nullable_references_are_read_as_nullable(wrapper: str) -> None:
    thing = {"type": "object", "properties": {"id": {"type": "string"}}}
    ref = {"$ref": "#/components/schemas/Thing"}
    nullable = {wrapper: [ref], "nullable": True}
    old = components(Root={"type": "object", "properties": {"thing": ref}}, Thing=thing)
    new = components(Root={"type": "object", "properties": {"thing": nullable}}, Thing=thing)
    assert changes(old, new) == [("$.thing", "nullable_introduced")]


def test_a_named_schema_reached_twice_reports_its_changes_once() -> None:
    root = {
        "type": "object",
        "properties": {
            "first": {"$ref": "#/components/schemas/Code"},
            "second": {"$ref": "#/components/schemas/Code"},
        },
    }
    old = components(Root=root, Code={"type": "string"})
    new = components(Root=root, Code={"type": "integer"})
    assert changes(old, new) == [("$.first", "type_changed")]


def test_too_many_changes_are_counted_not_listed() -> None:
    old = spec({"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(5)}})
    new = spec({"type": "object", "properties": {}})
    [operation] = diff_operations(old, new, max_changes=2).operations
    assert [c.path for c in operation.changes] == ["$.f0", "$.f1"]
    assert operation.omitted == 3


def test_huge_specs_stay_fast() -> None:
    def ref(i: int) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/S{i % 1500}"}

    schemas = {
        f"S{i}": {
            "type": "object",
            "properties": {
                "next": {"anyOf": [{"type": "string"}, ref(i + 1)]},
                "other": ref(i * 7),
                "name": {"type": "string"},
            },
        }
        for i in range(1500)
    }
    old = components(**schemas)
    new = components(**{**schemas, "S0": {**schemas["S0"], "required": ["name"]}})
    started = time.monotonic()
    assert changes(old, new) == []  # S0 only gains a requirement, which narrows it
    assert time.monotonic() - started < 5


def test_free_strings_replacing_an_enum_read_as_any_string() -> None:
    old = spec({"type": "object", "properties": {"s": {"type": "string", "enum": ["a", "b"]}}})
    new = spec({"type": "object", "properties": {"s": {"type": "string"}}})
    [operation] = diff_operations(old, new).operations
    assert [(c.kind.value, c.after) for c in operation.changes] == [
        ("enum_value_added", "any string")
    ]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"paths": {}}, "OpenAPI 3 or Swagger 2.0"),
        ({"openapi": "3.0.0"}, "no paths"),
    ],
)
def test_unreadable_documents(document: dict[str, Any], message: str) -> None:
    with pytest.raises(OpenAPIError, match=message):
        operations(document)


@pytest.mark.parametrize("ref", ["other.yaml#/Customer", "#/components/schemas/Missing"])
def test_references_that_cannot_be_followed_compare_as_unknown(ref: str) -> None:
    known = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    for old, new in [(known, {"$ref": ref}), ({"$ref": ref}, known)]:
        diff = diff_operations(spec(old), spec(new))
        assert diff.operations == []
        assert diff.unresolved == [ref]
    [operation] = operations(spec({"$ref": ref})).values()
    assert operation.shape is not None


def test_base_urls() -> None:
    assert base_url({"openapi": "3.0.0", "servers": [{"url": "https://a.test/v1/"}]}) == (
        "https://a.test/v1"
    )
    assert base_url({"openapi": "3.0.0", "servers": [{"url": "/v1"}]}) is None
    assert base_url({"openapi": "3.0.0", "servers": [{"url": "https://{region}.a.test"}]}) is None
    assert base_url({"openapi": "3.0.0"}) is None
    assert base_url({"swagger": "2.0", "host": "a.test", "schemes": ["http"]}) is None


def test_odd_status_codes_are_not_numbers() -> None:
    content = {"content": {"application/json": {"schema": {"type": "object"}}}}
    [operation] = operations(spec({}, responses={"²⁰⁰": content, "201": content})).values()
    assert operation.status == "201"


def test_fan_out_no_longer_explodes_but_the_budget_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tiltmeter.core.openapi.MAX_NODES", 1_000)
    old, new = load("expandable_fields", "old.yaml"), load("expandable_fields", "new.yaml")
    assert diff_operations(old, new).operations  # fits: each schema is built once
    wide = spec(
        {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(2000)}}
    )
    with pytest.raises(OpenAPIError, match="more than 1000 nodes"):
        diff_operations(wide, wide)


def test_results_do_not_depend_on_the_order_of_paths() -> None:
    schemas: dict[str, Any] = {}
    for i in range(40):
        properties: dict[str, Any] = {"id": {"type": "string"}}
        if i < 39:
            properties["next"] = {
                "anyOf": [{"type": "string"}, {"$ref": f"#/components/schemas/S{i + 1}"}]
            }
        schemas[f"S{i}"] = {"type": "object", "required": ["id"], "properties": properties}

    def returning(schema: dict[str, Any]) -> Any:
        return spec(schema)["paths"]["/x"]

    old = components(**schemas)
    old["paths"] = {
        "/a": returning({"anyOf": [{"type": "string"}, {"$ref": "#/components/schemas/S0"}]}),
        "/b": returning({"anyOf": [{"type": "string"}, {"$ref": "#/components/schemas/S10"}]}),
        "/c": returning({"$ref": "#/components/schemas/S15"}),
    }
    new = {**old, "paths": dict(reversed(list(old["paths"].items())))}
    assert diff_operations(old, new).operations == []


def test_requirements_listed_in_another_part_keep_refs_as_refs() -> None:
    def expandable(name: str) -> dict[str, Any]:
        return {"anyOf": [{"type": "string"}, {"$ref": f"#/components/schemas/{name}"}]}

    def document(with_zip: bool) -> dict[str, Any]:
        address: dict[str, Any] = {"type": "object", "properties": {"line1": {"type": "string"}}}
        if with_zip:
            address["properties"]["zip"] = {"type": "string"}
            address["required"] = ["zip"]
        base = {"type": "object", "properties": {"customer": expandable("Customer")}}
        return components(
            Sub={"allOf": [{"$ref": "#/components/schemas/Base"}, {"required": ["customer"]}]},
            Base=base,
            Customer={"type": "object", "properties": {"address": expandable("Address")}},
            Address=address,
        )

    assert changes(document(False), document(True)) == []  # two "id or object" hops away
    assert changes(document(False), document(True), expand_depth=None) == [
        ("$.customer.address.zip", "field_added")
    ]


def test_an_enum_of_one_value_that_gains_more() -> None:
    def document(values: list[str] | None) -> dict[str, Any]:
        kind: dict[str, Any] = {"type": "string"}
        if values is not None:
            kind["enum"] = values
        return spec({"type": "object", "required": ["kind"], "properties": {"kind": kind}})

    [operation] = diff_operations(document(["card"]), document(["card", "bank"])).operations
    assert [(c.kind.value, c.before, c.after) for c in operation.changes] == [
        ("enum_value_added", "card", "bank")
    ]
    [free] = diff_operations(document(["card"]), document(None)).operations
    assert [c.after for c in free.changes] == ["any string"]
    assert diff_operations(document(["card", "bank"]), document(["card"])).operations == []


def test_renamed_path_parameters_are_the_same_operation() -> None:
    ok = spec({"type": "object", "properties": {"id": {"type": "string"}}})["paths"]["/x"]
    changed = spec({"type": "object", "properties": {"id": {"type": "integer"}}})["paths"]["/x"]
    old = {"openapi": "3.1.0", "paths": {"/v1/customers/{id}": ok}}
    new = {"openapi": "3.1.0", "paths": {"/v1/customers/{customer}": changed}}
    diff = diff_operations(old, new)
    assert diff.removed == []
    [operation] = diff.operations
    assert operation.path == "/v1/customers/{customer}"
    assert [c.path for c in operation.changes] == ["$.id"]
