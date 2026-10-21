"""A stand-in for the ``claude`` CLI, run as ``python fake_claude.py <args>``.

FAKE_CLAUDE_LOG names the file where it records its arguments, stdin, working
directory, environment and the settings and instructions it was given.
FAKE_CLAUDE_SCENARIO picks how the run ends: success (the default), text, silent,
max-turns, error, crash or slow.
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


def main() -> int:
    args = sys.argv[1:]
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    record = {
        "argv": args,
        "stdin": sys.stdin.read(),
        "cwd": os.getcwd(),
        "env": sorted(key for key in os.environ if key.startswith(("CLAUDE", "GIT_"))),
        "settings": json.loads(Path(option(args, "--settings")).read_text(encoding="utf-8")),
        "system": Path(option(args, "--append-system-prompt-file")).read_text(encoding="utf-8"),
    }
    Path(os.environ["FAKE_CLAUDE_LOG"]).write_text(json.dumps(record), encoding="utf-8")
    if scenario == "slow":
        time.sleep(10)
    if scenario == "crash":
        sys.stderr.write("Error: --json-schema is not valid JSON\n")
        return 1
    for name in ("client.py", "models.py"):
        path = Path(name)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            text = text.replace('["phone"]', '["phone_number"]')
            path.write_text(text.replace("phone: str", "phone_number: str"), encoding="utf-8")
    result: dict[str, object] = {"type": "result", "subtype": "success", "is_error": False}
    if scenario == "success":
        result |= {"result": json.dumps(REPORT), "structured_output": REPORT}
    elif scenario == "text":
        result |= {"result": "Done.\n```json\n" + json.dumps(REPORT) + "\n```"}
    elif scenario == "silent":
        result |= {"result": "I renamed the field."}
    elif scenario == "max-turns":
        result = {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "errors": ["Reached maximum number of turns (40)"],
        }
    elif scenario == "error":
        result = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["API Error: 529 Overloaded"],
        }
    print(json.dumps(result))
    return 1 if result["is_error"] else 0


if __name__ == "__main__":
    sys.exit(main())
