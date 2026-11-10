"""Find the retirements that providers' deprecation pages list and the catalog lacks.

    uv run python scripts/watch.py [--report FILE] [--drafts DIR] [--only NAME] [--all]

The catalog of known API changes that `tiltmeter scan` reads is drafted by hand from each
provider's public documentation, so it goes stale each time a provider retires a model.
This script reads the pages listed in scripts/watch-sources.json, each a Markdown page with
tables of retired values, shutdown dates and replacements, and compares them with the values
the catalog already retires for that provider.

It reports what the pages list and the catalog lacks: what shuts down ahead first, then what
shut down within the last year. `--drafts DIR` also writes one unsigned notice per provider
and date, as a starting point for scripts/catalog.py. A draft is not an entry yet: someone
checks it against the page, spreads the values over the endpoints that take them and writes
the notes. Exit code 0: the catalog has them all; 1: there is something to add; 3: a page
could not be read. .github/workflows/catalog-watch.yml runs it every week and keeps one
issue up to date with the report.

The pages are third-party text. Only what looks like a model id, a date or a replacement id
is taken from them, and the report shows it as code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from tiltmeter.catalog import FILE_NAME
from tiltmeter.core.models import ChangeKind
from tiltmeter.errors import TiltmeterError
from tiltmeter.notices.models import NOTICE_SPEC
from tiltmeter.notices.reader import feed_items, parse_notice, read_json_file

ROOT: Final = Path(__file__).resolve().parent.parent
CATALOG: Final = ROOT / "src" / "tiltmeter" / "catalog" / FILE_NAME
SOURCES: Final = Path(__file__).resolve().parent / "watch-sources.json"

MAX_PAGE_BYTES: Final = 5_000_000
TIMEOUT: Final = 30.0
RECENT_DAYS: Final = 365
# An issue's body holds 65,536 characters; the report stays well under.
MAX_ROWS: Final = 60
MAX_REPORT_CHARS: Final = 60_000
MARK: Final = "catalog-watch:"

# A value code sends: a model id such as gpt-4o-2024-05-13 or meta-llama/llama-guard-4-12b.
_ID: Final = re.compile(r"[a-z0-9][a-z0-9._:/\-]{1,99}")
_CODE: Final = re.compile(r"`([^`\n]{1,200})`")
_CELLS: Final = re.compile(r"(?<!\\)\|")
_RULE: Final = re.compile(r":?-{3,}:?")
_SLASHED: Final = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})")
_WORDED: Final = re.compile(r"([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})")
_ISO: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DASHES: Final = re.compile("[\u2010-\u2015]")  # pages write dates with typographic hyphens
_MONTHS: Final = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]

type Fetch = Callable[[str], str]


class WatchError(Exception):
    """A source or a page that cannot be read."""


@dataclass(frozen=True, slots=True)
class Source:
    """A provider's deprecation page, and how the catalog names that provider."""

    name: str
    provider: str
    slug: str
    page: str
    cite: str
    method: str
    url: str
    path: str = "$.model"
    ignore: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Retirement:
    """A value a page says stops working on a date, and what it names instead."""

    value: str
    on: date
    replaced_by: str | None = None


@dataclass(slots=True)
class Finding:
    """What one source's page lists and the catalog lacks."""

    source: Source
    listed: int = 0
    ahead: list[Retirement] = field(default_factory=list)
    recent: list[Retirement] = field(default_factory=list)
    older: list[Retirement] = field(default_factory=list)
    error: str | None = None

    @property
    def new(self) -> list[Retirement]:
        return [*self.ahead, *self.recent]


# -- the sources ------------------------------------------------------------------


def load_sources(path: Path = SOURCES) -> list[Source]:
    """The pages to watch. Each needs a name, the provider's host as the catalog writes
    it, a slug for ids, the page's URL, how summaries cite it, and the endpoint that takes
    the value."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WatchError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise WatchError(f"{path} must hold a list of sources")
    sources = []
    for position, item in enumerate(raw):
        try:
            ignore = frozenset(str(value) for value in item.get("ignore", []))
            source = Source(**{**item, "ignore": ignore})
        except (AttributeError, TypeError) as exc:
            raise WatchError(f"{path}: source {position} is not one: {exc}") from exc
        if not source.page.startswith("https://") or not source.url.startswith("https://"):
            raise WatchError(f"{path}: {source.name} must use https URLs")
        sources.append(source)
    return sources


def fetch(url: str) -> str:
    """A public page's text. No header, cookie or token of ours goes with the request."""
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "tiltmeter-catalog-watch"})
    except httpx.HTTPError as exc:
        raise WatchError(f"cannot fetch {url}: {type(exc).__name__}") from exc
    if response.status_code != httpx.codes.OK:
        raise WatchError(f"{url} answered {response.status_code}")
    if len(response.content) > MAX_PAGE_BYTES:
        raise WatchError(f"{url} is larger than {MAX_PAGE_BYTES // 1_000_000} MB")
    return response.text


