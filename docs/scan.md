# Scanning for known API changes

`tiltmeter check` tells you when an API changes from now on, and a provider's feed brings
news from the day you subscribe. Neither tells you about the changes that already shipped:
the models Groq shut down in August, the Jira search endpoint that answers 410, the Stripe
field that moved in 2025. `tiltmeter scan` does. It searches your code for every entry of a
catalog of known API changes that comes with Tiltmeter, and it needs no configuration:

```console
$ uvx tiltmeter scan
✗ api.groq.com  Groq no longer serves 16 models it retired, the latest on August 16, 2026.  took effect 2026-08-16
    groq-retired-models-2026-08 · unsigned: drafted by Tiltmeter from the provider's public documentation
    src/llm.js:37  llama-3.1-8b-instant: retired; use openai/gpt-oss-20b
        groq: 'llama-3.1-8b-instant',
    src/store.js:108  llama-3.3-70b-versatile: retired; use openai/gpt-oss-120b
        groq: { fast: 'llama-3.1-8b-instant', smart: 'llama-3.3-70b-versatile' },
    fix: tiltmeter fix --notice groq-retired-models-2026-08 --allow-unsigned --repo .

1 of 15 known API changes affects .: 2 lines in 2 files.
The catalog's entries are drafted by Tiltmeter from each provider's public documentation; no provider signed or endorsed them.
```

Nothing is recorded, no agent runs, and the bundled catalog is read from the installed
package, so nothing is fetched or sent anywhere.

When there are no matches, the summary says how many catalog entries and files it checked.
That means no known catalog change was found in the scanned code; it does not mean that
your APIs are covered or that no unlisted API changes exist. To monitor an API you own, run
`tiltmeter init`, review or replace its example endpoint, then run `tiltmeter check` to
record the first baseline.

