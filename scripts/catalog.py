"""Add entries to the catalog of known API changes that `tiltmeter scan` reads.

    uv run python scripts/catalog.py notice.json [more.json ...]

Each file must be a valid, unsigned api-change-notice/v1 whose summary ends with the sentence
"Drafted by Tiltmeter from <Provider>'s public documentation; not signed or endorsed by
<Provider>." An entry with the same provider and id is replaced, and the catalog stays newest
first. docs/scan.md has the rest of what an entry needs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tiltmeter.catalog import DRAFTED, FILE_NAME
from tiltmeter.errors import TiltmeterError
from tiltmeter.notices.create import add_to_feed, write_json
from tiltmeter.notices.models import FEED_SPEC
from tiltmeter.notices.reader import feed_items, parse_notice, read_json_file

CATALOG = Path(__file__).resolve().parent.parent / "src" / "tiltmeter" / "catalog" / FILE_NAME


class CatalogError(Exception):
    """A notice that cannot go into the catalog."""


def add(catalog: Path, notice_files: list[Path]) -> list[str]:
    """Add the notices to the catalog file and return their ids."""
    try:
        current = read_json_file(catalog, limit=10_000_000) if catalog.exists() else None
        if current is not None:
            for item in feed_items(current, str(catalog)):
                parse_notice(item)
    except TiltmeterError as exc:
        raise CatalogError(str(exc)) from exc
    added = []
    for path in notice_files:
        try:
            raw = read_json_file(path)
            notice = parse_notice(raw)
        except TiltmeterError as exc:
            raise CatalogError(f"{path}: {exc}") from exc
        if notice.signature is not None:
            raise CatalogError(f"{path} is signed: catalog entries are Tiltmeter's own drafts")
        if not DRAFTED.search(notice.summary):
            raise CatalogError(
                f"{path}: the summary must end with \"Drafted by Tiltmeter from <Provider>'s "
                'public documentation; not signed or endorsed by <Provider>."'
            )
        current = add_to_feed(current, raw)
        added.append(notice.id)
    write_json(catalog, current or {"spec": FEED_SPEC, "notices": []})
    return added


def main(argv: list[str] | None = None, *, catalog: Path = CATALOG) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("notices", nargs="+", type=Path, help="Notice files to add.")
    args = parser.parse_args(argv)
    try:
        added = add(catalog, args.notices)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for notice_id in added:
        print(f"added {notice_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
