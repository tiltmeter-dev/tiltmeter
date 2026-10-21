from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from tests.fix.kit import CLIENT, ScriptedChatModel, rename_request, report, tool_call
from tiltmeter.config import FixConfig
from tiltmeter.errors import FixError
from tiltmeter.fix.agent import HIDDEN_TOOLS, DeepFixAgent, parse_report
from tiltmeter.fix.models import FixReport
from tiltmeter.fix.prompts import SYSTEM_PROMPT, render_fix_request


def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "client.py").write_text(CLIENT, encoding="utf-8")
    (root / ".env").write_text("BILLING_KEY=sk-live-secret\n", encoding="utf-8")
    return root


def finish(**overrides: Any) -> Any:
    return tool_call("FixReport", "call_done", **report(**overrides).model_dump())


async def test_the_agent_edits_files_under_its_root_and_reports(tmp_path: Path) -> None:
    root = worktree(tmp_path)
    model = ScriptedChatModel(
        script=[
            tool_call("grep", "call_1", pattern="phone", output_mode="content"),
            tool_call(
                "edit_file",
                "call_2",
                file_path="/client.py",
                old_string='customer["phone"]',
                new_string='customer["phone_number"]',
            ),
            tool_call("read_file", "call_3", file_path="/.env"),
            tool_call("task", "call_4", description="look around", subagent_type="general-purpose"),
            tool_call("read_file", "call_5", file_path="/../outside.txt"),
            finish(),
        ]
    )
    agent = DeepFixAgent(model, model_name="scripted", max_model_calls=10, ignore=[".env*"])

    result = await agent.run(rename_request(), root)

    assert result == report()
    assert 'customer["phone_number"]' in (root / "client.py").read_text(encoding="utf-8")
    assert sorted(path.name for path in root.iterdir()) == [".env", "client.py"]  # no scratch

    replies = {
        message.tool_call_id: str(message.content)
        for message in model.seen[-1]
        if isinstance(message, ToolMessage)
    }
    assert 'customer["phone"]' in replies["call_1"]
    assert "permission denied" in replies["call_3"]
    assert "sk-live-secret" not in json.dumps([str(m.content) for m in model.seen[-1]])
    assert "not available" in replies["call_4"]
    assert "outside.txt" in replies["call_5"]
    assert "Error" in replies["call_5"] or "error" in replies["call_5"].lower()

    for offered in model.offered:
        assert not HIDDEN_TOOLS & set(offered)
        assert {"read_file", "edit_file", "grep", "glob"} <= set(offered)

    system, first = model.seen[0][0], model.seen[0][1]
    assert SYSTEM_PROMPT in str(system.content)
    assert first.content == render_fix_request(rename_request())


async def test_the_model_call_limit_stops_a_runaway_agent(tmp_path: Path) -> None:
    root = worktree(tmp_path)
    looping = [tool_call("ls", f"call_{i}", path="/") for i in range(5)]
    agent = DeepFixAgent(
        ScriptedChatModel(script=looping), model_name="scripted", max_model_calls=3
    )
    with pytest.raises(FixError, match="used its 3 model calls"):
        await agent.run(rename_request(), root)


def test_parse_report_accepts_models_and_mappings() -> None:
    assert parse_report({"structured_response": report()}) == report()
    assert parse_report({"structured_response": report().model_dump()}) == report()
    with pytest.raises(FixError, match="without reporting"):
        parse_report({"messages": []})
    with pytest.raises(FixError):
        parse_report(None)


def test_report_schema_is_strict() -> None:
    schema = FixReport.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"summary", "files_changed", "unresolved", "confidence"}


def test_from_config_builds_a_claude_model() -> None:
    config = FixConfig(model="claude-sonnet-5-5", effort="high", max_model_calls=7)
    agent = DeepFixAgent.from_config(config)
    assert agent.model_name == "claude-sonnet-5-5"
    model = agent._model
    assert model.model == "claude-sonnet-5-5"  # type: ignore[attr-defined]
    assert model.effort == "high"  # type: ignore[attr-defined]
    assert model.thinking == {"type": "adaptive"}  # type: ignore[attr-defined]
    assert agent._max_model_calls == 7


def test_from_config_explains_the_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def without_deepagents(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "deepagents" or name.startswith("deepagents."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_deepagents)
    with pytest.raises(FixError, match=r'pip install "tiltmeter\[fix\]"'):
        DeepFixAgent.from_config(FixConfig())
