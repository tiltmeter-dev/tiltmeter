from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.fix.kit import make_repo, rename_request
from tiltmeter.config import FixConfig
from tiltmeter.errors import FixError
from tiltmeter.fix.claude_code import ClaudeCodeFixAgent
from tiltmeter.fix.runner import FixSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("claude") is None, reason="needs Claude Code on PATH"),
]


async def test_real_claude_code_follows_a_rename(tmp_path: Path) -> None:
    try:
        agent = ClaudeCodeFixAgent.from_config(
            FixConfig(agent="claude-code", effort="low"), environ=os.environ
        )
    except FixError as exc:
        pytest.skip(str(exc))
    repo, _ = make_repo(tmp_path)
    with FixSession.open(repo, rename_request()) as session:
        report = await agent.run(session.request, session.root)
        preview = session.review(report)
        assert not (session.root / ".claude").exists()
    assert {"client.py", "models.py"} <= set(preview.files)
    assert 'customer["phone_number"]' in preview.diff
    assert "phone_number: str" in preview.diff
    assert report.summary
    assert report.confidence in {"low", "medium", "high"}
