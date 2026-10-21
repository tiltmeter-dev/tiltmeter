"""A stand-in for the ``codex`` CLI, run as ``python fake_codex.py <args>``.

FAKE_CODEX_LOG names the file where it records its arguments, stdin, working directory,
environment and the schema it was given. FAKE_CODEX_SCENARIO picks how the run ends:
success (the default), text, silent, failed, crash or slow.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPORT = {
    "summary": "Read phone_number instead of phone.",
    "files_changed": ["client.py", "models.py"],
    "unresolved": [],
    "confidence": "high",
}


def option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def event(kind: str, **fields: object) -> None:
    print(json.dumps({"type": kind, **fields}))


def main() -> int:
    args = sys.argv[1:]
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")
    record = {
        "argv": args,
        "stdin": sys.stdin.read(),
        "cwd": os.getcwd(),
        "env": sorted(key for key in os.environ if key.startswith(("CODEX", "GIT_"))),
        "schema": json.loads(Path(option(args, "--output-schema")).read_text(encoding="utf-8")),
    }
    Path(os.environ["FAKE_CODEX_LOG"]).write_text(json.dumps(record), encoding="utf-8")
    if scenario == "slow":
        time.sleep(10)
    if scenario == "crash":
        sys.stderr.write("error: unexpected argument '--ignore-rules' found\n")
        return 2
    event("thread.started", thread_id="t-1")
    event("turn.started")
    if scenario == "failed":
        event("error", message="stream disconnected before completion")
        event("turn.failed", error={"message": "You've hit your usage limit."})
        return 1
    for name in ("client.py", "models.py"):
        path = Path(name)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            text = text.replace('["phone"]', '["phone_number"]')
            path.write_text(text.replace("phone: str", "phone_number: str"), encoding="utf-8")
    # Deny-glob warnings arrive as error items, and retries as error events; neither fails
    # the run.
    event("item.completed", item={"id": "item_0", "type": "error", "message": "glob warning"})
    event("error", message="Reconnecting... 1/5")
    message = {
        "success": json.dumps(REPORT),
        "text": "Done.\n```json\n" + json.dumps(REPORT) + "\n```",
        "silent": "I renamed the field.",
    }[scenario]
    event("item.completed", item={"id": "item_1", "type": "agent_message", "text": message})
    event("turn.completed", usage={"input_tokens": 10, "output_tokens": 5})
    Path(option(args, "--output-last-message")).write_text(message, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
