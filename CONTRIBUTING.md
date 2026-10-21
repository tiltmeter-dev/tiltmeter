# Contributing to Tiltmeter

Thanks for helping. Bug reports, documentation fixes, new notifiers and sharper drift
rules are all welcome. This guide covers how to set up the project, the rules the code
follows, and what a pull request needs.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md). Please
report security problems privately, as described in [SECURITY.md](SECURITY.md), not in
public issues.

## Ways to help

- **Report a bug** with the smallest config and API response that reproduce it. Remove
  secrets first.
- **Report noise.** A false alarm on a real API is a bug too. Tell us what the API returned
  and what Tiltmeter reported.
- **Propose a feature** in an issue before writing a large change, so we can agree on the
  approach.
- **Improve the docs** whenever something confused you.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
git clone <your fork> tiltmeter && cd tiltmeter
uv sync                         # creates .venv with the project and the dev tools
uv run tiltmeter --version
```

Optionally install the git hooks, which run ruff on every commit:

```bash
uvx pre-commit install
```

## Everyday commands

```bash
uv run pytest                          # unit tests, offline, a few seconds
uv run pytest --cov                    # with coverage
uv run pytest -m integration           # also calls Claude; needs ANTHROPIC_API_KEY
uv run ruff check src tests scripts    # lint
uv run ruff format src tests scripts   # format
uv run mypy                            # type-check
uv run python scripts/demo.py          # re-render docs/demo.svg (needs ANTHROPIC_API_KEY)
```

CI runs the same lint, type and test commands on Linux with Python 3.12, 3.13 and 3.14,
and on macOS and Windows with Python 3.13. It also builds the package and checks it.

## How the code is organised

| Package | Responsibility |
|---|---|
| `tiltmeter/core` | Shape inference, the diff, severity rules, HTTP fetching and the SQLite store. |
| `tiltmeter/graph` | The LangGraph pipeline that sequences one check. |
| `tiltmeter/llm` | The Claude explainer and its prompt. |
| `tiltmeter/notify` | Slack and webhook notifiers. |
| `tiltmeter/fix` | `tiltmeter fix`: the fix request, the agents (Deep Agents, Claude Code), the git worktree and the pull request. |
| `tiltmeter/mcp_server` | The MCP server. |
| `tiltmeter/service.py` | The service the CLI and the MCP server share. |
| `tiltmeter/cli.py`, `console.py` | The command line and its rich output. |

A few rules keep it that way:

- **`core` stays framework-free.** It never imports LangChain, LangGraph, the MCP SDK or
  Typer, and it passes `mypy --strict`. Anything that decides what counts as drift belongs
  there, with tests that need no network and no LLM.
- **The graph only orchestrates.** Nodes call into `core`, the store, the explainer and the
  notifiers. Dependencies reach them through the runtime context, never through the state.
- **Secrets never leave the request code.** Config objects keep `${VAR}` placeholders;
  only `core/fetch.py` and the notifiers expand them, and errors are scrubbed. Don't log,
  persist or send expanded values anywhere.
- **stdout belongs to the protocol in `serve`.** Library code never prints; the CLI does,
  through `console.py` or `_print_json`.
- **The fix agent only touches files.** The Deep Agent runs on `FilesystemBackend` rooted
  at a fresh worktree, never a shell backend, and `deepagents` and `langchain` are imported
  lazily so the base install works without the `fix` extra. An agent CLI such as Claude Code
  runs through `fix/_proc.py`, without a shell, in the worktree, with its own shell tools
  turned off and `fix.ignore` denied. A new backend implements the `FixAgent` protocol in
  `fix/agent.py` and is picked in `build_fix_agent`. Git and `gh` run the same way, and
  only on the worktree's branch.

[How it works](docs/how-it-works.md) explains the design in depth.

## Tests

- Unit tests are offline. `tests/conftest.py` strips `ANTHROPIC_API_KEY`, LangSmith and
  `TILTMETER_CONFIG` from the environment, HTTP is mocked with
  [respx](https://lundberg.github.io/respx/), and `tests/helpers.py` provides a fake
  explainer, a recording notifier and a controllable clock.
- `tests/service_kit.py` builds a `DriftService` wired to those fakes, which is the easiest
  way to test anything end to end.
- `tests/fix/kit.py` builds real git repositories with a bare remote, a fake fix agent,
  a fake GitHub and a scripted chat model that drives the real Deep Agent offline.
  `tests/fix/fake_claude.py` stands in for the `claude` CLI.
- Tests that call real services are marked `@pytest.mark.integration` and skipped by
  default.

### Golden diff cases

`tests/fixtures/pairs/<case>/` holds a `before.json` response, an `after.json` response and
the `expected.json` list of changes. `tests/core/test_diff.py` runs every folder
automatically, so a new diff behaviour usually starts with a new folder:

```json
[
  {"path": "$.user.phone", "kind": "nullable_introduced", "severity": "breaking"}
]
```

Add `"hint"` to an expected change when the diff should attach one.

## Common changes

### A new kind of change

1. Add it to `ChangeKind` and give it a sentence in `describe_change`, both in
   `core/models.py`.
2. Give it a default in `DEFAULT_SEVERITY` in `core/classify.py`.
3. Detect it in `core/diff.py`, or in `core/analyze.py` for whole-response checks.
4. Add a golden case and unit tests.
5. Document it in the tables of `README.md`, `docs/configuration.md` and
   `docs/how-it-works.md`.

### A new notifier

1. Add a config model to `config.py` with a unique `type` literal and include it in the
   `NotifierConfig` union.
2. Implement it in `notify/`, subclassing `ThresholdNotifier`, and register it in
   `build_notifiers`. Errors must never include the target URL.
3. Test it with respx and document it in `docs/configuration.md`.

### A new MCP tool

1. Add it in `mcp_server/server.py` as an `async` function with a docstring written for
   the model, and the right `ToolAnnotations`.
2. Raise `ToolError` with a readable message for expected failures.
3. Cover it in `tests/test_mcp_server.py` and document it in `docs/agents.md`.

## Style

- Type hints everywhere. Prefer small functions, plain data and clear names over comments.
- Docstrings say what a function is for and anything surprising about it.
- Error messages are lowercase sentences that tell the user what to do next.
- Keep dependencies few. A new one needs a reason in the pull request.

## Commits and pull requests

- One logical change per pull request, with tests and docs.
- Commit subjects in the imperative mood and under about 72 characters, such as
  `Add a Microsoft Teams notifier`. Use the body to explain why.
- Add a line to the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md) for anything a
  user would notice.
- CI must be green before review. Maintainers squash or rebase on merge.

## Releases

Maintainers cut releases from tags; the whole process is in [RELEASING.md](RELEASING.md).
