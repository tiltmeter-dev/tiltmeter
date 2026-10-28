# Changelog

All notable changes to Tiltmeter are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a minor release may include breaking changes.

## [Unreleased]

### Added

- Notices can say who a change affects, with `applies_to` on the notice or on one change,
  when that is not every caller: a plan, a deployment or an API version. Tiltmeter can't
  tell which of them a repository serves, so a pull request now opens with who the change
  affects, `tiltmeter scan` shows it next to the places it lists, and the fix agent prefers
  edits that keep working for the callers outside it. `tiltmeter notice create
  --applies-to` sets it. Tiltmeter 0.8.0 and earlier reject notices that use it.
- The catalog says who its scoped entries affect: Groq's 2026 retirements (free and
  developer-tier keys; enterprise contracts keep the models), Jira Cloud's removed search
  (Server and Data Center keep it), Spotify's Development Mode changes and Stripe's API
  version 2025-03-31.

## [0.8.0] - 2026-10-27

### Added

- `tiltmeter scan [PATH]` finds code that still uses API changes that already shipped, with
  no configuration: run `uvx tiltmeter scan`. It searches the folder, a git checkout or any
  folder, for every entry of a catalog bundled with Tiltmeter: retired models at Anthropic,
  OpenAI and Groq, Stripe's subscription periods moved in API version 2025-03-31, Jira
  Cloud's removed search, Hugging Face's serverless Inference API, Spotify's February 2026
  changes, OpenWeather's One Call 2.5, Slack's `files.upload` and HubSpot's Pipelines v1.
  Each change it finds comes with its date, the matching lines as `file:line`, a mark on
  files nothing else imports, and the command that fixes it. Tests and docs are listed
  apart and never count on their own. The entries are drafted by Tiltmeter from each
  provider's public documentation, unsigned and not endorsed by the providers, and the
  output says so. `--catalog` reads another feed from a file or an https URL, `--all` also
  lists what did not match, and `--json` prints it all. Exit code 0: nothing found; 1: the
  folder uses a change in the catalog; 3: the catalog could not be read. Nothing is
  recorded or sent.
- `tiltmeter fix --notice` also takes the id of a catalog entry, or `FEED#ID` for one entry
  of a feed, still with `--allow-unsigned` for an unsigned one. The pull request says the
  entry was drafted by Tiltmeter.

### Changed

- The affected-repository search behind `notices check --repo` and `notices apply` no
  longer counts a retired value, a removed endpoint's path or a changed field that appears
  only in comment lines or Python docstrings, a short value such as `o1` outside quotes, a
  value inside a file name (`gpt-4.txt`), `.mdx`, Quarto and LaTeX docs, data tables such as
  benchmark `.csv` files, ignore files, or change notices and
  feeds saved in the repository.
- A removed endpoint counts as called only where a host in front of its path, if any, is on
  the provider's domain and not the one it moved to, so OpenAI's `/v1/chat/completions` is
  not Hugging Face's. An SDK call counts only in a file that names the API, and only when
  the replacement does not look the same to the SDK; an endpoint that moved to the same
  path on another host counts wherever code still names the old host.
- The search is several times faster on large repositories: value and field names are
  looked for as plain text before their patterns run.

## [0.7.4] - 2026-10-27

### Added

- `tiltmeter fix` checks whether the code the agent changed is still used, before anything
  is committed. A changed JavaScript, TypeScript, Python, Rust or Swift file that nothing
  else in the repository imports or names gets a warning with the date it last changed, and
  a line in the pull request. When every edit is in such a file and none changed in the last
  180 days, the confirmation defaults to no and says why, and `--yes` keeps the commit local
  and exits with code 1; `--allow-unused` opens the pull request anyway, in `fix` and
  `notices apply`. ospfranco/sol had closed a fix for `src/lib/weather.ts`, which the app
  stopped using in 2024. Go, Java, Ruby and other languages that find files on their own are
  never flagged.

## [0.7.3] - 2026-10-27

### Fixed

- The affected-repository search finds a path that code joins right after an interpolation
  holding the slash, such as Semantic Kernel's `$"{endpoint}{separator}pipeline/feature-extraction/{model}"`,
  so a removed endpoint called that way reaches the agent.

### Changed

- For a removed endpoint without `moved_to`, the agent uses the replacement the change's note
  describes, such as Slack's three calls in place of `files.upload`, instead of always leaving
  the call for a human. With no replacement named, it still leaves it.

## [0.7.2] - 2026-10-27

### Added

