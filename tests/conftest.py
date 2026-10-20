from __future__ import annotations

from collections.abc import Iterator

import pytest

from driftwatch.core.store import Store


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as db:
        yield db
