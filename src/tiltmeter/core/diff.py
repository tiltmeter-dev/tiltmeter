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
from typing import Final

from tiltmeter.core.models import ChangeKind, RawChange
from tiltmeter.core.paths import ITEMS, PathMatcher, Segment, format_path
from tiltmeter.core.shape import MAX_TRACKED_VALUES, Shape, core_types

_MAX_LISTED_VALUES = 8
_RENAME_SIMILARITY = 0.7

RENAMED_TO: Final = "possibly renamed to "
"""Hint prefix on a removed field that probably lives on under the path that follows."""
REPLACES: Final = "possibly replaces "
"""Hint prefix on an added field that probably replaces the path that follows."""
MOVED_TO: Final = "possibly moved to "
"""Hint prefix on a removed endpoint that probably lives on as the method and path that
follow, such as ``GET /v1/playlists/{id}/items``."""


def diff_shapes(
    baseline: Shape,
    observed: Shape,
    *,
    ignore: PathMatcher | None = None,
    complete: bool = False,
    renames: bool = True,
) -> list[RawChange]:
    """Every way ``observed`` contradicts ``baseline``, in traversal order.

    ``complete`` says that ``observed`` lists every field the API can return, as a spec
    does, so an optional field that is gone was removed rather than absent this time.
    ``renames=False`` leaves out the rename hints, for callers that pair fields later.
    """
    found: list[RawChange] = []
    _diff_node(baseline, observed, (), ignore or PathMatcher(), found, complete)
    return annotate_renames(found) if renames else found


def _diff_node(
    baseline: Shape,
    observed: Shape,
    segments: tuple[Segment, ...],
    ignore: PathMatcher,
    out: list[RawChange],
    complete: bool,
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
        _diff_properties(baseline, observed, segments, ignore, out, complete)
    if baseline.items is not None and observed.items is not None:
        _diff_node(baseline.items, observed.items, (*segments, ITEMS), ignore, out, complete)
    _diff_enum(baseline, observed, path, segments, out)


def _diff_properties(
    baseline: Shape,
    observed: Shape,
    segments: tuple[Segment, ...],
    ignore: PathMatcher,
    out: list[RawChange],
    complete: bool,
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
            if required or complete:
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
        _diff_node(before, after, child_segments, ignore, out, complete)


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


def annotate_renames(
    changes: Sequence[RawChange], inside: Sequence[RawChange] = ()
) -> list[RawChange]:
    """Pair removed and added fields that look like the same data under a new name.

    A pair needs the same parent object and the same non-null type. It is
    accepted when the names are similar (one contains the other once case and
    separators are ignored, or they are mostly the same letters), or when it is
    the only possible pair under that parent. A field left without a pair that
    reappears nearby under another parent, with the same name and type, moved
    there (``$.current_period_end`` to ``$.items.data[*].current_period_end``)
    when it is the only field of that name and type to go and the only one to
    appear. Nearby means at most three segments added, removed or replaced.

    ``inside`` lists the fields within added fields, which are not reported on their
    own (a spec's new object is one ``field_added``). They count only as places a field
    moved to, when the added field that holds them sits beside the removed one:
    ``$.subscription`` to ``$.parent.subscription_details.subscription``. That added
    field then says which fields it possibly replaces.
    """
    result = list(changes)
    removed = [i for i, c in enumerate(changes) if c.kind is ChangeKind.FIELD_REMOVED]
    added_by_parent: dict[tuple[Segment, ...], list[int]] = defaultdict(list)
    for i, change in enumerate(changes):
        if change.kind is ChangeKind.FIELD_ADDED and change.segments:
            added_by_parent[change.segments[:-1]].append(i)
    if not removed or not (added_by_parent or inside):
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
        result[i] = replace(gone, hint=f"{RENAMED_TO}{replacement.path}")
        result[best] = replace(replacement, hint=f"{REPLACES}{gone.path}")
    unpaired = {i for i in removed if result[i] is changes[i]}
    _annotate_moves(changes, inside, result, unpaired, used)
    return result


def _annotate_moves(
    changes: Sequence[RawChange],
    inside: Sequence[RawChange],
    result: list[RawChange],
    unpaired: set[int],
    used: set[int],
) -> None:
    """Pair each unpaired removed field with the one added elsewhere under its name."""
    gone: dict[tuple[Segment, frozenset[str]], list[int]] = defaultdict(list)
    appeared: dict[tuple[Segment, frozenset[str]], list[int]] = defaultdict(list)
    pool = [*changes, *inside]  # positions past the changes are fields inside added ones
    for i, change in enumerate(pool):
        if not change.segments or not isinstance(change.segments[-1], str):
            continue
        name = (change.segments[-1], core_types(change.types))
        if i in unpaired:
            gone[name].append(i)
        elif change.kind is ChangeKind.FIELD_ADDED and i not in used:
            appeared[name].append(i)
    holders = (
        {c.segments: k for k, c in enumerate(changes) if c.kind is ChangeKind.FIELD_ADDED}
        if inside
        else {}
    )
    for field, removed in gone.items():
        added = appeared.get(field, [])
        if len(removed) != 1 or len(added) != 1:
            continue
        i, j = removed[0], added[0]
        source, target = changes[i], pool[j]
        if j < len(changes):
            holder: int | None = j
            near = _distance(source.segments, target.segments) <= _MOVE_DISTANCE
        else:
            holder = _holder(holders, target.segments)
            near = holder is not None and changes[holder].segments[:-1] == source.segments[:-1]
        if near and holder is not None:
            result[i] = replace(source, hint=f"{RENAMED_TO}{target.path}")
            _add_replaced(result, holder, source.path)


def _holder(holders: dict[tuple[Segment, ...], int], segments: tuple[Segment, ...]) -> int | None:
    """The reported added field that a field inside an added one lies within."""
    for end in range(1, len(segments)):
        found = holders.get(segments[:end])
        if found is not None:
            return found
    return None


def _add_replaced(result: list[RawChange], index: int, path: str) -> None:
    """Say that the added field at ``index`` possibly replaces ``path``, besides others."""
    change = result[index]
    hint = change.hint
    hint = f"{hint}, {path}" if hint and hint.startswith(REPLACES) else f"{REPLACES}{path}"
    result[index] = replace(change, hint=hint)


# How far a field may move into an object that already existed. A field inside a new
# object is not held to it, since it sits below that object and whatever wraps it: Stripe
# moved `$.payment_intent` to `$.payments.data[*].payment.payment_intent`, four segments
# away. It counts instead only when its new object was added beside the removed field, which
# is stricter: Stripe's `$.last_finalization_error.payment_intent.latest_charge.invoice` did
# not go to `$.payments.data[*].invoice`, though that is three segments away.
_MOVE_DISTANCE: Final = 3


def _distance(left: Sequence[Segment], right: Sequence[Segment]) -> int:
    """How many segments to add, remove or replace to turn one path into the other."""
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _normalize(name: Segment) -> str:
    return "".join(char for char in str(name).lower() if char.isalnum())


def _similarity(left: Segment, right: Segment) -> float:
    a, b = _normalize(left), _normalize(right)
    if a and b and (a in b or b in a):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