- The fix agent reads the repository's contribution rules (`AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`, a pull request template) and follows what they ask of the change, such
  as a changelog entry. Rules that edits cannot satisfy, such as a linked issue or a signed
  CLA, are listed for a human. The commit and the pull request take a title in the
  repository's convention, read from those rules or the latest commit titles, when the
  agent finds one. Maintainers had asked for both.

### Changed

- For a retired value whose note says requests to the replacement must leave something out,
  such as `temperature` or a thinking budget, the agent adapts the code that builds requests
  for that model instead of leaving it for a human. A fix that only swaps the model id would
  still fail with a 400 error.
- For a retired value, the agent leaves an alias or a name that spells the old model, such
  as a `llama-3.1` shortcut, as it is and reports it, instead of pointing it at the
  replacement: whoever picks that name expects that model.

### Fixed

- Runs started together on a new history file no longer fail with "table shapes already
  exists": the schema is created under SQLite's write lock, after reading its version
  again. A migration that fails now leaves the history as it was instead of half upgraded.
- Opening the history while another run holds a lock no longer fails now and then with
  "database is locked": switching it to WAL, which SQLite does not wait for, is retried for
  as long as the busy timeout.

## [0.7.1] - 2026-10-26

### Changed

- For a retired value, the agent changes what the code sends and leaves alone code that only
  recognizes or describes the old value: lookup tables, capability and pricing lists, model
  profiles and comparisons. Fixes for retired models had moved such entries to the new
  model, which may not share their traits.
- A fix for a notice that only retires values is titled after them, such as "Replace the
  models api.anthropic.com retired", instead of "Adapt to the api.anthropic.com API
  change".
- A fix for retired values shows the agent, and lists in the pull request, only the ones
  the search found in the code, so a repository that sends one retired model gets a
  pull request about that model. A note on a retired value is labelled with the value.

## [0.7.0] - 2026-10-26

### Added

- Two change kinds for provider notices. `endpoint_removed` says an endpoint is gone, and
  with `moved_to` where it lives on; the endpoint's other changes then compare the old
  response with the new one's. `value_retired` says requests can no longer send a value,
  such as a retired model id, and `replaced_by` what to send instead. The fix agent calls
  the new endpoint or sends the new value, and the pull request lists both. Readers before
  this release reject notices that use them.
- Spec diffs report operations that are gone as `endpoint_removed` instead of leaving
  them for a human, and guess where each one moved: a new operation with the same method,
  a path one segment away (`/playlists/{id}/tracks` and `/items`, `/search` and
  `/search/jql`) and a response alike enough.
- The affected-repository search finds retired values spelled whole in code, so a model
  id names its API without a host or an SDK. A removed endpoint counts only where code
  spells its whole path, so code already on its replacement is left alone. The provider's
  domain counts as the API's name, for APIs where every customer has a host of its own
  (`your-domain.atlassian.net`), and so does a literal path of four segments or more, such
  as `/rest/api/3/search`, when the host comes from the configuration. A path quoted
  without its leading slash (`"rest/api/3/search"`) counts as that path, and a removed
  endpoint of two literal segments or more counts when code spells it after a configured
  base URL (`${baseURL}/artists/${id}/top-tracks`). Calls of removed endpoints come first
  in what the agent is shown.

### Changed

- A pull request or commit from an unsigned notice no longer says the provider announced
  the change: anyone can write such a notice, so it says which notice describes the change
  and that the provider did not sign it. Pull requests now end with "This pull request was
  generated automatically by Tiltmeter".
- When `fix.test_command` is set, the agent no longer says in its summary that it did not
  run the tests: Tiltmeter runs them, and the pull request reports how they went.
- In a pull request from a notice, a removed endpoint comes first among its changes, a
  provider note on a whole endpoint names that endpoint, and a note shared by several
  endpoints shows once.
- Spec diffs find fields that moved into a new object. The new object is still reported
  once, as a whole, but the fields inside it, up to three properties deep, now count as
  places a removed field went when the object was added beside it. On Stripe's 2025-03-31
  API, the hint on `$.subscription` now names `$.parent.subscription_details.subscription`
  and the one on `$.payment_intent` names `$.payments.data[*].payment.payment_intent`, so
  notices carry `renamed_to` and `tiltmeter fix` follows the move instead of finding no
  replacement. The new object's hint lists the fields it possibly replaces.

## [0.6.0] - 2026-10-26

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
  only next to them, so an app's own routes are not taken for the provider's. The search
  skips installed and generated code (`node_modules`, `.venv`, `.next`) and agents'
  worktree copies (`.claude/worktrees`), which can outnumber a repository's own files. A fix
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
