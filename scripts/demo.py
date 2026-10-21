"""Render docs/demo.svg: a fake billing API changes, Tiltmeter catches it, and an agent
adapts the code that reads it.

    uv run python scripts/demo.py

Everything runs locally: a throwaway HTTP server plays the vendor API, a temporary git
repository with a bare remote plays your service, and the pull request goes to a stub
that answers with an example URL. The fix itself is real, so ANTHROPIC_API_KEY must be
set: Claude reads the toy repository and writes the diff shown in the image.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rich.console import Console

from tiltmeter.cli import exit_code_for
from tiltmeter.config import Config
from tiltmeter.console import render_fix_outcome, render_fix_preview, render_results
from tiltmeter.fix.agent import DeepFixAgent
from tiltmeter.fix.runner import FixSession
from tiltmeter.service import DriftService

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "demo.svg"
PULL_REQUEST = "https://github.com/acme/billing-service/pull/41"

SERVICE = {
    "billing/__init__.py": "",
    "billing/client.py": '''\
import httpx

BASE_URL = "https://api.billing.example/v1"


def list_customers() -> list[dict]:
    return httpx.get(f"{BASE_URL}/customers").json()["data"]


def contact_lines() -> list[str]:
    return [f"{customer['email']} <{customer['phone']}>" for customer in list_customers()]


def total_balance(customers: list[dict]) -> int:
    """Sum of balances, in cents."""
    return sum(customer["balance"] for customer in customers)
''',
    "billing/models.py": """\
from typing import TypedDict


class Customer(TypedDict):
    id: str
    email: str
    phone: str
    balance: int
""",
    "tests/test_client.py": """\
from billing.client import total_balance

ADA = {"id": "cus_1000", "email": "ada@example.com", "phone": "+1 555 0100", "balance": 1200}


def test_total_balance():
    assert total_balance([ADA, ADA]) == 2400
""",
}


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


class _DemoGitHub:
    """Stands in for `gh`: the demo has no real repository to open a pull request on."""

    def problem(self, cwd: Path) -> str | None:
        return None

    def create_pull_request(self, **kwargs: Any) -> str:
        return PULL_REQUEST


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def billing_service(root: Path) -> Path:
    """The toy service whose code reads the billing API, pushed to a bare remote."""
    repo = root / "billing-service"
    for name, text in SERVICE.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "init", "--quiet", "--bare", "-b", "main", "remote.git")
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "Demo")
    _git(repo, "config", "user.email", "demo@example.com")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "Billing client")
    _git(repo, "remote", "add", "origin", str(root / "remote.git"))
    return repo


async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("set ANTHROPIC_API_KEY: the demo runs the real fix agent")
    console = Console(record=True, width=124, highlight=False)
    with tempfile.TemporaryDirectory() as tmp:
        www = Path(tmp) / "www"
        (www / "v1").mkdir(parents=True)
        body = www / "v1" / "customers"
        body.write_text(json.dumps(customers(renamed=False)))
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(www)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = Config.model_validate(
            {
                "endpoints": [
                    {
                        "id": "billing_customers",
                        "description": "Customer list from the billing API",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/v1/customers",
                    }
                ]
            }
        )
        try:
            async with DriftService(config, state_dir=Path(tmp) / "state") as service:
                console.print("[bold]$ tiltmeter check[/]")
                render_results(console, await service.check(explain=False, notify=False))
                body.write_text(json.dumps(customers(renamed=True)))
                console.print()
                console.print(
                    "[bold]$ tiltmeter check[/]  "
                    "[dim]# the vendor renamed phone and turned balance into a string[/]"
                )
                results = await service.check(explain=False, notify=False)
                render_results(console, results)
                console.print(f"[dim]$ echo $?[/]\n{exit_code_for(results)}")

                check_id = results[0].check_id
                assert check_id is not None
                request = service.fix_request(check_id)
                agent = DeepFixAgent.from_config(config.fix)
                console.print()
                console.print(f"[bold]$ tiltmeter fix {check_id} --repo ~/code/billing-service[/]")
                with FixSession.open(billing_service(Path(tmp)), request) as session:
                    console.print(
                        f"Working on a new branch, [bold]{session.branch}[/], "
                        f"from {session.base} at {session.repo.head()}."
                    )
                    report = await agent.run(request, session.root)
                    preview = session.review(report, ignore=config.fix.ignore)
                    render_fix_preview(console, preview)
                    console.print("Commit, push and open a pull request? [y/N]: y", markup=False)
                    outcome = session.deliver(
                        preview, push=True, github=_DemoGitHub(), model_name=agent.model_name
                    )
                render_fix_outcome(console, outcome)
        finally:
            server.shutdown()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="Tiltmeter")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
