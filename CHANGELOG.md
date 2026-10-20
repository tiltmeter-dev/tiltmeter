# Changelog

## 0.1.0 (unreleased)

First version.

- Learn the shape of JSON responses from real traffic and diff it against a rolling baseline.
- Severity rules for removed, newly optional, retyped and newly nullable fields, new enum
  values, status and content-type changes, and latency regressions, plus rename hints.
- CLI: `init`, `check`, `watch`, `history`, `show`, `schema`, `explain` and `serve`, with
  exit codes for CI.
- MCP server with `has_drifted`, `get_schema`, `get_changes`, `list_endpoints`, `check_api`
  and `check_all`.
- Optional drift explanations written by Claude through LangChain, using structured outputs.
- Slack and generic webhook notifications.
- Local SQLite history with content-addressed shapes.
