# Changelog

All notable changes to Tiltmeter are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a minor release may include breaking changes.

## [Unreleased]

### Added

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
