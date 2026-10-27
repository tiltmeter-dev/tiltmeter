"""Known API changes that already shipped, bundled with Tiltmeter for ``tiltmeter scan``.

``api-changes.json`` is an ``api-change-feed/v1`` document, so every entry is a notice that
validates like any other. Tiltmeter drafted each one from the provider's public
documentation, and none is signed or endorsed by the provider: ``tiltmeter fix`` takes an
entry only with ``--allow-unsigned``, and every summary ends with :data:`DRAFTED`.
docs/scan.md says how to add an entry.
"""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Final

FILE_NAME: Final = "api-changes.json"
# Where an entry was read from, as notices and fixes record it: "tiltmeter catalog#<id>".
LOCATION: Final = "tiltmeter catalog"
# The sentence that ends every summary, naming the provider in words.
DRAFTED: Final = re.compile(
    r"Drafted by Tiltmeter from (?P<name>[^.;]+?)'s public documentation; "
    r"not signed or endorsed by (?P=name)\.$"
)


def catalog_bytes() -> bytes:
    """The bundled catalog, as stored in the package."""
    return files(__name__).joinpath(FILE_NAME).read_bytes()


def from_catalog(location: str) -> bool:
    """Whether a notice was read from the bundled catalog."""
    return location.startswith(f"{LOCATION}#")