# -- reading a page ----------------------------------------------------------------


def tables(markdown: str) -> Iterator[tuple[list[str], list[list[str]]]]:
    """The pipe tables of a Markdown page: each one's header and rows of cells."""
    lines = markdown.splitlines()
    position = 0
    while position < len(lines) - 1:
        header, rule = _cells(lines[position]), _cells(lines[position + 1])
        if len(header) < 2 or len(rule) != len(header) or not all(map(_RULE.fullmatch, rule)):
            position += 1
            continue
        rows = []
        position += 2
        while position < len(lines) and len(cells := _cells(lines[position])) >= 2:
            rows.append(cells)
            position += 1
        yield header, rows


def _cells(line: str) -> list[str]:
    """A table line's cells. A pipe written as ``\\|`` belongs to its cell."""
    text = line.strip()
    if not text.startswith("|") or len(text) > 2_000:
        return []
    return [cell.strip().replace("\\|", "|") for cell in _CELLS.split(text.strip("|"))]


def parse_date(text: str) -> date | None:
    """A cell that is a date and nothing else: 08/16/26, August 16, 2026, Aug 16, 2026 or
    2026-08-16. "Not sooner than June 9, 2027" is no date: nothing is retired then."""
    cell = _DASHES.sub("-", text).strip().strip("*_ ")
    try:
        if found := _SLASHED.fullmatch(cell):
            year = int(found[3])
            return date(year + 2000 if year < 100 else year, int(found[1]), int(found[2]))
        if found := _ISO.fullmatch(cell):
            return date(int(found[1]), int(found[2]), int(found[3]))
        if found := _WORDED.fullmatch(cell):
            named = found[1].lower().replace("sept", "sep")
            months = [n for n, month in enumerate(_MONTHS, start=1) if month.startswith(named)]
            if len(months) == 1:
                return date(int(found[3]), months[0], int(found[2]))
    except ValueError:
        return None
    return None


def ids(cell: str) -> list[str]:
    """The values a cell names: what it writes as code, or the cell itself when it is one
    value. A cell of prose, such as "Llama Guard 3", names none."""
    quoted = [text.strip() for text in _CODE.findall(cell)]
    candidates = quoted or re.split(r"[\s,|]+|\bor\b", cell)
    found = [text for text in candidates if text and _ID.fullmatch(text)]
    if not quoted and len(found) != len([text for text in candidates if text]):
        # Without code marks, a cell with other words in it is prose.
        return []
    return list(dict.fromkeys(found))


def _column(header: Sequence[str], *words: str, skip: int | None = None) -> int | None:
    for position, title in enumerate(header):
        if position != skip and any(word in title.lower() for word in words):
            return position
    return None


def retirements(markdown: str) -> list[Retirement]:
    """Every value the page's tables retire on a date, each once: where it names a
    replacement, if it does anywhere."""
    found: dict[str, Retirement] = {}
    for header, rows in tables(markdown):
        replacement = _column(header, "replacement", "substitute")
        model = _column(header, "model", skip=replacement)
        when = _column(header, "shutdown", "retire", skip=replacement)
        if model is None or when is None:
            continue
        for row in rows:
            if len(row) != len(header):
                continue
            on = parse_date(row[when])
            if on is None:
                continue
            named = ids(row[replacement]) if replacement is not None else []
            for value in ids(row[model]):
                # A value is not its own replacement, whatever the row says next to it.
                instead = next((name for name in named if name != value), None)
                known = found.get(value)
                if known is None or (known.replaced_by is None and instead is not None):
                    found[value] = Retirement(value, known.on if known else on, instead)
    return list(found.values())


# -- comparing with the catalog -------------------------------------------------------


