"""Render docs/demo.svg: a fake billing API changes and driftwatch catches it.

    uv run python scripts/demo.py

Everything runs locally: a throwaway HTTP server plays the vendor API. Set
ANTHROPIC_API_KEY to include a real explanation written by Claude in the image.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rich.console import Console

from driftwatch.cli import exit_code_for
from driftwatch.config import Config
from driftwatch.console import render_results
from driftwatch.service import DriftService

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "demo.svg"


def customers(renamed: bool) -> dict[str, Any]:
    rows = []
    for i in range(6):
        row: dict[str, Any] = {
            "id": f"cus_{1000 + i}",
            "email": f"user{i}@example.com",
            "status": "active" if i % 3 else "past_due",
        }
        if renamed:
            row["phone_number"] = "+1 555 0100"
            row["balance"] = str(1200 + i)
        else:
            row["phone"] = "+1 555 0100"
            row["balance"] = 1200 + i
        rows.append(row)
    return {"object": "list", "data": rows, "has_more": False}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


async def main() -> None:
    console = Console(record=True, width=124)
    explain = bool(os.environ.get("ANTHROPIC_API_KEY"))
    with tempfile.TemporaryDirectory() as tmp:
        www = Path(tmp) / "www"
        www.mkdir()
        body = www / "customers.json"
        body.write_text(json.dumps(customers(renamed=False)))
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(www)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = Config.model_validate(
            {
                "endpoints": [
                    {
                        "id": "billing_customers",
                        "description": "Customer list from the billing API",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/customers.json",
                    }
                ]
            }
        )
        try:
            async with DriftService(config, state_dir=Path(tmp) / "state") as service:
                console.print("[bold]$ driftwatch check[/]")
                render_results(console, await service.check(explain=explain, notify=False))
                body.write_text(json.dumps(customers(renamed=True)))
                console.print()
                console.print(
                    "[bold]$ driftwatch check[/]  "
                    "[dim]# the vendor renamed phone and turned balance into a string[/]"
                )
                results = await service.check(explain=explain, notify=False)
                render_results(console, results)
                console.print(f"[dim]$ echo $?[/]\n{exit_code_for(results)}")
        finally:
            server.shutdown()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="driftwatch")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
