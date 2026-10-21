from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.fix.kit import make_repo, rename_request
from tiltmeter.config import FixConfig
from tiltmeter.fix.agent import DeepFixAgent
from tiltmeter.fix.runner import FixSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY"),
]


async def test_real_claude_follows_a_rename(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    agent = DeepFixAgent.from_config(FixConfig())
    with FixSession.open(repo, rename_request()) as session:
        report = await agent.run(session.request, session.root)
        preview = session.review(report)
    assert {"client.py", "models.py"} <= set(preview.files)
    assert 'customer["phone_number"]' in preview.diff
    assert "phone_number: str" in preview.diff
    assert report.summary
    assert report.confidence in {"low", "medium", "high"}