def retired_in_catalog(catalog: Path = CATALOG) -> dict[str, set[str]]:
    """The values the catalog retires, by provider."""
    try:
        feed = read_json_file(catalog, limit=10_000_000)
        notices = [parse_notice(item) for item in feed_items(feed, str(catalog))]
    except TiltmeterError as exc:
        raise WatchError(str(exc)) from exc
    known: dict[str, set[str]] = {}
    for notice in notices:
        values = known.setdefault(notice.provider, set())
        for endpoint in notice.endpoints:
            values.update(
                change.before
                for change in endpoint.changes
                if change.kind is ChangeKind.VALUE_RETIRED and change.before
            )
    return known


def watch(
    sources: Sequence[Source],
    known: Mapping[str, set[str]],
    *,
    today: date,
    fetch: Fetch = fetch,
) -> list[Finding]:
    """What each source's page lists and the catalog lacks, soonest shutdown first."""
    findings = []
    for source in sources:
        finding = Finding(source)
        findings.append(finding)
        try:
            listed = retirements(fetch(source.page))
        except WatchError as exc:
            finding.error = str(exc)
            continue
        finding.listed = len(listed)
        if not listed:
            finding.error = "no table of retired values was read: has the page changed?"
            continue
        have = known.get(source.provider, set()) | source.ignore
        missing = sorted((r for r in listed if r.value not in have), key=lambda r: (r.on, r.value))
        finding.ahead = [r for r in missing if r.on > today]
        past = [r for r in reversed(missing) if r.on <= today]
        finding.recent = [r for r in past if (today - r.on).days <= RECENT_DAYS]
        finding.older = [r for r in past if (today - r.on).days > RECENT_DAYS]
    return findings


# -- drafts ---------------------------------------------------------------------------


def _day(on: date) -> str:
    return f"{_MONTHS[on.month - 1].capitalize()} {on.day}, {on.year}"


def draft(
    source: Source, on: date, retired: Sequence[Retirement], *, today: date
) -> dict[str, Any]:
    """An unsigned notice for what ``source`` retires on one date. A starting point: the
    notes say only what the page's table says."""
    ahead = on > today
    count = f"{len(retired)} model{'' if len(retired) == 1 else 's'}"
    verb = "shuts down" if ahead else "shut down"
    fails = "will fail from then on" if ahead else "fail"
    # The values are in the changes: a summary has 500 characters, and some dates take 16.
    summary = (
        f"{source.name} {verb} {count} on {_day(on)}. Requests that name "
        f"{'it' if len(retired) == 1 else 'them'} {fails}. "
        f"Each change names the replacement the page gives, when it gives one. Source: "
        f"{source.cite}. Drafted by Tiltmeter from {source.name}'s public documentation; "
        f"not signed or endorsed by {source.name}."
    )
    changes = []
    for item in retired:
        when = f"Shuts down on {_day(on)}" if ahead else f"Shut down on {_day(on)}"
        note = f"{when}: requests with this model {'will fail' if ahead else 'fail'}."
        change: dict[str, Any] = {
            "path": source.path,
            "kind": "value_retired",
            "before": item.value,
        }
        if item.replaced_by is not None:
            change["replaced_by"] = item.replaced_by
            note += f" {item.replaced_by} is the replacement {source.name}'s page names."
        changes.append({**change, "note": note})
    return {
        "spec": NOTICE_SPEC,
        "id": f"{source.slug}-model-retirements-{on.isoformat()}",
        "provider": source.provider,
        "published_at": f"{min(on, today).isoformat()}T00:00:00Z",
        "effective_at": f"{on.isoformat()}T00:00:00Z",
        "summary": summary,
        "endpoints": [{"method": source.method, "url": source.url, "changes": changes}],
    }


def drafts(findings: Sequence[Finding], *, today: date) -> dict[str, dict[str, Any]]:
    """One draft per provider and date, by file name, each a notice that validates."""
    written: dict[str, dict[str, Any]] = {}
    for finding in findings:
        by_date: dict[date, list[Retirement]] = {}
        for item in finding.new:
            by_date.setdefault(item.on, []).append(item)
        for on, retired in sorted(by_date.items()):
            notice = draft(finding.source, on, retired, today=today)
            try:
                parse_notice(notice)
            except TiltmeterError as exc:
                raise WatchError(f"the draft {notice['id']} is not a valid notice: {exc}") from exc
            written[f"{notice['id']}.json"] = notice
    return written


# -- the report -----------------------------------------------------------------------


def _code(text: str) -> str:
    return f"`{text.replace('`', '')}`"


def _distance(on: date, today: date) -> str:
    days = (on - today).days
    if days > 0:
        return "tomorrow" if days == 1 else f"in {days} days"
    return "today" if days == 0 else f"{-days} day{'' if days == -1 else 's'} ago"


