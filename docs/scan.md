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

1 of 10 known API changes affects .: 2 lines in 2 files.
The catalog's entries are drafted by Tiltmeter from each provider's public documentation; no provider signed or endorsed them.
```

Nothing is recorded, no agent runs, and the bundled catalog is read from the installed
package, so nothing is fetched or sent anywhere.

- [What the catalog holds](#what-the-catalog-holds)
- [Reading the output](#reading-the-output)
- [Options and exit codes](#options-and-exit-codes)
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
| `hubspot-pipelines-v1-sunset` | api.hubapi.com | The Pipelines API v1 is sunset; v3 renames `pipelineId`, `stageId` and `active` and returns dates as strings. | 2026-12-04 |
| `anthropic-retired-models-2026-10-27` | api.anthropic.com | 12 retired Claude model versions, from Claude 2.0 to Claude Opus 4.1. | 2026-08-05 |
| `groq-retired-models-2026-08` | api.groq.com | 16 models Groq no longer serves, Llama, Mixtral, Gemma, DeepSeek and Qwen ones. | 2026-08-16 |
| `openai-retired-models-2026-10-23` | api.openai.com | 36 retired GPT-3.5, GPT-4, o1, o3-mini, o4-mini, davinci and DALL-E models. | 2026-10-23 |
| `spotify-2026-02-development-mode` | api.spotify.com | Web API endpoints and fields gone or moved for Development Mode apps. | 2026-03-09 |
| `huggingface-serverless-inference-api-retired` | huggingface.co | The serverless Inference API at `api-inference.huggingface.co`, replaced by `router.huggingface.co`. | 2025-07-01 |
| `stripe-2025-03-31-subscription-periods` | api.stripe.com | `current_period_start` and `current_period_end` moved from the Subscription to its items, from API version 2025-03-31. | 2025-03-31 |
| `jira-cloud-change-2046-search-jql` | atlassian.net | Jira Cloud's `/rest/api/3/search` and `/rest/api/2/search`, replaced by `/search/jql`. | 2025-05-01 |
| `openweathermap-one-call-2-5-closed` | api.openweathermap.org | One Call API 2.5, replaced by 3.0. | June 2024 |
| `slack-files-upload-sunset` | slack.com | `files.upload`, replaced by a three-step upload. | 2025-11-12 |

The scan considers every entry, however old: the point is changes that already shipped.
Some apply only to some customers. The Stripe change applies only to accounts and requests
on API version 2025-03-31 or later, Spotify's only to apps in Development Mode, and Groq's
August 2026 shutdowns only to free and developer-tier keys. The entry's notes say so, and
the scan cannot tell which case your code is in.

## Reading the output

For each change the folder uses:

- a line with the provider, the first sentence of the entry's summary and the date it took
  effect (or takes effect, for HubSpot's), or was published when it has no such date;
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
- the command that fixes it.

A summary line ends the output. Text from the catalog is printed as plain text, never as
markup. `--all` also lists every entry that does not affect the folder, with the reason,
and where tests or docs mention it anyway. `--json` prints the same as one JSON object:
`affected` lists each change with all its `matches` (up to 50), `count`, `files`, `tests`
and `fix_command`, and each match's `applies_to` says who its change affects, or is
`null` for every caller; `unaffected` comes with `--all`; `rejected` lists entries of a
`--catalog` that are not valid notices.

## Options and exit codes

```console
$ tiltmeter scan [PATH] [--catalog FILE_OR_URL] [--all] [--json]
```

| Option | Description |
|---|---|
| `PATH` | The folder to search, `.` by default. A git checkout is read as git sees it: tracked files and new ones it does not ignore. Any other folder is read file by file. |
| `--catalog FILE_OR_URL` | Search for the entries of another feed instead, such as your team's own: a file or an https URL, fetched like any subscribed feed. A signed entry's signature is checked against its provider's key. |
| `--all` | Also list the entries that do not affect `PATH`, and why. |
| `--json` | Print JSON instead. |

| Exit code | Meaning |
|---|---|
| 0 | Nothing in the catalog affects `PATH`. |
| 1 | `PATH` uses at least one change in the catalog. |
| 3 | The catalog could not be read. |
| 4 | `PATH` is not a folder, or another usage error. |

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
  files over 1 MB and symbolic links are not read as code.
- **Not in comments.** A retired value, a removed endpoint's path or a changed field counts
  only outside comment lines (`#`, `//`, `/* */`, `--`, `<!--`) and Python docstrings.
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
- **Calls through an SDK.** A removed endpoint is found where code spells its path, or
  calls it through an SDK in the usual `resource.verb(` form. A method the notice never
  names, such as Slack's `files_upload` in the Python SDK, is not.
- **Versions and plans.** The scan cannot tell which Stripe API version your account is
  on, whether a Spotify app has extended quota, or which Groq tier a key is on.
- **Generated or vendored code** in folders with unusual names, data files such as JSON
  corpora, and text inside strings (an error message that names a model) are read as code.
- **Large trees.** At most 20,000 files are read, and the unused-code check gives up above
  that.

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
   `applies_to`, on the entry or on that change. Name your source.
3. End the summary with the sentence `Drafted by Tiltmeter from <Provider>'s public
   documentation; not signed or endorsed by <Provider>.` Never sign an entry.
4. Add it with `uv run python scripts/catalog.py notice.json`, which validates it, refuses
   signed notices or a missing sentence, and keeps the catalog newest first. An entry with
   the same id is replaced.
5. Run `uv run tiltmeter scan --all` on a repository that uses the old API and on one that
   does not, check the places it lists, and run `uv run pytest -q`:
   `tests/notices/test_scan.py` checks that every entry is valid, unsigned and unique.
