# Changelog

All notable changes to Tiltmeter are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a minor release may include breaking changes.

## [Unreleased]

### Added

- Provider mode: API providers announce a change once and every customer that follows them
  gets the pull request. The format is an open specification, API change notices v1
  (`docs/notice-spec.md`, with JSON Schemas for notices and feeds in `docs/schemas/`),
  in Tiltmeter's own vocabulary of paths and change kinds, signed with Ed25519 and a key
  served at `https://<provider>/.well-known/api-change-key`.
- For providers: `tiltmeter notice create` drafts a notice from a check of your own API
  (`--from-check`) or from the old and new versions of an OpenAPI 3.0, 3.1 or Swagger 2.0
  spec (`--from-openapi`), using the same diff as drift. `notice keygen`, `notice sign` and
  `notice publish` create the key pair, sign the notice and add it to the static feed at
  `/.well-known/api-changes.json`; `notice verify` checks one, and `notice schema` prints
  the JSON Schema.
- For customers: `subscriptions` in `tiltmeter.yaml` lists the providers you follow.
  `tiltmeter notices check` lists new notices with their signatures checked, and
  `tiltmeter notices apply` opens one pull request per notice that affects the repository.
  A plain text search for the API's hosts, paths and SDKs and the changed field names
  decides first, without an LLM, and each notice is handled once.
- `fix.test_command` runs the repository's tests on the agent's edits before anything is
  committed, with no network, no secrets and an empty home folder, and hands a failure back
  to the agent for another round (`fix.test_rounds`). Pull requests get a "Tests" section.
- `tiltmeter fix --notice <file or URL>` fixes what one notice announces. Unsigned notices
  are accepted only from a file, with `--allow-unsigned`.
- `openapi:` subscriptions follow a provider's OpenAPI spec when it publishes no notices:
  the first run keeps a baseline, later runs turn changes to its responses into entries
  for the endpoints the repository uses, handled once like notices. `sdks` lists the
  provider's SDKs as anchors, `versioned: true` waits for the repository to adopt a new
  API version, and `baseline` names the spec version the code was written against, so the
  first run already compares with it.
- The OpenAPI diff compares each named schema once and reports it at the shortest path,
  so specs with thousands of schemas and "id or expanded object" fields, like Stripe's,
  compare in about a second instead of exploding. References to other files or to schemas
  a spec does not define, which real specs such as Square's have, are not compared and are
  counted in a note, instead of failing the whole diff. YAML aliases are expanded up to a
  limit instead of refused.
- Pull requests for notices pick the endpoints the repository uses, from the paths in its
  code, its SDK calls (`stripe.Subscription.retrieve(...)`, `client.messages.create(...)`)
  or the field names it reads, and list the changes to fields the code reads first. SDK
  imports count as uses of the API, and paths without their version (`/account`) count
  only next to them, so an app's own routes are not taken for the provider's. A fix
  branch that is already pushed gets its missing pull request opened from its commit.
- `notice sign` and `notice publish` take several notices, and the signing key can be the
  PEM itself in `TILTMETER_SIGNING_KEY`. `publish --key` signs drafts on the way into the
  feed and `--replace` builds the feed from the given notices only; `notice create
  --if-changed` writes nothing when no response changed.
- The GitHub Action gains `mode: notices`, which runs `notices apply` on a schedule with
  the state in the Actions cache; `mode: notice-draft` and `mode: notice-publish`, which
  draft a notice on pull requests that change a provider's spec and publish the signed feed
  on merge; and `source`, which runs Tiltmeter from a git branch.

### Changed

- `endpoints` is optional in `tiltmeter.yaml` when there are `subscriptions`.
- A removed field that reappears nearby under another parent, with the same name and type,
  is flagged as a probable move (`possibly renamed to $.items.data[*].current_period_end`),
  in drift and in spec diffs alike.
- Pull requests opened by `tiltmeter fix` always have a "Left for a human" section, which
  says so when the agent flagged nothing.
- The fix prompt shares its 60 changes between the endpoints instead of giving them all to
  the first one, and pull request bodies list at most 100 changes, which keeps them under
  GitHub's size limit.
- The history database moves to schema version 3, with tables for handled notices,
  followed feeds and spec baselines. It is upgraded in place; older Tiltmeter versions
  refuse to open it afterwards.
- `tiltmeter fix --json` reports `notice_id` and `provider` for notices, and `check_id` and
  `endpoint_id` are null then.
- `cryptography`, already installed with the MCP SDK, is now a direct dependency.

## [0.5.1] - 2026-10-25

### Added

- Tiltmeter is listed in the official MCP Registry as `dev.tiltmeter/tiltmeter`, described
  by `server.json` at the repository root.

## [0.5.0] - 2026-10-21

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
