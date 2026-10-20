from __future__ import annotations

import pickle

import pytest

from driftwatch.core.paths import (
    ITEMS,
    PathMatcher,
    PathPattern,
    PathSyntaxError,
    format_path,
    parse_path,
)


@pytest.mark.parametrize(
    ("segments", "text"),
    [
        ([], "$"),
        (["data", ITEMS, "id"], "$.data[*].id"),
        ([ITEMS, "name"], "$[*].name"),
        (["content-type"], '$["content-type"]'),
        (["a b", "ñandú", "_ok1"], '$["a b"]["ñandú"]._ok1'),
        (['say "hi"'], '$["say \\"hi\\""]'),
    ],
)
def test_format_and_parse_round_trip(segments: list[object], text: str) -> None:
    assert format_path(segments) == text  # type: ignore[arg-type]
    assert parse_path(text) == segments


@pytest.mark.parametrize(
    "text",
    ["data", "$.", "$[1]", '$["unterminated', '$["a"', "$..x", "$x", "$.a[*", "$['a'"],
)
def test_parse_path_rejects_bad_syntax(text: str) -> None:
    with pytest.raises(PathSyntaxError):
        parse_path(text)


def test_parse_path_accepts_single_quotes() -> None:
    assert parse_path("$['odd.key'][*]") == ["odd.key", ITEMS]


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("$.email", ["email"], True),
        ("$.email", ["emails"], False),
        ("$.metadata", ["metadata", "plan", "tier"], True),  # prefix covers the subtree
        ("$.data[*].created", ["data", ITEMS, "created"], True),
        ("$.data[*].created", ["data", "created"], False),  # [*] only matches array items
        ("$.data.*", ["data", "anything"], True),
        ("$.data.*", ["data", ITEMS], False),  # * is a property glob, not items
        ("$.data[*].created_*", ["data", ITEMS, "created_at"], True),
        ("$.?d", ["id"], True),
        ("$..updated_at", ["updated_at"], True),
        ("$..updated_at", ["a", ITEMS, "b", "updated_at"], True),
        ("$..updated_at", ["a", "updated_at_utc"], False),
        ("$..[*]", ["a", ITEMS, "x"], True),
        ('$["a.*"]', ["a.*"], True),
        ('$["a.*"]', ["a.b"], False),  # bracketed names are literal
        ("$", [], True),
        ("$", ["anything", ITEMS], True),
    ],
)
def test_pattern_matching(pattern: str, path: list[object], expected: bool) -> None:
    assert PathPattern(pattern).matches(path) is expected  # type: ignore[arg-type]


def test_matcher_combines_patterns_and_is_falsy_when_empty() -> None:
    matcher = PathMatcher(["$.a", PathPattern("$..b")])
    assert matcher
    assert matcher.matches(["a", "x"])
    assert matcher.matches(["z", "b"])
    assert not matcher.matches(["z"])
    empty = PathMatcher()
    assert not empty
    assert not empty.matches([])


def test_items_marker_survives_pickling() -> None:
    assert pickle.loads(pickle.dumps(ITEMS)) is ITEMS
    assert repr(ITEMS) == "ITEMS"
