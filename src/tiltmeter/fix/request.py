"""Turn a stored check into the request the fix agent works from.

Only changes that code can follow become targets: field removals, renames, type
and nullability changes. A plain new field breaks nobody, and status, media type
and latency changes are not something a code edit can repair.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from tiltmeter.core.diff import RENAMED_TO
from tiltmeter.core.models import TRANSPORT_KINDS, Change, ChangeKind, CheckResult
from tiltmeter.core.paths import PathSyntaxError, parse_path
from tiltmeter.fix.models import FixRequest, FixTarget
from tiltmeter.llm.explainer import build_request

if TYPE_CHECKING:
    from tiltmeter.config import EndpointConfig
    from tiltmeter.core.shape import Shape

_SKIPPED_KINDS = TRANSPORT_KINDS | {ChangeKind.FIELD_ADDED}
_RESOURCE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_VERSION = re.compile(r"v\d+(\.\d+)*", re.IGNORECASE)


def field_name(path: str) -> str | None:
    """The property name a path ends with, or None for the body or an array item."""
    try:
        segments = parse_path(path)
    except PathSyntaxError:
        return None
    last = segments[-1] if segments else None
    return last if isinstance(last, str) else None


def renamed_to(change: Change, renames: dict[str, str]) -> str | None:
    """Where a removed field probably went: the diff's hint first, then Claude's reading."""
    if change.hint and change.hint.startswith(RENAMED_TO):
        return change.hint.removeprefix(RENAMED_TO)
    return renames.get(change.path)


def build_targets(check: CheckResult) -> list[FixTarget]:
    renames = {}
    if check.explanation is not None:
        renames = {r.before_path: r.after_path for r in check.explanation.probable_renames}
    targets = []
    for change in check.changes:
        if change.kind in _SKIPPED_KINDS:
            continue
        new_path = renamed_to(change, renames) if change.kind is ChangeKind.FIELD_REMOVED else None
        targets.append(
            FixTarget(
                path=change.path,
                kind=change.kind,
                severity=change.severity,
                before=change.before,
                after=change.after,
                renamed_to=new_path,
                field=field_name(change.path),
                new_field=field_name(new_path) if new_path else None,
            )
        )
    return targets


def search_terms(endpoint: EndpointConfig, targets: Sequence[FixTarget]) -> list[str]:
    """Strings worth grepping for first: the old field names and the API's address."""
    terms = [target.field for target in targets if target.field]
    parts = urlsplit(endpoint.display_url())
    if parts.hostname and "${" not in parts.hostname:
        terms.append(parts.hostname)
    for part in reversed(parts.path.split("/")):
        # The last name in the path, skipping ids, versions and placeholders.
        if _RESOURCE.fullmatch(part) and not _VERSION.fullmatch(part) and len(part) <= 40:
            terms.append(part)
            break
    return list(dict.fromkeys(terms))


def build_fix_request(
    endpoint: EndpointConfig,
    check: CheckResult,
    *,
    baseline: Shape | None,
    observed: Shape | None,
) -> FixRequest:
    """Collect the targets, the affected objects and the explanation of one check."""
    if check.check_id is None:
        raise ValueError("the check has not been saved")
    targets = build_targets(check)
    shapes = build_request(endpoint, check.changes, baseline=baseline, observed=observed)
    return FixRequest(
        check_id=check.check_id,
        endpoint_id=endpoint.id,
        method=endpoint.method,
        url=endpoint.display_url(),
        description=endpoint.description,
        targets=targets,
        affected=list(shapes.affected),
        explanation=check.explanation,
        search_terms=search_terms(endpoint, targets),
    )
