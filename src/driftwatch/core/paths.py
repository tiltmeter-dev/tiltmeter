"""Paths into a JSON body, and the patterns used to ignore parts of it.

A path starts at ``$`` (the response body) and adds one segment per step:
``.name`` for an object property, ``["odd key"]`` for a property whose name is not
a plain identifier, and ``[*]`` for "every item of this array". For example
``$.data[*].customer.email``.

Patterns use the same notation plus three conveniences: ``*`` and ``?`` glob
characters inside a dotted property name, ``..`` to skip any number of levels,
and prefix matching, so ``$.metadata`` also covers everything below it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Final


class ArrayItems:
    """Marker segment for "every item of an array", rendered as ``[*]``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ITEMS"

    def __reduce__(self) -> str:
        return "ITEMS"


ITEMS: Final = ArrayItems()

type Segment = str | ArrayItems


class PathSyntaxError(ValueError):
    """A path or pattern that cannot be parsed."""


class _Descend:
    __slots__ = ()


_DESCEND: Final = _Descend()


@dataclass(frozen=True, slots=True)
class _Name:
    value: str
    literal: bool  # written in brackets: compared exactly, never globbed


type _Token = _Name | ArrayItems | _Descend

_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def format_path(segments: Iterable[Segment]) -> str:
    """Render segments as a path string such as ``$.data[*]["content-type"]``."""
    parts = ["$"]
    for segment in segments:
        if isinstance(segment, ArrayItems):
            parts.append("[*]")
        elif _IDENTIFIER.fullmatch(segment):
            parts.append(f".{segment}")
        else:
            parts.append(f"[{json.dumps(segment, ensure_ascii=False)}]")
    return "".join(parts)


def parse_path(text: str) -> list[Segment]:
    """Parse a concrete path (no wildcards) back into segments."""
    segments: list[Segment] = []
    for token in _tokenize(text, allow_patterns=False):
        if isinstance(token, _Name):
            segments.append(token.value)
        elif isinstance(token, ArrayItems):
            segments.append(ITEMS)
    return segments


def _tokenize(text: str, *, allow_patterns: bool) -> list[_Token]:
    text = text.strip()
    if not text.startswith("$"):
        raise PathSyntaxError(f"a path must start with '$': {text!r}")
    tokens: list[_Token] = []
    index, size = 1, len(text)
    while index < size:
        if text.startswith("..", index):
            if not allow_patterns:
                raise PathSyntaxError(f"'..' is only allowed in ignore patterns: {text!r}")
            tokens.append(_DESCEND)
            index += 2
            if index < size and text[index] not in ".[":
                name, index = _read_name(text, index)
                tokens.append(_Name(name, literal=False))
            continue
        char = text[index]
        if char == ".":
            name, index = _read_name(text, index + 1)
            tokens.append(_Name(name, literal=not allow_patterns))
        elif char == "[":
            if text.startswith("[*]", index):
                tokens.append(ITEMS)
                index += 3
            else:
                name, index = _read_quoted(text, index + 1)
                tokens.append(_Name(name, literal=True))
        else:
            raise PathSyntaxError(f"unexpected {char!r} at position {index} in {text!r}")
    return tokens


def _read_name(text: str, start: int) -> tuple[str, int]:
    end = start
    while end < len(text) and text[end] not in ".[":
        end += 1
    if end == start:
        raise PathSyntaxError(f"empty property name at position {start} in {text!r}")
    return text[start:end], end


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] not in "\"'":
        raise PathSyntaxError(f"expected [*] or a quoted name at position {start - 1} in {text!r}")
    quote = text[start]
    end = start + 1
    while end < len(text) and text[end] != quote:
        end += 2 if text[end] == "\\" and quote == '"' else 1
    if end >= len(text):
        raise PathSyntaxError(f"unterminated quoted name in {text!r}")
    raw = text[start : end + 1]
    if quote == '"':
        try:
            name = json.loads(raw)
        except ValueError as exc:
            raise PathSyntaxError(f"invalid quoted name {raw} in {text!r}") from exc
    else:
        name = raw[1:-1]
    if end + 1 >= len(text) or text[end + 1] != "]":
        raise PathSyntaxError(f"expected ']' after {raw} in {text!r}")
    return str(name), end + 2


def _match(tokens: Sequence[_Token], segments: Sequence[Segment], ti: int, si: int) -> bool:
    if ti == len(tokens):
        return True  # prefix match: the pattern covers everything below it
    token = tokens[ti]
    if isinstance(token, _Descend):
        return any(_match(tokens, segments, ti + 1, k) for k in range(si, len(segments) + 1))
    if si == len(segments):
        return False
    segment = segments[si]
    if isinstance(token, ArrayItems):
        matched = isinstance(segment, ArrayItems)
    elif isinstance(segment, ArrayItems):
        matched = False
    elif token.literal:
        matched = segment == token.value
    else:
        matched = fnmatchcase(segment, token.value)
    return matched and _match(tokens, segments, ti + 1, si + 1)


class PathPattern:
    """One ignore pattern, e.g. ``$.data[*].metadata`` or ``$..updated_at``."""

    __slots__ = ("_tokens", "text")

    def __init__(self, text: str) -> None:
        self.text = text.strip()
        self._tokens = _tokenize(self.text, allow_patterns=True)

    def matches(self, segments: Sequence[Segment]) -> bool:
        """True when the pattern covers the path or one of its ancestors."""
        return _match(self._tokens, segments, 0, 0)

    def __repr__(self) -> str:
        return f"PathPattern({self.text!r})"


class PathMatcher:
    """A set of ignore patterns."""

    __slots__ = ("_patterns",)

    def __init__(self, patterns: Iterable[str | PathPattern] = ()) -> None:
        self._patterns = tuple(
            p if isinstance(p, PathPattern) else PathPattern(p) for p in patterns
        )

    def matches(self, segments: Sequence[Segment]) -> bool:
        """True when any pattern covers the path."""
        return any(pattern.matches(segments) for pattern in self._patterns)

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def __repr__(self) -> str:
        return f"PathMatcher({[p.text for p in self._patterns]!r})"
