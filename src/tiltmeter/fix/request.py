"""Turn a stored check, or a provider's change notice, into the request the fix agent
works from.

Only changes that code can follow become targets: field removals, renames, type
and nullability changes. A plain new field breaks nobody, and status, media type
and latency changes are not something a code edit can repair.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from tiltmeter.core.classify import default_severity
from tiltmeter.core.diff import RENAMED_TO
from tiltmeter.core.models import TRANSPORT_KINDS, Change, ChangeKind, CheckResult, RawChange
from tiltmeter.core.paths import PathSyntaxError, parse_path
from tiltmeter.core.timeutil import to_iso
from tiltmeter.fix.models import FixApi, FixRequest, FixTarget, NoticeSource
from tiltmeter.llm.explainer import build_request

if TYPE_CHECKING:
    from tiltmeter.config import EndpointConfig
    from tiltmeter.core.shape import Shape
    from tiltmeter.notices.models import ApiChangeNotice, NoticeEndpoint, SpecInfo

_SKIPPED_KINDS = TRANSPORT_KINDS | {ChangeKind.FIELD_ADDED}
_RESOURCE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_VERSION = re.compile(r"v\d+(\.\d+)*", re.IGNORECASE)


def is_target(kind: ChangeKind) -> bool:
    """Whether code that calls the API may have to follow this kind of change."""
    return kind not in _SKIPPED_KINDS


def reads_response(kind: ChangeKind) -> bool:
    """Whether the change's path names a field of the response, which code reads. A
    retired value's path names what requests send, and a removed endpoint's is the body."""
    return kind not in {ChangeKind.VALUE_RETIRED, ChangeKind.ENDPOINT_REMOVED}


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


def url_terms(url: str) -> list[str]:
    """The host of a URL and the last resource name in its path."""
    terms = []
    parts = urlsplit(url)
    if parts.hostname and "${" not in parts.hostname:
        terms.append(parts.hostname)
    for part in reversed(parts.path.split("/")):
        # The last name in the path, skipping ids, versions and placeholders.
        if _RESOURCE.fullmatch(part) and not _VERSION.fullmatch(part) and len(part) <= 40:
            terms.append(part)
            break
    return terms


def search_terms(endpoint: EndpointConfig, targets: Sequence[FixTarget]) -> list[str]:
    """Strings worth grepping for first: the old field names and the API's address."""
    terms = [target.field for target in targets if target.field]
    terms += url_terms(endpoint.display_url())
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
    api = FixApi(
        endpoint_id=endpoint.id,
        method=endpoint.method,
        url=endpoint.display_url(),
        description=endpoint.description,
        targets=targets,
        affected=list(shapes.affected),
    )
    return FixRequest(
        check_id=check.check_id,
        apis=[api],
        explanation=check.explanation,
        search_terms=search_terms(endpoint, targets),
    )


def notice_targets(endpoint: NoticeEndpoint, applies_to: str | None = None) -> list[FixTarget]:
    """The changes of a notice that code has to follow, with the default severity.

    ``applies_to`` is the notice's own, for the changes that do not name theirs."""
    targets = []
    for change in endpoint.changes:
        if not is_target(change.kind):
            continue
        raw = RawChange(change.path, change.kind, before=change.before, after=change.after)
        moved = change.moved_to
        targets.append(
            FixTarget(
                path=change.path,
                kind=change.kind,
                severity=default_severity(raw),
                before=change.before,
                after=change.after,
                renamed_to=change.renamed_to,
                moved_to=f"{moved.method} {moved.url}" if moved is not None else None,
                replaced_by=change.replaced_by,
                field=field_name(change.path) if reads_response(change.kind) else None,
                new_field=field_name(change.renamed_to) if change.renamed_to else None,
                note=change.note,
                applies_to=change.applies_to or applies_to,
            )
        )
    return targets


def _in_use(targets: list[FixTarget], used: set[str | None]) -> list[FixTarget]:
    """The targets without the retired values the code was not found to send."""
    if not used:
        return targets
    return [t for t in targets if t.kind is not ChangeKind.VALUE_RETIRED or t.before in used]


def _relevance(target: FixTarget, read: set[str]) -> tuple[bool, bool, int]:
    """A removed endpoint first, since the rest is about the endpoint it moved to."""
    try:
        depth = len(parse_path(target.path))
    except PathSyntaxError:
        depth = len(target.path)
    gone = target.kind is ChangeKind.ENDPOINT_REMOVED
    return (not gone, target.field not in read, depth)


def build_notice_request(
    notice: ApiChangeNotice,
    *,
    location: str,
    verified: bool,
    candidate_files: Sequence[str] = (),
    endpoints: Sequence[int] | None = None,
    spec: SpecInfo | None = None,
    read: Sequence[str] = (),
    found: Sequence[str] = (),
) -> FixRequest:
    """The request for a provider's notice: its endpoints, changes and SDK releases.

    ``endpoints`` keeps only those positions, in that order, such as the ones the
    repository uses. ``spec`` says the notice comes from comparing the provider's spec.
    Changes to the field names in ``read``, which the code reads, come first, the
    shallowest first, so that they are the ones shown when not all of them fit.
    ``found`` is what the search found in the code: when it holds some of the retired
    values, the others are left out, so a fix for one retired model lists that model.
    """
    names = set(read)
    retired = {
        change.before
        for endpoint in notice.endpoints
        for change in endpoint.changes
        if change.kind is ChangeKind.VALUE_RETIRED
    }
    used = retired & set(found)
    picked = (
        [notice.endpoints[i] for i in endpoints if 0 <= i < len(notice.endpoints)]
        if endpoints
        else notice.endpoints
    )
    apis = [
        FixApi(
            method=endpoint.method,
            url=endpoint.url,
            targets=sorted(targets, key=lambda t: _relevance(t, names)) if names else targets,
        )
        for endpoint in picked
        if (targets := _in_use(notice_targets(endpoint, notice.applies_to), used))
    ]
    terms = [target.field for api in apis for target in api.targets if target.field]
    terms += [
        target.before
        for api in apis
        for target in api.targets
        if target.kind is ChangeKind.VALUE_RETIRED and target.before
    ]
    terms += [term for api in apis for term in url_terms(api.url)]
    terms += [sdk.name for sdk in notice.sdks]
    source = NoticeSource(
        kind="spec" if spec is not None else "notice",
        spec=spec,
        id=notice.id,
        provider=notice.provider,
        summary=notice.summary,
        published_at=to_iso(notice.published_at),
        effective_at=to_iso(notice.effective_at) if notice.effective_at else None,
        location=location,
        key=notice.signature.key if verified and notice.signature else None,
        sdks=notice.sdks,
    )
    return FixRequest(
        notice=source,
        apis=apis,
        search_terms=list(dict.fromkeys(terms)),
        candidate_files=list(candidate_files),
    )
