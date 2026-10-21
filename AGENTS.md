# Notes for AI coding agents

This file tells coding agents (Claude Code, Codex, Cursor and similar) how to work on this
repository. Humans should read [CONTRIBUTING.md](CONTRIBUTING.md), which says the same in
more detail.

## Commands

```bash
uv sync                                   # set up .venv
uv run pytest -q                          # offline unit tests; run before every commit
uv run ruff check --fix src tests scripts && uv run ruff format src tests scripts
uv run mypy                               # must stay clean; core is --strict
```

Never run `uv run pytest -m integration` unless asked: it calls the Anthropic API and costs
money.

## Rules

- `src/tiltmeter/core` must not import LangChain, LangGraph, `mcp` or Typer, and must pass
  `mypy --strict`. Drift logic lives there.
- Never expand, log, persist or send `${VAR}` secrets outside `core/fetch.py` and the
  notifiers. Never print from library code; `tiltmeter serve` uses stdout for MCP.
- The fix agent (`src/tiltmeter/fix`) gets file tools on a fresh worktree only: never a
  shell backend, never the user's checkout. Import `deepagents` and `langchain` lazily;
  they come with the optional `fix` extra. Agent CLIs such as Claude Code run through
  `fix/_proc.py` without a shell, with their own shell tools turned off.
- Behaviour changes need a test. Diff changes usually need a golden case in
  `tests/fixtures/pairs/<case>/` with `before.json`, `after.json` and `expected.json`.
- Keep the docs in sync: option or change-kind tables live in `README.md`,
  `docs/configuration.md` and `docs/how-it-works.md`.
- Add user-visible changes to the `[Unreleased]` section of `CHANGELOG.md`.
- Do not bump versions, create tags or publish. Releases are a maintainer task described
  in `RELEASING.md`.

## Layout

`core/` (diff, shapes, store, fetch), `graph/` (LangGraph pipeline), `llm/` (Claude
explainer), `fix/` (`tiltmeter fix`: Deep Agent or Claude Code, worktree, pull request), `notify/` (Slack,
webhook), `mcp_server/` (MCP tools), `har.py` (`init --from-har`), `service.py` (shared by CLI and MCP), `cli.py` and `console.py` (Typer and rich), `scripts/` (demo and release
helpers), `tests/` (mirrors `src/`).