def report(findings: Sequence[Finding], *, today: date, show_all: bool = False) -> str:
    """The findings as Markdown, for a terminal or an issue."""
    new = sum(len(finding.new) for finding in findings)
    failed = [finding for finding in findings if finding.error]
    if new:
        opening = (
            f"The providers' deprecation pages list {new} retired value"
            f"{'' if new == 1 else 's'} that the catalog of known API changes lacks."
        )
    else:
        opening = "The catalog of known API changes has every retirement the providers' pages list."
    lines = [opening, ""]
    for finding in findings:
        source = finding.source
        lines += [f"## {source.name}", "", f"Page: {source.page}", ""]
        if finding.error:
            lines += [f"**Could not be read:** {finding.error}", ""]
            continue
        if not finding.new:
            lines += [f"Nothing to add: the page lists {finding.listed} retired values.", ""]
        else:
            lines += ["| Value | Shuts down | | Replacement the page names |", "|---|---|---|---|"]
            for item in finding.new[:MAX_ROWS]:
                instead = _code(item.replaced_by) if item.replaced_by else "none"
                lines.append(
                    f"| {_code(item.value)} | {item.on.isoformat()} | "
                    f"{_distance(item.on, today)} | {instead} |"
                )
            if len(finding.new) > MAX_ROWS:
                lines.append(f"\n… and {len(finding.new) - MAX_ROWS} more.")
            lines.append("")
        if finding.older and show_all:
            listed = ", ".join(_code(item.value) for item in finding.older[:MAX_ROWS])
            lines += [f"Shut down more than a year ago and not in the catalog: {listed}.", ""]
        elif finding.older:
            lines += [
                f"{len(finding.older)} more shut down over a year ago and are not in the "
                "catalog (`--all` lists them).",
                "",
            ]
    if new:
        lines += [
            "## Adding them",
            "",
            "```console",
            "$ uv run python scripts/watch.py --drafts drafts/",
            "$ uv run python scripts/catalog.py drafts/*.json",
            "```",
            "",
            "The first command writes one unsigned notice per provider and date. Before the "
            "second, check each against its page, move every value to the endpoints that "
            "take it and write what callers of the replacement must change. A value that "
            "is no retirement goes in `ignore` in `scripts/watch-sources.json`.",
            "",
        ]
    if failed:
        lines += [f"{len(failed)} of {len(findings)} pages could not be read.", ""]
    body = "\n".join(lines)[:MAX_REPORT_CHARS].rstrip()
    return f"{body}\n\n<!-- {MARK}{fingerprint(findings)} -->\n"


def fingerprint(findings: Sequence[Finding]) -> str:
    """A name for the set of values to add, which stays the same from one week to the next
    while the pages and the catalog do: the weekly job comments only when it changes."""
    missing = sorted(
        f"{finding.source.provider} {item.value} {item.on.isoformat()}"
        for finding in findings
        for item in finding.new
    )
    return hashlib.sha256("\n".join(missing).encode()).hexdigest()[:16]


def exit_code(findings: Sequence[Finding]) -> int:
    if any(finding.new for finding in findings):
        return 1
    return 3 if any(finding.error for finding in findings) else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog: Path = CATALOG,
    sources: Path = SOURCES,
    fetch: Fetch = fetch,
    today: date | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--report", type=Path, help="write the report to this file as well")
    parser.add_argument("--drafts", type=Path, help="write a draft notice per provider and date")
    parser.add_argument("--only", metavar="NAME", help="watch one source, by its name")
    parser.add_argument(
        "--all", action="store_true", help="list what shut down over a year ago too"
    )
    args = parser.parse_args(argv)
    day = today or datetime.now(UTC).date()
    try:
        watched = load_sources(sources)
        if args.only:
            watched = [source for source in watched if source.name.lower() == args.only.lower()]
            if not watched:
                raise WatchError(f"no source is named {args.only}")
        findings = watch(watched, retired_in_catalog(catalog), today=day, fetch=fetch)
        text = report(findings, today=day, show_all=args.all)
        if args.drafts is not None:
            args.drafts.mkdir(parents=True, exist_ok=True)
            for name, notice in drafts(findings, today=day).items():
                path = args.drafts / name
                path.write_text(json.dumps(notice, indent=2) + "\n", encoding="utf-8")
                print(f"wrote {path}", file=sys.stderr)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if args.report is not None:
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    return exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
