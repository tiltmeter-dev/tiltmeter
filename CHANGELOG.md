# Changelog

All notable changes to Tiltmeter are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a minor release may include breaking changes.

## [Unreleased]

### Added

- `tiltmeter fix --agent codex` (or `fix.agent: codex`) runs the fix with Codex on your own
  login, with no API key and no extra install. Codex reads files through its shell, so the
  shell stays, inside Codex's OS sandbox with a permission profile passed on the command
  line: nothing readable outside the worktree except system paths and Codex itself, writes
  only inside it, `.git` and `fix.ignore` denied, no network, a core set of environment
  variables, and web search, apps, plugins and the browser turned off. Your Codex config
  and rules are not loaded and the session is not saved.

## [0.4.0] - 2026-10-21

### Added

- `tiltmeter init --from-har session.har` writes the config from a HAR file exported from
  the browser's Network tab: one endpoint per JSON API the page called. Tokens, cookies,
  API keys and password fields become `${VAR}` placeholders and are never written or
  printed. `--host` keeps one host and its subdomains; POST, PUT, PATCH and DELETE requests
  are left out unless you pass `--include-writes` (GraphQL queries count as reads).

## [0.3.0] - 2026-10-21

### Added

- `tiltmeter fix --agent claude-code` (or `fix.agent: claude-code`) runs the fix with
  Claude Code on your own login, with no API key and no extra install. Claude Code runs
  headless in the worktree with file tools only: no shell, no MCP servers, no hooks, and
  deny rules for `.git` and `fix.ignore`. Deep Agents stays the default.
- The getting-started tour ends with a fix: an agent adapts a small client to the vendor
  change.

## [0.2.0] - 2026-10-21

### Added

- `tiltmeter fix <check-id>`: a LangChain Deep Agent adapts the code that reads a drifted
  API in a separate git worktree, shows the diff, and on approval commits, pushes and opens
  a pull request with `gh`. It needs the new `fix` extra (`pip install "tiltmeter[fix]"`)
  and `ANTHROPIC_API_KEY`, and is configured in the new `fix` section of `tiltmeter.yaml`.
- A GitHub Action, [tiltmeter-dev/tiltmeter-action](https://github.com/tiltmeter-dev/tiltmeter-action),
  that keeps the baseline in the Actions cache and reports drift in the job summary.

### Changed

- The project homepage is now https://tiltmeter.dev.

## [0.1.0] - 2026-10-21

First public release, with the same features as 0.1.0rc1.

## [0.1.0rc1] - 2026-10-21

First release candidate, published to TestPyPI only.

### Added

- Learn the shape of JSON responses from real traffic and compare every check with a
  rolling baseline of recent responses, with no spec required.
- Classify changes as breaking, warning or info: removed, newly optional, retyped and newly
  nullable fields, new enum values, status and content-type changes, latency regressions
  and new fields, with hints for probable renames.
- The `tiltmeter` command with `init`, `check`, `watch`, `history`, `show`, `schema`,
  `explain` and `serve`, and exit codes for CI.
- An MCP server with `has_drifted`, `get_schema`, `get_changes`, `list_endpoints`,
  `check_api` and `check_all`, plus endpoint and schema resources.
- Optional drift explanations written by Claude through LangChain, using structured
  outputs.
- Slack and webhook notifications.
- A local SQLite history with content-addressed shapes and retention.