- [What the catalog holds](#what-the-catalog-holds)
- [Reading the output](#reading-the-output)
- [Options and exit codes](#options-and-exit-codes)
- [Before a change takes effect](#before-a-change-takes-effect)
- [In GitHub code scanning](#in-github-code-scanning)
- [The release and API version you are on](#the-release-and-api-version-you-are-on)
- [Fixing what it finds](#fixing-what-it-finds)
- [What counts as a use](#what-counts-as-a-use)
- [Blind spots](#blind-spots)
- [Adding an entry to the catalog](#adding-an-entry-to-the-catalog)

## What the catalog holds

The catalog, `src/tiltmeter/catalog/api-changes.json`, is an ordinary
[feed](notice-spec.md) of change notices, so every entry is validated like any provider's
notice. Tiltmeter drafted each entry from the provider's public documentation, deprecation
pages and changelogs, and says so at the end of its summary. **No entry is signed or
endorsed by its provider**, and the output marks every one as unsigned.

| Entry | Provider | What changed | In effect |
|---|---|---|---|
| `anthropic-claude-sonnet-4-5-retirement-2026-11-30` | api.anthropic.com | Claude Sonnet 4.5 (`claude-sonnet-4-5-20250929`) retires. | 2026-11-30 |
| `openai-gpt-image-shutdown-2026-12-01` | api.openai.com | `gpt-image-1.5`, `gpt-image-1-mini` and `chatgpt-image-latest` shut down. | 2026-12-01 |
| `openai-gpt-5-o3-shutdown-2026-12-11` | api.openai.com | `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-pro`, `o3`, `o3-pro` and their snapshots shut down. | 2026-12-11 |
| `openai-gpt-4-series-shutdown-2027-01-14` | api.openai.com | Five dated GPT-4o, GPT-4o mini, GPT-4.1 and GPT-4.1 mini snapshots shut down. | 2027-01-14 |
| `hubspot-pipelines-v1-sunset` | api.hubapi.com | The Pipelines API v1 is sunset; v3 renames `pipelineId`, `stageId` and `active` and returns dates as strings. | 2026-12-04 |
| `anthropic-retired-models-2026-10-27` | api.anthropic.com | 12 retired Claude model versions, from Claude 2.0 to Claude Opus 4.1. | 2026-08-05 |
| `groq-retired-models-2026-08` | api.groq.com | 16 models Groq no longer serves, Llama, Mixtral, Gemma, DeepSeek and Qwen ones. | 2026-08-16 |
| `groq-retired-models-2026-09` | api.groq.com | 8 more models Groq no longer serves: Compound, Qwen 3 and 3.6, Kimi K2, Llama Guard 4 and PlayAI text to speech. | 2026-09-21 |
| `openai-retired-models-2026-10-23` | api.openai.com | 36 retired GPT-3.5, GPT-4, o1, o3-mini, o4-mini, davinci and DALL-E models. | 2026-10-23 |
| `spotify-2026-02-development-mode` | api.spotify.com | Web API endpoints and fields gone or moved for Development Mode apps. | 2026-03-09 |
| `huggingface-serverless-inference-api-retired` | huggingface.co | The serverless Inference API at `api-inference.huggingface.co`, replaced by `router.huggingface.co`. | 2025-07-01 |
| `stripe-2025-03-31-subscription-periods` | api.stripe.com | `current_period_start` and `current_period_end` moved from the Subscription to its items, from API version 2025-03-31. | 2025-03-31 |
| `jira-cloud-change-2046-search-jql` | atlassian.net | Jira Cloud's `/rest/api/3/search` and `/rest/api/2/search`, replaced by `/search/jql`. | 2025-05-01 |
| `openweathermap-one-call-2-5-closed` | api.openweathermap.org | One Call API 2.5, replaced by 3.0. | June 2024 |
| `slack-files-upload-sunset` | slack.com | `files.upload`, replaced by a three-step upload. | 2025-11-12 |

The scan considers every entry, however old: the point is changes that already shipped,
and those with a date ahead, which it lists with the days left
([below](#before-a-change-takes-effect)). Some apply only to some customers. The Stripe change applies only to accounts and requests
on API version 2025-03-31 or later, Spotify's only to apps in Development Mode, and Groq's
shutdowns since July 2026 only to free and developer-tier keys. The entry's notes say so, and
the scan cannot tell which case your code is in.

## Reading the output

For each change the folder uses:

- a line with the provider, the first sentence of the entry's summary and the date it took
  effect with how long ago that was, or the date it takes effect with the days left, or
  the date it was published when it has no such date. What already broke comes first,
  marked `✗`; what has not taken effect yet follows, soonest first, marked `!`;
- the entry's id and where it comes from;
- `only for some callers`, when a change affects only some of them, such as one plan, one
  deployment or one API version, with who it affects. Tiltmeter can't tell which of them
  the folder serves;
- up to five places as `file:line`, with what changes for them and the line itself. The
  surest come first: lines that send a retired value, call a removed endpoint, then read a
  changed field near the API. A line that only names the API, such as its host, shows up
  only when nothing more telling does. The rest is counted;
- `possibly unused` on a file that nothing else in the folder imports or names, by the
  same check `tiltmeter fix` runs before it commits ([details](fix.md#code-nothing-uses)),
  so you don't chase dead code;
- `also in tests`, the places in test files, which never decide on their own;
- the release of the provider's SDK the folder is on and the API versions its code pins,
  when it says so ([below](#the-release-and-api-version-you-are-on));
- the command that fixes it.

A summary line ends the output. Text from the catalog is printed as plain text, never as
markup. `--all` also lists every entry that does not affect the folder, with the reason,
and where tests or docs mention it anyway. `--json` prints the same as one JSON object:
`affected` lists each change with all its `matches` (up to 50), `count`, `files`, `tests`
and `fix_command`, and each match's `applies_to` says who its change affects, or is
`null` for every caller; `unaffected` comes with `--all`; `rejected` lists entries of a
`--catalog` that are not valid notices; `truncated` is true when the folder has more text
files than a scan reads. Each change has `days_left`, the days until it takes effect (zero
or less once it has, `null` without a date), and `due`, whether it counts for the exit code.
`sdk_pins` and `api_versions` list the releases and API versions the folder is on.

## Options and exit codes

```console
$ tiltmeter scan [PATH] [--catalog FILE_OR_URL] [--within DAYS] [--sarif FILE] [--all] [--json]
```

| Option | Description |
|---|---|
| `PATH` | The folder to search, `.` by default. A git checkout is read as git sees it: tracked files and new ones it does not ignore. Any other folder is read file by file. |
| `--catalog FILE_OR_URL` | Search for the entries of another feed instead, such as your team's own: a file or an https URL, fetched like any subscribed feed. A signed entry's signature is checked against its provider's key. |
| `--within DAYS` | Count a change that has not taken effect yet only when it does within `DAYS`. One further away is still listed, marked `·`, and does not make the exit code 1. Without it, every change the folder uses counts. |
| `--sarif FILE` | Also write what was found to `FILE` as [SARIF](#in-github-code-scanning) 2.1.0. The terminal or JSON output and the exit code stay the same. |
| `--all` | Also list the entries that do not affect `PATH`, and why. |
| `--json` | Print JSON instead. |

| Exit code | Meaning |
|---|---|
| 0 | Nothing in the catalog affects `PATH`. |
| 1 | `PATH` uses at least one change in the catalog that is in effect, has no date, or takes effect within `--within` days. |
| 3 | The catalog could not be read. |
| 4 | `PATH` is not a folder, the `--sarif` file cannot be written, or another usage error. |

## Before a change takes effect

An entry whose date is still ahead, such as a model with a shutdown date, is found like
any other, and the scan says how many days are left. In CI that makes the scan a warning
you can time: fail the job for what is broken now or will be within a month, and only list
the rest.

```console
$ tiltmeter scan --within 30
! api.hubapi.com  HubSpot sunsets the Pipelines API v1 on December 4, 2026.  takes effect 2026-12-04, in 24 days
    hubspot-pipelines-v1-sunset · unsigned: drafted by Tiltmeter from the provider's public documentation
    sync.py:5  GET /crm-pipelines/v1/pipelines/{objectType}: removed; moved to GET /crm/v3/pipelines/{objectType}
        return requests.get("https://api.hubapi.com/crm-pipelines/v1/pipelines/{objectType}", ...
    fix: tiltmeter fix --notice hubspot-pipelines-v1-sunset --allow-unsigned --repo .

1 of 15 known API changes affects .: 1 line in 1 file. 1 of them takes effect later: in 24 days.
```

That run exits with 1. With `--within 7` it would list the same change, marked `·`, and
exit with 0 until a week before the date.

Without `--within`, a change counts as soon as the catalog has it, however far its date.

## In GitHub code scanning

`--sarif FILE` writes the scan as [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html),
the format GitHub code scanning, GitLab and most CI systems read. Uploaded to GitHub,
each line shows up as an alert in the repository's Security tab and as an annotation on
the pull request that adds it:

```yaml
permissions:
  contents: read
  security-events: write
steps:
  - uses: actions/checkout@v4
  - run: uvx tiltmeter scan --within 30 --sarif tiltmeter.sarif
    continue-on-error: true   # the alerts carry the result; drop this to also fail the job
  - uses: github/codeql-action/upload-sarif@v3
    with:
      sarif_file: tiltmeter.sarif
      category: tiltmeter
```

| In the scan | In SARIF |
|---|---|
| A catalog entry the folder uses | A rule, with the id `<provider>/<entry id>`, the headline, the summary and the fix command as its help. |
| A line that counts | A result at that file and line, with what the change does to what the line spells. |
| In effect, or undated | Level `error`. |
| Still ahead | Level `warning`, with the date and the days left. |
| Ahead, and further away than `--within` | Level `note`. |
| The catalog could not be read | A run with `executionSuccessful: false`, the reason, and no results. |

Paths start at the repository's root, also when the scan starts in a subfolder, since
that is where code scanning looks for them. Places in tests and docs are left out, as
they never count, and so are the lines past the first 50 of an entry. The text of an
entry is a third party's: it is written as plain text, with `[`, `]` and `\` escaped so
that none of it turns into a link.

## The release and API version you are on

Many changes only reach some callers: those on an old release of the provider's SDK, or
on one version of its API. Whether that is you is written in your own files, so the scan
reads it and says it under the change:

```console
✗ api.stripe.com  Stripe API version 2025-03-31.basil moved subscription billing periods to the items.  took effect 2025-03-31, 19 months ago
    stripe-2025-03-31-subscription-periods · unsigned: drafted by Tiltmeter from the provider's public documentation
    only for some callers: Accounts and requests on API version 2025-03-31.basil or later. ...
    app/billing.py:7  current_period_end: moved to $.items.data[*].current_period_end
        return stripe.Subscription.retrieve(subscription_id)["current_period_end"]
    installs stripe 11.6.0 (uv.lock), which is older than 12.0.0, the first release that follows the change
    asks for stripe ^17.4.0 (web/package.json), which is older than 18.0.0, the first release that follows the change
    pins API version 2024-06-20 (app/billing.py:3), 2025-02-24.acacia (web/renew.ts:2)
```

- **`installs`** is the release a lock file names: `uv.lock`, `poetry.lock`,
  `Pipfile.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile.lock`,
  `Cargo.lock` or `composer.lock`. Lock files are read for this and nothing else.
- **`asks for`** is the requirement a manifest writes, such as `>=11,<12`, `^17.4.0` or
  `~> 12.0`, in `requirements*.txt`, `pyproject.toml`, `Pipfile`, `package.json`, `go.mod`,
  `Gemfile`, `Cargo.toml`, `composer.json`, `pom.xml`, Gradle builds and .NET project
  files. Where a lock file names the SDK, the manifests are left out: the lock file says
  what runs.
- Next to it, the first release that follows the change, from the entry's `sdks`: the one
  you are on **is older**, **already follows the change**, or **may be older** when the
  requirement allows releases on both sides (`>=10`) or cannot be read as a range (a git
  URL, a tag, an "or").
- **`pins API version`** lists dated versions written next to the API, in files that name
  its host or import its SDK: `apiVersion: "2024-06-20"`, `stripe.api_version = "..."`,
  a `Stripe-Version`, `X-GitHub-Api-Version`, `anthropic-version` or `Notion-Version`
  header. Versions that are not dates, such as `v1`, are not listed.

These are facts for you to weigh: what "older" means is the provider's to say. An SDK
that pins its own API version, as Stripe's do, keeps old code working until you upgrade,
while an SDK that calls an endpoint the provider removed breaks until you do. The scan
decides one thing by itself with them: when an entry removes an endpoint, the folder only
reaches it through an SDK, and every place that pins that SDK is at or past the release
that follows the change, the folder is not affected, unless its own code calls the
endpoint too.

`tiltmeter fix` hands the same facts to the agent, which raises a requirement that is
older and leaves alone one that already follows the change, and lists them in the pull
request.

## Fixing what it finds

The command under each change hands it to the [fix agent](fix.md), which adapts the code
in a fresh worktree and opens a pull request:

```console
$ tiltmeter fix --notice groq-retired-models-2026-08 --allow-unsigned --repo .
```

`--notice` takes the id of a catalog entry, besides a notice file or URL. Entries are not
signed by their providers, so `--allow-unsigned` is required, as for any unsigned notice;
the pull request says the entry was drafted by Tiltmeter from the provider's public
documentation, and never puts it in the provider's mouth. For an entry of a `--catalog`,
the command names it as `FEED#ID`. Only signed notices are read from URLs, so an unsigned
entry of a catalog behind a URL has no command: save the catalog to a file and scan with
that. `tiltmeter fix` needs a git repository.

## What counts as a use

The scan runs each entry through the same plain-text search as `tiltmeter notices apply`
([the rules](notices.md#is-my-repository-affected)), with a few more for the line-by-line
view:

- **Code only.** Docs (`.md`, `.mdx`, `.rst`, `.txt`, Quarto and LaTeX chapters and the like,
  changelogs, licenses), data tables and pictures (`.csv`, `.tsv`, `.svg`),
  lock files, ignore files, change notices and feeds (the catalog file included),
  `tiltmeter.yaml`, installed or generated code (`node_modules`, `.venv`, `dist`), binaries,
  files over 1 MB and symbolic links are not read as code. Lock files are read only for
  the release of an SDK they install.
- **Not in comments.** A retired value, a removed endpoint's path or a changed field counts
  only outside comment lines (`#`, `//`, `/* */`, `--`, `<!--`) and Python docstrings. In
  JavaScript and TypeScript `#` opens no comment, so a private member such as
  `#model = "..."` is code. A string that spans lines, like a prompt assigned to a name, is
  code too.
- **Hosts whole.** `slack.com` is named by `slack.com` and by `hooks.slack.com`, not by
  `myslack.com`, `slack.community` or `slack.commands`.
- **Tests apart.** Files in `test`, `tests`, `spec`, `__tests__`, `fixtures` and similar
  folders, or named like `test_*.py`, `*_test.go`, `*.test.ts`, `*.spec.js` or
  `*Test.java`, are searched on their own. They are listed, and never make a folder
  affected: tests feed code made-up data more often than they call the API.
- **Whole values.** A retired value counts spelled whole, not inside a longer id or a file
  name (`gpt-4` is not in `gpt-4o`, `gpt-4.1` or `gpt-4.txt`). A short one made of letters
  and digits, such as `o1`, counts only in quotes.
- **The API's own paths.** A removed endpoint's path counts only with no host in front of
  it or with one on the provider's domain, other than the host it moved to: OpenAI's
  `/v1/chat/completions` is not Hugging Face's. When an endpoint moved to the same path on
  another host, as Hugging Face's did, only code that still names the old host calls it.
- **Methods whole.** A method an entry names for a removed endpoint counts after an
  accessor and before its arguments, in a file that imports its SDK:
  `client.files_upload(` and `web?.files?.upload(` do, `files_upload_v2(` and a
  `files_upload` in a file that imports another library do not.

## Blind spots

The search reads text; it does not run or parse your code. Known gaps:

- **It cannot see configuration.** A model id or base URL that comes from an environment
  variable, a database or a file outside the folder is not found.
- **Mentions are not requests.** A pricing table, a list of models a UI offers, or a check
  such as `name.startswith("o1")` that names a retired model is listed like a request that
  sends it. `tiltmeter fix` leaves such code alone, but the scan cannot tell them apart.
  The same id at another provider (`accounts/fireworks/models/gemma2-9b-it`) or a local
  checkpoint path also counts.
- **A field that moved keeps its name.** Stripe's `current_period_end` now lives on each
  subscription item, so code that already reads `items.data[0].current_period_end`
  matches too. Check that the line reads it from the subscription.
- **A removed endpoint counts by its call.** When an entry removes an endpoint and also
  renames fields of its response, code that calls the endpoint is affected whether or not
  it reads those fields: the call is what stops working.
- **Calls through an SDK.** A removed endpoint is found where code spells its path, calls
  it through an SDK in the usual `resource.verb(` form, or calls a method the entry names
  for it (`sdk_calls`): Slack's entry names `files_upload` in the Python SDK,
  `files.upload` in the Node one, `UploadFile` in Go, `filesUpload` in Java and
  `files_upload` in Ruby. A named method counts only in a file that imports that SDK, so
  another library's method of the same name does not, and neither does
  `files_upload_v2`. For Java, where an import names a class and not the package, the
  `pom.xml` or `build.gradle` that lists the SDK stands for the import. Not seen: a file
  that gets its client from another file and imports nothing from the SDK, a call spread
  over several lines when the scan lists lines (the file still counts), a method reached
  through a wrapper of your own, and Slack's `api_call("files.upload")`.
- **Versions and plans.** The scan lists the API versions your code pins, but cannot tell
  which Stripe API version your account defaults to, whether a Spotify app has extended
  quota, or which Groq tier a key is on. A release set outside the folder, such as a
  version in a Docker image, a constraints file elsewhere or a private registry's alias,
  is not seen either.
- **Generated or vendored code** in folders with unusual names, data files such as JSON
  corpora, and text inside strings (an error message that names a model) are read as code.
- **Large trees.** At most 20,000 text files are read, and the unused-code check gives up
  above that. Pictures, archives and lock files do not count towards the limit. A folder
  with more has its code read first, docs and data tables last, and the scan says so: a
  note after the summary, `truncated` in the JSON, and "so this is not certain" in the
  reason of each entry it did not find. Scan its folders one by one to cover the rest.

## Adding an entry to the catalog

The catalog is meant to grow. To add a change that already shipped:

1. Write the notice as a JSON file, by hand or with
   [`tiltmeter notice create`](notices.md#2-draft-the-notice), following the
   [spec](notice-spec.md). Use the provider's domain, its real endpoint URLs and the change
   kinds the spec lists; a retired model is a `value_retired` change on `$.model`.
2. Keep the text factual and within the limits: a `summary` of at most 500 characters
   whose first sentence says what changed in a few words (the scan shows it as the
   headline), and `note`s of at most 500 that say what the replacement needs. When a
   change applies to some customers only, such as one plan or one deployment, say who in
   `applies_to`, on the entry or on that change. Name your source. When an endpoint is
   removed and the provider's SDKs still have a method that calls it, list each one under
   the endpoint's [`sdk_calls`](notice-spec.md#sdk-calls): code on an SDK spells no URL.
3. End the summary with the sentence `Drafted by Tiltmeter from <Provider>'s public
   documentation; not signed or endorsed by <Provider>.` Never sign an entry.
4. Add it with `uv run python scripts/catalog.py notice.json`, which validates it, refuses
   signed notices or a missing sentence, and keeps the catalog newest first. An entry with
   the same id is replaced.
5. Run `uv run tiltmeter scan --all` on a repository that uses the old API and on one that
   does not, check the places it lists, and run `uv run pytest -q`:
   `tests/notices/test_scan.py` checks that every entry is valid, unsigned and unique.

Retired models are found for you. `uv run python scripts/watch.py` reads the deprecation
pages of OpenAI, Anthropic and Groq, which publish them as Markdown tables, and lists the
values they retire that the catalog lacks: what shuts down ahead first, then what shut
down within the last year. A workflow runs it every Monday and keeps one issue, labelled
`catalog-watch`, up to date with the list. `--drafts DIR` writes an unsigned notice per
provider and date to start step 1 from; it holds only what the page's table says, so the
notes and the endpoints are still yours to write. To watch another page, add it to
`scripts/watch-sources.json`; a value on a page that is no retirement goes in that
source's `ignore`.
