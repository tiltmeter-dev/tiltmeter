"""Fakes for ``tiltmeter fix``: a real git repository with a bare remote, a scripted
agent, a fake GitHub and a scripted chat model that drives the real Deep Agent."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tests.helpers import explanation, make_endpoint
from tiltmeter.core.models import Change, ChangeKind, CheckResult, Severity
from tiltmeter.errors import FixError
from tiltmeter.fix.models import FixReport, FixRequest
from tiltmeter.fix.request import build_fix_request

CLIENT = """\
import httpx


def customer_phone(customer_id: int) -> str:
    customer = httpx.get(f"https://api.billing.test/v1/customers/{customer_id}").json()
    return customer["phone"]
"""

MODELS = """\
from typing import TypedDict


class Customer(TypedDict):
    id: int
    phone: str
"""

PR_URL = "https://github.test/acme/app/pull/7"


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return completed.stdout


def make_repo(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, Path]:
    """A repository with one commit on main, pushed to a bare ``origin`` next to it."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(remote))
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    for key, value in {
        "user.name": "Ada Lovelace",
        "user.email": "ada@example.com",
        "commit.gpgsign": "false",
        "core.autocrlf": "false",
    }.items():
        git(repo, "config", key, value)
    contents = files or {"client.py": CLIENT, "models.py": MODELS, ".gitignore": ".env\n"}
    for name, text in contents.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "Initial commit")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "--quiet", "-u", "origin", "main")
    return repo, remote


def rename_check(check_id: int = 12) -> CheckResult:
    """A stored check where ``phone`` became ``phone_number`` and ``balance`` a string."""
    return CheckResult(
        endpoint_id="billing_customers",
        check_id=check_id,
        checked_at="2026-10-21T12:00:00Z",
        status_code=200,
        changes=[
            Change(
                path="$.phone",
                kind=ChangeKind.FIELD_REMOVED,
                severity=Severity.BREAKING,
                before="string",
                hint="possibly renamed to $.phone_number",
            ),
            Change(
                path="$.phone_number",
                kind=ChangeKind.FIELD_ADDED,
                severity=Severity.INFO,
                after="string",
                hint="possibly replaces $.phone",
            ),
            Change(
                path="$.balance",
                kind=ChangeKind.TYPE_CHANGED,
                severity=Severity.BREAKING,
                before="integer",
                after="string",
            ),
        ],
        max_severity=Severity.BREAKING,
        explanation=explanation(),
    )


def billing_endpoint(**overrides: Any) -> Any:
    data: dict[str, Any] = {
        "id": "billing_customers",
        "url": "https://api.billing.test/v1/customers?api_key=${BILLING_KEY}",
        "description": "Customers from the billing provider",
    }
    data.update(overrides)
    return make_endpoint(**data)


def rename_request(check_id: int = 12) -> FixRequest:
    return build_fix_request(
        billing_endpoint(), rename_check(check_id), baseline=None, observed=None
    )


def report(**overrides: Any) -> FixReport:
    data: dict[str, Any] = {
        "summary": "Read phone_number instead of phone.",
        "files_changed": ["/client.py", "/models.py"],
        "unresolved": [],
        "confidence": "high",
    }
    data.update(overrides)
    return FixReport.model_validate(data)


def rename_phone(root: Path) -> None:
    for name in ("client.py", "models.py"):
        path = root / name
        text = path.read_text(encoding="utf-8")
        text = text.replace('["phone"]', '["phone_number"]').replace(
            "phone: str", "phone_number: str"
        )
        path.write_text(text, encoding="utf-8")


class FakeFixAgent:
    """Applies ``edit`` to the worktree and answers with a canned report (or raises)."""

    model_name = "fake-fixer"

    def __init__(
        self,
        edit: Callable[[Path], None] = rename_phone,
        result: FixReport | None = None,
        error: Exception | None = None,
    ) -> None:
        self.edit = edit
        self.result = result or report()
        self.error = error
        self.requests: list[FixRequest] = []
        self.roots: list[Path] = []

    async def run(self, request: FixRequest, root: Path) -> FixReport:
        self.requests.append(request)
        self.roots.append(root)
        if self.error is not None:
            raise self.error
        self.edit(root)
        return self.result


class FakeGitHub:
    """Records pull requests instead of opening them."""

    def __init__(self, problem: str | None = None, error: str | None = None) -> None:
        self._problem = problem
        self._error = error
        self.created: list[dict[str, Any]] = []

    def problem(self, cwd: Path) -> str | None:
        return self._problem

    def create_pull_request(
        self, *, cwd: Path, head: str, base: str | None, title: str, body: str
    ) -> str:
        if self._error is not None:
            raise FixError(self._error)
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        return PR_URL

    def find_pull_request(self, *, cwd: Path, head: str) -> str | None:
        return next((PR_URL for pr in self.created if pr["head"] == head), None)


def tool_call(name: str, call_id: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


class ScriptedChatModel(BaseChatModel):
    """Plays back AI messages in order and records the tool names each call offered."""

    script: list[AIMessage]
    offered: list[list[str]] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        names = []
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
            names.append(str(name or getattr(tool, "__name__", "?")))
        self.offered.append(sorted(names))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        message = self.script[len(self.seen) - 1]
        return ChatResult(generations=[ChatGeneration(message=message)])
