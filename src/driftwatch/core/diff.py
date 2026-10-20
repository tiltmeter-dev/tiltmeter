"""Compare what an API returns now with the baseline learned from its history.

The baseline is a merge of many past responses, so the diff asks one question at
every location: *does the latest observation contradict what we know?* A field
that was optional and is absent now is fine; a field that was in every object
and is absent now was removed. A narrower observation (no nulls this time) is
never a change, a wider one (a null where there never was one) is.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from difflib import SequenceMatcher

from driftwatch.core.models import ChangeKind, RawChange
from driftwatch.core.paths import ITEMS, PathMatcher, Segment, format_path
from driftwatch.core.shape import MAX_TRACKED_VALUES, Shape, core_types

_MAX_LISTED_VALUES = 8
_RENAME_SIMILARITY = 0.7


def diff_shapes(
    baseline: Shape, observed: Shape, *, ignore: PathMatcher | None = None
) -> list[RawChange]:
    """Every way ``observed`` contradicts ``baseline``, in traversal order."""
    found: list[RawChange] = []
    _diff_node(baseline, observed, (), ignore or PathMatcher(), found)
    return annotate_renames(found)


def _diff_node(
    baseline: Shape,
    observed: Shape,
    segments: tuple[Segment, ...],
    ignore: PathMatcher,
    out: list[RawChange],
) -> None:
    if observed.count == 0 or ignore.matches(segments):
        return
    path = format_path(segments)
    new_types = set(observed.types) - set(baseline.types)
    if "integer" in new_types and "number" in baseline.types:
        new_types.discard("integer")  # integers are valid numbers
    if "null" in new_types:
        new_types.discard("null")
        out.append(
            RawChange(
                path,
                ChangeKind.NULLABLE_INTRODUCED,
                before=baseline.label,
                after=observed.label,
                segments=segments,
                types=observed.type_set,
            )
        )
    if new_types:
        out.append(
            RawChange(
                path,
                ChangeKind.TYPE_CHANGED,
                before=baseline.label,
                after=observed.label,
                segments=segments,
                types=observed.type_set,
            )
        )
    if baseline.object_count and observed.object_count:
        _diff_properties(baseline, observed, segments, ignore, out)
    if baseline.items is not None and observed.items is not None:
        _diff_node(baseline.items, observed.items, (*segments, ITEMS), ignore, out)
    _diff_enum(baseline, observed, path, segments, out)


def _diff_properties(
    baseline: Shape,
    observed: Shape,
    segments: tuple[Segment, ...],
    ignore: PathMatcher,
    out: list[RawChange],
) -> None:
    for name in sorted(baseline.properties.keys() | observed.properties.keys()):
        child_segments = (*segments, name)
        if ignore.matches(child_segments):
            continue
        before = baseline.properties.get(name)
        after = observed.properties.get(name)
        child_path = format_path(child_segments)
        if before is None:
            if after is not None:
                out.append(
                    RawChange(
                        child_path,
                        ChangeKind.FIELD_ADDED,
                        after=after.label,
                        segments=child_segments,
                        types=after.type_set,
                    )
                )
            continue
        required = baseline.is_required(name)
        if after is None:
            if required:
                out.append(
                    RawChange(
                        child_path,
                        ChangeKind.FIELD_REMOVED,
                        before=before.label,
                        segments=child_segments,
                        types=before.type_set,
                    )
                )
            continue
        if required and after.count < observed.object_count:
            missing = observed.object_count - after.count
            out.append(
                RawChange(
                    child_path,
                    ChangeKind.FIELD_NOW_OPTIONAL,
                    before="always present",
                    after=f"missing from {missing} of {observed.object_count} objects",
                    segments=child_segments,
                    types=after.type_set,
                )
            )
        _diff_node(before, after, child_segments, ignore, out)


def _diff_enum(
    baseline: Shape,
    observed: Shape,
    path: str,
    segments: tuple[Segment, ...],
    out: list[RawChange],
) -> None:
    known = baseline.enum_values()
    if known is None or "string" not in observed.types:
        return
    if not observed.categorical:
        after = "a value that does not look like an enum member (not shown)"
    elif observed.values is None:
        after = f"more than {MAX_TRACKED_VALUES} distinct values"
    else:
        fresh = sorted(set(observed.values) - set(known))
        if not fresh:
            return
        after = ", ".join(fresh[:_MAX_LISTED_VALUES])
        if len(fresh) > _MAX_LISTED_VALUES:
            after += f" (+{len(fresh) - _MAX_LISTED_VALUES} more)"
    out.append(
        RawChange(
            path,
            ChangeKind.ENUM_VALUE_ADDED,
            before=", ".join(known),
            after=after,
            segments=segments,
            types=observed.type_set,
        )
    )


def annotate_renames(changes: Sequence[RawChange]) -> list[RawChange]:
    """Pair removed and added fields that look like the same data under a new name.

    A pair needs the same parent object and the same non-null type. It is
    accepted when the names are similar (one contains the other once case and
    separators are ignored, or they are mostly the same letters), or when it is
    the only possible pair under that parent.
    """
    result = list(changes)
    removed = [i for i, c in enumerate(changes) if c.kind is ChangeKind.FIELD_REMOVED]
    added_by_parent: dict[tuple[Segment, ...], list[int]] = defaultdict(list)
    for i, change in enumerate(changes):
        if change.kind is ChangeKind.FIELD_ADDED and change.segments:
            added_by_parent[change.segments[:-1]].append(i)
    if not removed or not added_by_parent:
        return result
    used: set[int] = set()
    for i in removed:
        gone = changes[i]
        if not gone.segments or not isinstance(gone.segments[-1], str):
            continue
        parent, kind = gone.segments[:-1], core_types(gone.types)
        candidates = [
            j
            for j in added_by_parent.get(parent, [])
            if j not in used and core_types(changes[j].types) == kind
        ]
        if not candidates:
            continue
        rivals = [
            k
            for k in removed
            if changes[k].segments[:-1] == parent and core_types(changes[k].types) == kind
        ]
        scored = sorted(
            ((_similarity(gone.segments[-1], changes[j].segments[-1]), j) for j in candidates),
            key=lambda pair: (-pair[0], pair[1]),
        )
        score, best = scored[0]
        if score < _RENAME_SIMILARITY and not (len(candidates) == 1 and len(rivals) == 1):
            continue
        used.add(best)
        replacement = changes[best]
        result[i] = replace(gone, hint=f"possibly renamed to {replacement.path}")
        result[best] = replace(replacement, hint=f"possibly replaces {gone.path}")
    return result


def _normalize(name: Segment) -> str:
    return "".join(char for char in str(name).lower() if char.isalnum())


def _similarity(left: Segment, right: Segment) -> float:
    a, b = _normalize(left), _normalize(right)
    if a and b and (a in b or b in a):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
