# FAQ and troubleshooting

## Using it

### The first check only says "baseline recorded". Is that right?

Yes. The first successful check of an endpoint has nothing to compare against, so it
records the shape and stops. Drift can only show up from the second check on.

### I changed the API and the second check after that says "no drift"

That is intended: each change is reported by the check that sees it, then it becomes part
of the baseline. See [report once](how-it-works.md#report-once). In CI, it also means you
must keep `.tiltmeter/` between runs; the [CI guide](ci.md) shows how.

### How do I start over?

Delete `.tiltmeter/` next to your config. The next check of each endpoint records a new
baseline. There is no per-endpoint reset yet.

### Does it work with GraphQL?

Yes. Declare the endpoint with `method: POST` and the query in `body`. Remember that
GraphQL usually answers errors with HTTP 200 and an `errors` field, so a failing query
shows up as `field_added` for `errors` and, often, `nullable_introduced` for `data`.

### XML, CSV, gRPC?

Not supported. Tiltmeter only understands JSON over HTTP.

### How often should I check?

As often as you'd want to hear about a change, within the API's rate limits. Hourly in CI
works for most vendors. Each check costs `sample_size` requests per endpoint, plus
retries when the API struggles.

### How much do explanations cost?

They only run when a check finds drift at `llm.min_severity` or worse, and a request
carries the changes and some field names, not response bodies. With the default
`claude-haiku-5-5` model, one explanation costs a small fraction of a cent.

### How much does a fix cost?

It depends on how many files the agent reads. On a small service, a fix takes about five
model calls and 30,000 input tokens with `claude-sonnet-5-5`, which is two or three cents.
`fix.max_model_calls` caps a run, and with LangSmith tracing on, each `tiltmeter.fix` run
shows its tokens. With `--agent claude-code`, the run counts against your Claude Code plan
instead, unless `ANTHROPIC_API_KEY` is set.

## Noise

### I get `field_now_optional` or `nullable_introduced` on a list endpoint

Lists of user data often have fields that are present, or non-null, for most records but
not all. If the baseline never saw a record without the field, the first one is reported,
and from then on the field is optional in the baseline. When this is just how the data
looks, lower the severity for that endpoint or ignore the field:

```yaml
    severity_overrides:
      field_now_optional: warning
      nullable_introduced: warning
    ignore_paths:
      - $.data[*].phone
```

A larger `sample_size` or `baseline_window` also helps the baseline see more variety.

### A field that changes all the time keeps alerting

Free-form objects such as `metadata`, `attributes` or `extra` are better ignored:
`ignore_paths: [$.data[*].metadata]`. To ignore a name wherever it appears, use
`$..metadata`.

### An enum alert fired for something that is not an enum

Tiltmeter treats a field as an enum when it repeatedly returns a few short, lowercase or
UPPER_CASE values. Short identifiers that repeat, such as region codes, can look like one.
Lower `enum_value_added` to `info` for that endpoint or ignore the field.

### Latency warnings when the API is just slow sometimes

Raise `latency_factor` for that endpoint, or set it to `null` to turn latency checks off.
A latency regression needs the median to be both `latency_factor` times slower and 250 ms
slower than the baseline.

## Errors

### `environment variable X is not set`

Your config references `${X}` and the variable is missing or empty in the environment
that runs Tiltmeter. In CI, add it to the job's `env`; in an MCP client, to the server's
`env`. Use `${X:-default}` for optional values.

### `unexpected HTTP status 401`

The request reached the API and was rejected. Check the credentials and headers. If the
endpoint used to answer 2xx, the same situation is reported as a breaking
`status_changed` instead.

### `the response body is not valid JSON`

The endpoint answered 2xx with something else, often an HTML login or error page. Check
the URL, redirects and credentials.

### The MCP server doesn't show up in my client

Run the exact command from your client's config in a terminal, such as
`tiltmeter serve --config /path/to/tiltmeter.yaml`. A config error is printed to stderr.
GUI clients often don't see your shell's `PATH`, so use the absolute path that
`which tiltmeter` prints. Logs go to stderr, which most clients show in their MCP logs.

### The `tiltmeter` command conflicts with something

It shouldn't. There is deliberately no `tilt` alias, because `tilt` is the command of
another popular developer tool.

## Data and privacy

### What leaves my machine?

The requests to the APIs you configure, notifications to the targets you configure, and,
only when `ANTHROPIC_API_KEY` is set and drift is found, an explanation request to
Anthropic with the changes and the field names and types of the affected objects. With
LangSmith tracing turned on, traces go to LangSmith. `tiltmeter fix` also sends the change
list and the files its agent reads to Anthropic (see the [fix guide](fix.md#what-leaves-your-machine)).
Nothing else.

### What is stored?

Shapes, status codes, latencies, changes, notes and explanations, in
`.tiltmeter/tiltmeter.db`. No credentials, and no response bodies unless you set
`store_samples: true`. See [how it works](how-it-works.md#privacy-by-design).

## The name

### Why "Tiltmeter"?

A tiltmeter is the instrument volcanologists bolt to the ground to sense tiny
deformations long before an eruption. Tiltmeter senses small changes in the APIs under
your agents before they blow up a production run. And "on tilt" is what happens to an
agent, or a poker player, after a bad surprise.
