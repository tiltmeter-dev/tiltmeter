# How it works

Tiltmeter answers one question for every API you watch: *do the latest responses contradict
what this API has been returning?* This page explains how it builds that picture, which
differences it reports, and how the pieces fit together.

- [Observations and shapes](#observations-and-shapes)
- [The baseline](#the-baseline)
- [What gets reported](#what-gets-reported)
- [Status, content type and latency](#status-content-type-and-latency)
- [Outcomes and errors](#outcomes-and-errors)
- [Report once](#report-once)
- [The pipeline](#the-pipeline)
- [Storage](#storage)
- [Explanations](#explanations)
- [Fixing](#fixing)
- [Provider notices](#provider-notices)
- [Privacy by design](#privacy-by-design)
- [Limitations](#limitations)

## Observations and shapes

A check sends the configured request `sample_size` times and parses each JSON body into a
**shape**: a tree with one node per location in the body. Each node records:

- how many values were seen there, and how many of each JSON type (`object`, `array`,
  `string`, `integer`, `number`, `boolean`, `null`);
- for objects, a child node per property. A property's count is the number of objects that
  contained it, which is how Tiltmeter tells required fields from optional ones;
- for arrays, one merged node for all their items;
- for strings that look **categorical**, the values seen and how often.

A string looks categorical when it is at most 32 characters of lowercase words joined by
`_`, `.` or `-` (`past_due`, `in-progress`, `v1.2`), or of UPPER_SNAKE_CASE (`USD`,
`IN_REVIEW`). The first value that doesn't fit, such as a name, an email, a URL, a
timestamp or a mixed-case id, switches value tracking off for that field, and so does a
33rd distinct value. That keeps personal data out of the history and keeps free text from
being mistaken for an enum.

Two limits keep huge or deep responses cheap: only the first 500 items of each array are
read, and nesting stops being explored after 32 levels.

## The baseline

The baseline of an endpoint is not stored. It is rebuilt before every check by adding up the
shapes of its last `baseline_window` successful checks, 20 by default. Because shapes are
counters, merging is just addition, and old observations age out as new checks arrive.

From the merged shape:

- a property is **required** when it appeared in every object observed at its location, and
  **optional** otherwise;
- a field is an **enum** when it holds 2 to 16 distinct categorical values and they repeat:
  at least 10 occurrences, and at least 3 per distinct value on average.

The baseline also remembers the last HTTP status the endpoint answered with, the media
type of its last JSON response, and the median latency of the window.

## What gets reported

The comparison walks the latest observation and the baseline side by side and reports only
what the observation **contradicts**. Seeing less than before is never a change: a
missing optional field, or no `null` this time for a field that is sometimes `null`, is
normal variation. Seeing something the baseline never saw is.

| Kind | Reported when | Default |
|---|---|---|
| `field_removed` | A required property is absent from every observed object. | breaking |
| `field_now_optional` | A required property is absent from some observed objects. | breaking |
| `type_changed` | A location holds a type the baseline never saw there. An integer where the baseline has numbers is fine; a float where it only has integers is not. | breaking |
| `nullable_introduced` | A location holds `null` for the first time. | breaking |
| `enum_value_added` | An enum field returns a value it never returned. Values that don't look categorical are reported without being shown. | warning |
| `field_added` | A property appears that the baseline never saw. Its subtree is reported once, as a whole. | info |

When a removed field and an added field share the same parent object and the same
non-null type, Tiltmeter marks them as a **probable rename**. It pairs them when their
names are similar (one contains the other once case, `_` and `-` are ignored, or they are
at least 70% alike), or when they are the only candidates under that parent. A removed
field left without a pair is a **probable move** when one field with the same name and
type appears nearby, at most three path segments away, and no other field of that name and
type goes or appears: `$.current_period_end` moving to
`$.items.data[*].current_period_end`, as in Stripe's 2025 API. In a spec diff, the fields
inside a new object count too, up to three properties deep, although only the object itself
is reported. Such a field is a probable move however deep it sits, provided its new object
was added beside the removed field: the same Stripe release moved `$.subscription` to
`$.parent.subscription_details.subscription` and `$.payment_intent` to
`$.payments.data[*].payment.payment_intent`, and the new object's hint lists the fields it
possibly replaces. An "id or object" field inside it counts as one field and is not opened.
The hint is evidence for a human or an LLM to weigh; the two changes are still reported
separately.

`ignore_paths` removes whole subtrees from the walk. See the
[configuration reference](configuration.md#paths-and-ignore-patterns).

## Status, content type and latency

Three checks look at the response as a whole:

- **`status_changed`** when the status differs from the last status the endpoint answered
  with. Going from 2xx to anything else is breaking, coming back to 2xx is info, and any
  other move is a warning. When the status is not 2xx, the body is not compared.
- **`content_type_changed`** when a 2xx response stops being valid JSON after the endpoint
  used to return JSON, such as a login page or an HTML error.
- **`latency_regression`** when the median latency of the check is more than
  `latency_factor` times the baseline median and at least 250 ms slower, provided the
  baseline has three or more latency measurements.

## Outcomes and errors

Every check ends with one outcome:

| Outcome | Meaning |
|---|---|
| `baseline` | First successful check of the endpoint: the shape is recorded, nothing is compared. |
| `clean` | Nothing contradicts the baseline. |
| `changed` | Only info-level changes, such as a new field. |
| `drift` | At least one change at warning level or worse. |
| `error` | The API could not be verified. |

A check is an `error` when the request fails for good after its retries, when a
`${VAR}` placeholder has no value, when the response is larger than `max_body_bytes`,
when the status is not 2xx and no status change explains it, or when the very first 2xx
response is not JSON. Errors are recorded in the history like any other check. They
never change the learned shape; only their HTTP status, when there is one, becomes the
status the next check compares against.

## Report once

The shape of every successful check joins the baseline window, including the check that
found drift. So the next check compares against a baseline that already contains the new
shape, and the change is not reported again. A field renamed today shows up in today's
check, and tomorrow both names are simply optional fields in the baseline, until the old
one ages out of the window.

That makes Tiltmeter a changelog of what your vendors did, not a stuck alarm. In CI, it
means you must keep the `.tiltmeter/` folder between runs, as the [CI guide](ci.md)
shows, or every run starts from an empty baseline.

## The pipeline

Each check is one run of a small [LangGraph](https://github.com/langchain-ai/langgraph)
`StateGraph`:

```mermaid
graph TD;
  start([start]) --> observe
  observe -. fetch failed .-> persist
  observe --> load_baseline --> compare
  compare -. drift worth explaining .-> explain --> persist
  compare --> persist
  persist -. a notifier wants it .-> notify --> finish([end])
  persist --> finish
```

| Node | What it does |
|---|---|
| `observe` | Expands placeholders, sends the requests and builds the observed shape. |
| `load_baseline` | Rebuilds the baseline from the history. |
| `compare` | Runs the deterministic diff and the severity rules. |
| `explain` | Asks Claude to explain the drift. Runs only when explanations are on, an API key is set and the worst change reaches `llm.min_severity`. |
| `persist` | Saves the check, its changes and its shape, then applies retention. |
| `notify` | Sends the result to every notifier whose threshold it meets, in parallel. |

LangGraph only sequences these steps and routes between them. Everything that decides
what counts as drift lives in `tiltmeter.core`, which is plain Python with no LangChain
imports and is type-checked with `mypy --strict`. Dependencies such as the store, the
HTTP client and the explainer reach the nodes through LangGraph's runtime context, so the
graph state only carries data. Endpoints are checked concurrently, up to `concurrency` at
a time, one graph run each. There is no checkpointer, because a run takes seconds and is
recorded in the history anyway.

With `LANGSMITH_TRACING=true` and a `LANGSMITH_API_KEY`, each run appears in LangSmith as
`tiltmeter check <endpoint>`, with the explanation call nested inside.

## Storage

The history is one SQLite file, `.tiltmeter/tiltmeter.db`, in WAL mode so the MCP server
can read while a scheduled check writes. It holds these tables:

| Table | Rows |
|---|---|
| `shapes` | Observed shapes, stored once per distinct content and keyed by the SHA-256 of their canonical JSON. An API that keeps returning the same structure costs one row. |
| `checks` | One row per check: time, status, media type, latency, outcome details, notes, explanation and a reference to its shape. |
| `changes` | One row per reported change, linked to its check. |
| `notices` | One row per provider notice handled: its provider and id, what was done (not affected, pull request opened, failed...), and how many attempts it took. |
| `subscriptions` | When each notice feed was first followed, so a first run skips old history. |
| `spec_baselines` | The last version of each followed OpenAPI spec, compressed, which the next run compares against. |
| `spec_changes` | Each change found in a followed spec, kept as it was found until it is handled. |

After each check, Tiltmeter keeps the newest `keep_checks` checks of that endpoint, never
fewer than its `baseline_window`, and deletes shapes nothing refers to anymore.

## Explanations

When the `explain` node runs, Claude receives:

- the endpoint id, method, description and URL without its query string;
- the detected changes, up to 60;
- for up to 8 affected objects, their field names and types before and after;
- the first 4,000 characters of the latest response, only with `llm.include_samples: true`,
  wrapped in tags and labelled as untrusted third-party data.

The call goes through LangChain's `ChatAnthropic` with structured outputs, so the reply
always parses into `summary`, `likely_cause`, `impact`, `suggested_action`,
`probable_renames` and `confidence`. The default model is `claude-haiku-5-5` at low effort,
because explanations only run when drift is found. A refusal, a truncated reply or an API
error becomes a note on the check, never a failed check.

## Fixing

`tiltmeter fix <check-id>` turns a check into a pull request. It is separate from the
check pipeline and only runs when you ask:

1. The check's removed, renamed, retyped, optional and nullable fields become *targets*.
   Renames come from the diff's hint, or from Claude's `probable_renames` when the diff had
   none.
2. A new branch is checked out from HEAD as a git worktree in a temporary folder, so the
   user's checkout is never touched.
3. A [LangChain Deep Agent](https://github.com/langchain-ai/deepagents) works in that
   worktree with file tools only (list, read, glob, grep, edit, write). The shell,
   subagent and delete tools are removed, and `.git` plus the `fix.ignore` paths are denied.
   Files the harness writes for itself, such as offloaded tool results, go to memory
   instead of the worktree. With `fix.agent: claude-code`, `claude -p` does the same job
   instead: `--restricted` with only the Read, Edit, Write, Glob and Grep tools, deny rules
   for `.git` and `fix.ignore`, and no MCP servers, hooks or user settings. With
   `fix.agent: codex`, `codex exec` keeps its shell, inside Codex's OS sandbox with a
   permission profile that denies reads outside the worktree, writes outside it, the
   network and `fix.ignore`, and with web search, apps, plugins and the browser turned
   off. Either way the
   agent ends with a structured report: summary, files changed, what is left for a human,
   and confidence.
4. Tiltmeter stages the edits, refuses any diff that touches an ignored path, and shows
   the diff. It also searches the rest of the worktree for each changed JavaScript,
   TypeScript, Python, Rust or Swift file, the way code reaches it (imports, `mod`
   declarations, file names in configs, Swift's top-level type names), and reads from git
   when the files it does not find last changed. On approval it commits, pushes and opens
   the pull request with `gh`. When every edit is in such a file, unchanged for over 180
   days, the commit stays local unless `--allow-unused` is passed
   ([details](fix.md#code-nothing-uses)).

The [fix guide](fix.md) has the flags and the review checklist.

## Provider notices

A provider can announce a change before it ships, in a signed
[change notice](notice-spec.md) that uses the same paths and change kinds as drift. On the
provider's side, `tiltmeter notice create --from-openapi` turns each operation's success
response schema into a shape, as if responses had been observed: required properties in
every object, optional ones in some, enums with their values, `nullable` as `null`. The two
versions then go through the same diff as two checks, in a mode that also reports an
optional field that disappeared, since a spec lists every field. `--from-check` reuses a
stored check instead.

Large specs reuse their named schemas everywhere: Stripe makes most fields "an id or the
expanded object", and copying every `$ref` into place would build millions of nodes for a
single operation. So a reference stays a leaf: each named schema is flattened once, each
pair of old and new schemas is compared once, and an operation reports the changes of
every pair it reaches at the shortest path that reaches it, through at most one "id or
object" field. A month of changes to Stripe's 1,500 schemas compares in under a second.

A reference that points at another file, or at a schema the spec does not define (real
specs have both), stands for an unknown schema: it is compared with nothing, so neither
side reports changes below it, and the output counts such references.

An operation that is gone is reported as **`endpoint_removed`**. It is a **probable move**
when exactly one new operation has the same method, a path one literal segment away (one
replaced, added or dropped: `/playlists/{id}/tracks` and `/playlists/{id}/items`,
`/search` and `/search/jql`) and a response alike enough: the two responses share at
least a quarter of the property names at their top when the new path keeps every old
segment, and half when a segment was replaced, since unrelated resources such as
`/assistants/{id}/files` and `/containers/{id}/files` share paths of that kind. No other
candidate may share as many. Its other changes then compare the old response with the new operation's. A new
operation that two gone ones could have moved to is neither's.

A customer can follow a spec too (`openapi:` in `subscriptions`). The first apply keeps it
as a baseline, or compares it with the `baseline` URL the subscription names; later runs
compare the latest version with the stored one in the same way, pick the endpoints the
repository uses, and keep the change as it was found, with the baseline moved past it, so
a retry works on the same change.

On the customer's side, `tiltmeter notices apply`:

1. downloads each subscribed feed and validates every notice on its own;
2. checks each signature against the key file on the provider's domain, and drops
   unsigned notices, tampered ones and those already handled;
3. searches the tracked files for the notice's hosts, endpoint paths and SDKs, then for the
   changed field names near those uses, with no LLM. A path also counts quoted without its
   leading slash (`"rest/api/3/search"`), and one of four segments or more names its API
   even when the host comes from the configuration. A retired value, such as a model id,
   counts on its own when code spells it whole, and a removed endpoint only where code
   spells its whole path. A repository that does not use them is recorded as not affected;
4. otherwise turns the notice into the same fix request a check would produce (the
   renames come from `renamed_to`, the severities from the default rules) and runs the
   fix, with the matching files as starting points and the provider's text marked as
   untrusted data;
5. records the outcome, so each notice is handled once.

The [notices guide](notices.md) walks through both sides.

## Privacy by design

- **Secrets** are expanded only inside the request code, then scrubbed from error messages
  and excerpts. Config objects, the graph state, prompts and traces only ever see the
  `${VAR}` placeholders.
- **Response bodies** are reduced to shapes as soon as they arrive. The only values kept
  are short categorical strings such as `active`, unless you turn on `store_samples` or
  `llm.include_samples`.
- **Agents** can only check endpoints declared in the config. The MCP tools take an
  endpoint id, never a URL.
- **Fixes** send the change list and the files the agent reads to Anthropic, never response
  values or `${VAR}` values. The agent works in a fresh worktree, which holds no untracked
  files such as `.env`, and the `fix.ignore` paths are denied to its file tools.
- **Notices** are public files fetched without credentials. Only signed ones are applied,
  and the search that decides whether a repository is affected runs locally.

## Limitations

- JSON over HTTP only. XML, CSV, binary and streaming responses are not supported.
- Checks see what the API returns for the requests you declare. A vendor's OpenAPI spec
  is only read when you follow it as a subscription, and its changelog never is.
- It watches responses, not requests: it cannot tell you that a parameter became
  mandatory, only what that did to the response.
- A field that is already optional in the baseline can disappear without an alert.
- Enum detection is a heuristic. It needs repeated categorical values, so a field that
  returns a single value will not alert when a second one appears for the first time.
- Only the first 500 items of each array and the first 32 levels of nesting are
  examined.
- `tiltmeter fix` works from types, not values, so it leaves format and unit questions to
  a human. It finds code by name and does not run your tests.
