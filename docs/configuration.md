# Configuration reference

Tiltmeter reads one YAML file, `tiltmeter.yaml` by default. Every key is validated when the
file is loaded, and unknown keys are rejected, so a typo fails loudly instead of being
ignored.

- [Finding the file](#finding-the-file) (and [building it from a HAR file](#from-a-har-file))
- [A complete example](#a-complete-example)
- [Top-level options](#top-level-options)
- [Endpoints](#endpoints)
- [Placeholders and secrets](#placeholders-and-secrets)
- [Paths and ignore patterns](#paths-and-ignore-patterns)
- [Severity overrides](#severity-overrides)
- [LLM explanations](#llm-explanations)
- [Fix](#fix)
- [Subscriptions](#subscriptions)
- [Notifications](#notifications)
- [Environment variables](#environment-variables)
- [Files on disk](#files-on-disk)

## Finding the file

Every command looks for `tiltmeter.yaml` in the current folder. Point it elsewhere with
`--config` (`-c`) or the `TILTMETER_CONFIG` environment variable:

```bash
tiltmeter check --config ops/apis.yaml
TILTMETER_CONFIG=ops/apis.yaml tiltmeter watch
```

`tiltmeter init [PATH]` writes a starter file with commented examples.

### From a HAR file

If a web app already calls the API, let the browser describe it. Open DevTools, go to the
Network tab, load the pages you care about, then export everything as HAR ("Export HAR" in
Chrome and Edge, "Save All As HAR" in Firefox):

```bash
tiltmeter init --from-har session.har                        # every JSON API the page called
tiltmeter init --from-har session.har --host api.example.com # only this host and its subdomains
```

Tiltmeter keeps the requests that got a 2xx JSON response, one endpoint per method, host
and path (GraphQL requests are told apart by operation). It drops headers the HTTP client
sets itself or that only mean something in a browser (`User-Agent`, `Referer`, `sec-*`,
`If-*` and the like) and the user info in URLs.

| What | Becomes |
|---|---|
| `Authorization: Bearer abc…` | `Authorization: Bearer ${API_EXAMPLE_COM_AUTHORIZATION}` |
| `Cookie`, `X-Api-Key`, any header whose name mentions a token, key, secret, session, auth or password | `${API_EXAMPLE_COM_<NAME>}` |
| Query parameters with such names (`access_token`, `api_key`, `sig`…) | `${API_EXAMPLE_COM_<NAME>}` |
| JSON body fields with such names, at any depth | `${API_EXAMPLE_COM_<NAME>}` |

The original values are never written to the file or printed. `init` lists the variables
to set before the first `tiltmeter check`. Session cookies expire, so prefer an API token
when the service offers one, and delete headers the API works without.

Requests that may change data (POST, PUT, PATCH and DELETE) are left out, because the
monitor would replay them on every check. GraphQL queries count as reads. Add
`--include-writes` for APIs that read through POST, such as search endpoints, and review
each one. Form and file uploads are always skipped: only JSON bodies can be replayed.

## A complete example

```yaml
version: 1
state_dir: .tiltmeter          # history database, relative to this file
concurrency: 5                 # endpoints checked in parallel
keep_checks: 500               # history kept per endpoint
severity_overrides:
  latency_regression: info     # applies to every endpoint

endpoints:
  - id: stripe_customers
    description: Customer list from Stripe
    url: https://api.stripe.com/v1/customers
    method: GET
    params:
      limit: 10
    headers:
      Authorization: Bearer ${STRIPE_API_KEY}
    timeout: 10
    retries: 2
    sample_size: 1
    baseline_window: 20
    ignore_paths:
      - $.data[*].metadata
    severity_overrides:
      field_now_optional: warning

  - id: github_viewer
    description: The authenticated GitHub user, through GraphQL
    method: POST
    url: https://api.github.com/graphql
    headers:
      Authorization: Bearer ${GITHUB_TOKEN}
    body:
      query: "{ viewer { login createdAt } }"
    strict: true

llm:
  enabled: true
  model: claude-haiku-5-5
  effort: low
  min_severity: warning

notify:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK_URL}
    min_severity: warning
  - type: webhook
    url: https://example.com/hooks/tiltmeter
    headers:
      Authorization: Bearer ${HOOK_TOKEN}
    min_severity: breaking
    on_error: true
```

## Top-level options

| Option | Default | Description |
|---|---|---|
| `version` | `1` | Config format version. Only `1` exists. |
| `state_dir` | `.tiltmeter` | Folder for the history database. Relative paths start at the config file's folder; `~` is expanded. |
| `concurrency` | `5` | How many endpoints are checked at the same time, from 1 to 64. |
| `keep_checks` | `500` | Checks kept per endpoint, from 10 to 100,000. Never fewer than that endpoint's `baseline_window`. |
| `severity_overrides` | none | Severity per change kind for every endpoint. See [severity overrides](#severity-overrides). |
| `llm` | see below | [LLM explanations](#llm-explanations). |
| `fix` | see below | The agent behind [`tiltmeter fix`](#fix). |
| `notify` | none | A list of [notifications](#notifications). |
| `subscriptions` | none | API providers whose [change notices](#subscriptions) you follow. |
| `endpoints` | none | The [endpoints](#endpoints) to check. Ids must be unique. A config needs at least one endpoint or one subscription. |

## Endpoints

| Option | Default | Description |
|---|---|---|
| `id` | required | 1 to 64 letters, digits, `_`, `.` or `-`, starting with a letter or digit. Used in commands, history and MCP tools. |
| `url` | required | Must start with `http://`, `https://` or a `${VAR}` placeholder. Put credentials in `params` or `headers`, not here. |
| `method` | `GET` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE`. |
| `description` | none | Shown by `list_endpoints` and passed to the LLM as context. |
| `headers` | none | Request headers. Values may use placeholders. Tiltmeter sends `User-Agent: tiltmeter/<version>` unless you set your own. |
| `params` | none | Query parameters. Values may use placeholders. Numbers and booleans are sent as text. |
| `body` | none | A JSON request body, for GraphQL and other POST APIs. Strings anywhere inside may use placeholders. |
| `timeout` | `10` | Seconds per request, up to 300. |
| `retries` | `2` | Extra attempts, up to 10, after a timeout, a connection error or HTTP 429, 500, 502, 503 or 504. Waits 0.5 s, 1 s, 2 s and so on, up to 8 s, or what `Retry-After` asks for, up to 10 s. |
| `sample_size` | `1` | Requests per check, up to 20. Their bodies are merged into one observation, which helps with APIs that return varying data. |
| `max_body_bytes` | `5000000` | Responses larger than this fail the check. |
| `ignore_paths` | none | Parts of the body to leave out of the comparison. See [ignore patterns](#paths-and-ignore-patterns). |
| `strict` | `false` | Report new fields as warnings instead of info, for consumers that reject unknown keys. |
| `baseline_window` | `20` | How many recent successful checks form the baseline, from 1 to 500. |
| `latency_factor` | `3.0` | A check is a latency regression when its median latency is this many times the baseline median and at least 250 ms slower. It needs three earlier successful checks. `null` turns the check off. |
| `severity_overrides` | none | Per change kind, on top of the global ones. |
| `store_samples` | `false` | Keep the first 4,000 characters of each response in the history. Off by default because responses often contain personal data. |

Redirects are followed. Only JSON responses are supported: a 2xx response whose body is
not valid JSON fails the first check, and on later checks it is reported as
`content_type_changed`.

## Placeholders and secrets

Values can reference environment variables:

| Syntax | Meaning |
|---|---|
| `${NAME}` | The value of `NAME`. The check fails with `environment variable NAME is not set` if it is missing or empty. |
| `${NAME:-fallback}` | The value of `NAME`, or `fallback` when it is missing or empty. |

Placeholders work in `url`, `headers`, `params` and `body` of endpoints, and in the URLs
and headers of notifications. They are expanded right before each request, so:

- the expanded values never appear in the history database, the LangGraph state, LLM
  prompts or LangSmith traces;
- any expanded value of four characters or more is replaced with `***` in error messages
  and response excerpts;
- you can rotate a secret without touching the config.

`description` and `id` are never expanded.

## Paths and ignore patterns

Tiltmeter names locations in a response body with a small JSONPath-like syntax. The same
notation appears in reports, in the MCP tools and in `ignore_paths`.

| Syntax | Meaning | Example |
|---|---|---|
| `$` | The whole body. | `$` |
| `.name` | A property whose name is a plain identifier. | `$.data` |
| `["name"]` | Any other property name, as a JSON string. | `$["content-type"]` |
| `[*]` | Every item of an array. | `$.data[*].id` |

Ignore patterns add three conveniences:

| Pattern feature | Example | Matches |
|---|---|---|
| Prefix matching | `$.metadata` | `metadata` and everything below it |
| `*` and `?` in dotted names | `$.data[*].created_*` | `created_at`, `created_by`... in every item |
| `..` for any depth | `$..updated_at` | every `updated_at`, wherever it is |

Bracketed names are literal, so `$["a.*"]` only matches a property called `a.*`. Use
ignore patterns for free-form objects such as `metadata`, and for fields that legitimately
come and go. Status, content-type and latency checks are not affected by them.

## Severity overrides

These are the change kinds and their default severities:

| Kind | Default |
|---|---|
| `field_removed` | `breaking` |
| `field_now_optional` | `breaking` |
| `type_changed` | `breaking` |
| `nullable_introduced` | `breaking` |
| `content_type_changed` | `breaking` |
| `status_changed` | `breaking` from 2xx to an error, `info` from an error to 2xx, `warning` otherwise |
| `enum_value_added` | `warning` |
| `latency_regression` | `warning` |
| `field_added` | `info`, or `warning` with `strict: true` |

Override them with `info`, `warning` or `breaking`, globally or per endpoint. An endpoint's
overrides win over the global ones, and an override replaces the default entirely,
including the direction-based rule for `status_changed`.

```yaml
severity_overrides:          # every endpoint
  latency_regression: info

endpoints:
  - id: search
    url: https://api.example.com/search
    severity_overrides:      # this endpoint only
      field_now_optional: warning
```

## LLM explanations

When a check finds drift at `min_severity` or worse and `ANTHROPIC_API_KEY` is set,
Tiltmeter asks Claude to explain it. Without the key, the check says so in a note and
carries on.

| Option | Default | Description |
|---|---|---|
| `enabled` | `true` | Turn explanations off entirely. |
| `model` | `claude-haiku-5-5` | Any Claude model id. |
| `effort` | `low` | `low`, `medium`, `high`, `xhigh` or `max`. Tiltmeter also turns on adaptive thinking whenever an effort is set. Use `null` for models without adaptive thinking, such as `claude-haiku-4-5`. |
| `max_tokens` | `2048` | Output budget per explanation, from 256 to 32,000. |
| `timeout` | `60` | Seconds per request, up to 600. |
| `min_severity` | `warning` | Only explain drift at this level or worse. |
| `include_samples` | `false` | Also send the first 4,000 characters of the latest response, labelled as untrusted third-party data. Off by default because responses often contain personal data. |

`--no-llm` skips explanations for one run, and `tiltmeter explain <check-id>` explains a
past check. If the model refuses, runs out of tokens or the API fails, the check still
succeeds and the problem is recorded as a note.

## Fix

`tiltmeter fix <check-id>` hands the drift a check found to an agent that adapts your code
and opens a pull request. The default agent needs `ANTHROPIC_API_KEY` and the `fix` extra
(`pip install "tiltmeter[fix]"`); `claude-code` and `codex` run on your Claude Code or Codex
login instead. The
[fix guide](fix.md) explains the whole flow.

| Option | Default | Description |
|---|---|---|
| `agent` | `deepagents` | `deepagents` runs a LangChain Deep Agent on `ANTHROPIC_API_KEY`. `claude-code` runs `claude -p` on your Claude Code login. `codex` runs `codex exec` on your Codex login. `--agent` overrides it for one run. |
| `model` | `claude-sonnet-5-5` | The model the agent runs on. With `codex`, a Claude model name is ignored and Codex uses its default; set an OpenAI model to pick one. |
| `effort` | `medium` | `low`, `medium`, `high`, `xhigh` or `max`, with adaptive thinking. Use `null` for models without adaptive thinking. Codex gets it as its reasoning effort, with `max` as `xhigh`. |
| `max_tokens` | `16000` | Output budget per model call, from 1,024 to 64,000. Deep Agent only. |
| `timeout` | `120` | Seconds per model call, up to 600. With Claude Code and Codex, the whole run may take `timeout` × `max_model_calls`. |
| `max_model_calls` | `40` | Model calls per fix (Claude Code turns), from 1 to 500. The run fails when the agent needs more; raise it for large repositories. Codex has no turn limit, so for Codex it only sets the time limit. |
| `branch_prefix` | `tiltmeter/fix` | Branches are named `<prefix>-<endpoint>-<check id>` unless `--branch` is given. |
| `ignore` | `.env*`, `*.pem`, `*.key`, `secrets*`, `.tiltmeter` | Paths the agent can never read or write. A pattern without a slash matches a name at any depth; one with a slash matches from the repository root. `.git` is always blocked. |
| `test_command` | none | A command that runs the repository's tests, as a list or a string split like a shell would (`python -m pytest -q`). It runs on the agent's edits, without a shell, before anything is committed; on a failure the agent sees the output and edits again. [How it runs](fix.md#running-the-tests). |
| `test_timeout` | `300` | Seconds the tests may take, up to 3,600. |
| `test_rounds` | `1` | How many more times the agent may edit after the tests fail, from 0 to 3. |
| `test_env` | none | Names of environment variables the tests get, besides `PATH` and the locale, such as `UV_CACHE_DIR` or `GOMODCACHE`. Nothing else in your environment reaches them. |
| `test_network` | `false` | Let the tests reach the network. Without it they run in a network namespace of their own (Linux) or under `sandbox-exec` (macOS), and are skipped where neither is available. |

## Subscriptions

API providers can announce changes in signed [change notices](notice-spec.md), and many
publish an OpenAPI spec. `tiltmeter notices check` lists what is new in either, and
`tiltmeter notices apply` hands what affects your repository to the fix agent. The
[notices guide](notices.md) explains the flow.

```yaml
subscriptions:
  - provider: api.partner.example                        # its /.well-known/api-changes.json
  - feed: https://status.other.example/api-changes.json   # any https feed
  - provider: payments.example                           # a feed elsewhere, this provider only
    feed: https://changes.example.org/payments.json
    since: 2026-11-01
  - openapi: https://raw.githubusercontent.com/acme/api/main/openapi.json  # follow a spec
    baseline: https://raw.githubusercontent.com/acme/api/v1.4.0/openapi.json  # what the code targets
    sdks: ["pypi:acme"]
```

| Option | Default | Description |
|---|---|---|
| `provider` | none | The provider's domain, in lowercase. Alone, the feed is `https://<provider>/.well-known/api-changes.json`. With `feed`, only this provider's notices in that feed count. With `openapi`, the provider changes are attributed to; the host of the spec's server by default. |
| `feed` | none | An `https` URL of a feed. Each notice in it is verified against its own provider's key. |
| `since` | none | Skip notices published before this date or time. Without it, the first run skips notices published more than 30 days before you subscribed that were already in effect then. |
| `openapi` | none | An `https` URL of the provider's OpenAPI or Swagger spec, JSON or YAML. Tiltmeter keeps the last version and turns changes to its responses into entries, [as described here](notices.md#following-a-spec-instead). Does not go with `feed` or `since`. |
| `baseline` | none | With `openapi`: an `https` URL of the spec version the code was written against. The first run compares the latest version with it instead of only keeping the latest, so a repository that is already behind gets its pull request on day one. Read once, while no baseline is stored. |
| `base_url` | the spec's first server | With `openapi`: what the paths start from, when the spec's server is relative or templated. |
| `sdks` | none | With `openapi`: the provider's SDKs as `ecosystem:name` (`pypi:stripe`, `npm:@acme/sdk`), so code that uses them counts as using the API. |
| `versioned` | `false` | With `openapi`: clients pin an API version, so a change that comes with a new `info.version` applies only when the repository mentions that version. |
| `max_bytes` | `32000000` | With `openapi`: the largest spec to download. |

Each entry needs `provider`, `feed`, `openapi`, or `provider` with one of the other two.
Feeds and specs are public files: no headers or credentials are sent. What was done about
each notice, when each feed was first followed and the last version of each spec are kept
in the history database, so keep `.tiltmeter/` between runs as you do for baselines.

## Notifications

Each entry in `notify` gets the results at or above its `min_severity`. Notifications are
sent after the check is saved, with a 10-second timeout. Delivery failures are recorded as
notes on the check and never include the target URL, which is usually a secret.

### Slack

```yaml
notify:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK_URL}   # an incoming webhook URL
    min_severity: warning               # default
    on_error: false                     # default; true also sends checks that could not run
```

The message lists up to 15 changes, the explanation summary and suggested action when
there is one, and the check id.

### Webhook

```yaml
notify:
  - type: webhook
    url: https://example.com/hooks/tiltmeter
    headers:
      Authorization: Bearer ${HOOK_TOKEN}
    min_severity: breaking
    on_error: true
```

Tiltmeter sends a `POST` with this JSON body. `event` is `drift.detected` or
`check.failed`, and `result` is the same object that `tiltmeter show <id> --json` prints:

```json
{
  "event": "drift.detected",
  "tiltmeter_version": "0.5.0",
  "result": {
    "endpoint_id": "billing_customers",
    "check_id": 2,
    "checked_at": "2026-10-21T10:00:00Z",
    "status_code": 200,
    "media_type": "application/json",
    "latency_ms": 2.1,
    "first_run": false,
    "changes": [
      {
        "path": "$.data[*].phone",
        "kind": "field_removed",
        "severity": "breaking",
        "before": "string",
        "after": null,
        "hint": "possibly renamed to $.data[*].phone_number",
        "message": "Field $.data[*].phone (string) is no longer returned. Possibly renamed to $.data[*].phone_number."
      }
    ],
    "max_severity": "breaking",
    "explanation": null,
    "explanation_model": null,
    "error": null,
    "notes": [],
    "outcome": "drift"
  }
}
```

`outcome` is one of `error`, `drift` (warning or worse), `changed` (info only), `baseline`
(first check) or `clean`.

## Environment variables

| Variable | Used for |
|---|---|
| `TILTMETER_CONFIG` | Default config file path. |
| `ANTHROPIC_API_KEY` | Turns on LLM explanations. Required by `tiltmeter fix` with the default agent. |
| `TILTMETER_SIGNING_KEY` | The private key `tiltmeter notice sign` and `notice publish` use when `--key` is not given: a path, or the PEM itself, as a CI secret holds it. |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Optional LangSmith tracing of every check, explanation and fix. |
| Anything your config references | Expanded into `${...}` placeholders. |

## Files on disk

| Path | Contents |
|---|---|
| `tiltmeter.yaml` | Your configuration. Safe to commit: secrets belong in environment variables. |
| `.tiltmeter/tiltmeter.db` | SQLite history: checks, changes, observed shapes, what was done about each provider notice and the last version of each followed spec. Keep `.tiltmeter/` out of git; `tiltmeter init` adds it to `.gitignore` when it finds one, or a git repository, next to the config. |

Delete `.tiltmeter/` to forget every baseline and start over. The next check of each
endpoint records a new baseline.
