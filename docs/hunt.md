# Hunting for repositories that still use a change

[`tiltmeter scan`](scan.md) answers "does my code use an API change that already shipped?".
`tiltmeter hunt` turns the question around, for an API provider or for anyone about to
propose a fix upstream: which public repositories still use what this notice changes, and
where? It asks GitHub's code search for what the notice changes, clones the most starred
repositories the search names and checks each clone with the search `scan` uses:

```console
$ tiltmeter hunt groq-retired-models-2026-08 --limit 3
api.groq.com  Groq no longer serves 16 models it retired, the latest on August 16, 2026.  took effect 2026-08-16, 2 months ago
    groq-retired-models-2026-08 · unsigned: drafted by Tiltmeter from the provider's public documentation
    only for some callers (llama-3.3-70b-versatile): Free and developer-tier keys. Not Groq enterprise customers with a committed-spend contract.

✗ acme/support-bot  4,210 stars · pushed 2026-11-02 · main
    https://github.com/acme/support-bot
    app/llm.py:4  llama-3.3-70b-versatile: retired; use openai/gpt-oss-120b
        MODELS = {"fast": "llama-3.1-8b-instant", "smart": "llama-3.3-70b-versatile"}
    app/llm.py:9  llama3-8b-8192: retired; use openai/gpt-oss-20b
        model="llama3-8b-8192",

✗ example-org/model-router  1,380 stars · pushed 2026-11-02 · main
    https://github.com/example-org/model-router
    src/router.js:2  llama-3.3-70b-versatile: retired; use openai/gpt-oss-120b
        groq: { fast: 'llama-3.1-8b-instant', smart: 'llama-3.3-70b-versatile' },

- example-org/chat-kit  960 stars  not affected: no tracked file mentions api.groq.com, /openai/v1/chat/completions, llama-3.3-70b-versatile, llama-3.1-8b-instant or 14 more
    used only in tests, which do not count: tests/test_llm.py:1
    mentioned in docs, which do not count: README.md:1

2 of 3 repositories checked still use what groq-retired-models-2026-08 changes.
GitHub's code search named 27 repositories for 16 queries; left out: 3 archived, 6 forks, 14 not pushed in 365 days, 1 too large to clone.
skipped as too large to clone: acme/monorepo (2,400 MB)
The code search is a heuristic over default branches: a repository it did not name can still use the change.
```

The repositories in this example are made up; the format is what the command prints.

**It only reads.** Nothing is forked, pushed, starred or commented on, no issue or pull
request is opened, no agent runs, and nothing is written outside the folder the clones go
to. What to do about a repository it finds is up to you.

- [What it needs](#what-it-needs)
- [How it works](#how-it-works)
- [What it sends to GitHub](#what-it-sends-to-github)
- [Reading the output](#reading-the-output)
- [Options and exit codes](#options-and-exit-codes)
- [Limits](#limits)

## What it needs

- The [GitHub CLI](https://cli.github.com), `gh`, logged in with `gh auth login`. GitHub's
  code search answers no anonymous request, and `gh` already holds your login, so
  Tiltmeter never sees a token.
- `git`, to clone.
- A notice. `NOTICE` is read as [`tiltmeter fix --notice`](notices.md#one-notice-by-hand)
  reads it: a notice file or https URL, `FEED#ID` for one entry of a feed, or the id of an
  entry in the [catalog](scan.md#what-the-catalog-holds) `scan` reads. An unsigned file or
  catalog entry is fine here, with no `--allow-unsigned`, because nothing is changed in
  anyone's code; the second line of the output says whether the notice is signed. From a
  URL, only signed notices are read, as everywhere.

It needs no `tiltmeter.yaml` and no API key.

## How it works

### 1. Search

The queries come from the notice, the surest sign first:

| The notice | The query | For example |
|---|---|---|
| retires a value (`value_retired`) | The value, quoted. | `"llama3-8b-8192"` |
| removes an endpoint (`endpoint_removed`) | The parts of its path that code spells: what lies between its parameters, without a leading version such as `/v1`, which often lives in a base URL. Unless one part has four segments or more, and so is no other API's, the provider's domain goes with them. For an endpoint that moved to another host, the old host does. | `"/rest/api/3/search"`<br>`"/playlists" "/tracks" "api.spotify.com"`<br>`"/models" "api-inference.huggingface.co"` |
| names the SDK methods that reach a removed endpoint (`sdk_calls`) | Each method next to the package that has it. | `"files_upload" "slack-sdk"`<br>`"files.upload" "@slack/web-api"` |
| changes a field (removed or renamed, another type, now nullable or optional, new values) | The old field name next to the provider's domain, and once more next to each SDK the notice names whose imports spell the package: PyPI, npm, Go, RubyGems and Cargo ones. | `"current_period_end" "api.stripe.com"`<br>`"current_period_end" "stripe"` |

The fields of an endpoint that is also removed are not searched: its path says more. A
query longer than the 256 characters GitHub accepts is not built.

Each query goes to GitHub's code search with
`gh api -X GET search/code -f q=... -f per_page=100`, with no shell in between. GitHub
allows about 10 code searches a minute, so a hunt sends one request every 7 seconds and
at most 30 in all: three and a half minutes at the longest. Every query gets its first
page of 100 results before any gets a second, and none gets more than five. When a notice
builds more queries than that, such as OpenAI's 36 retired models, the last ones are not
run and the output says how many.

### 2. Rank

One GraphQL request per 50 repositories (`gh api graphql`) brings each one's stars, last
push, size, default branch and whether it is archived, a fork or private. These are left
out, and the summary counts each reason:

- **private** repositories. The code search also names the private repositories your
  login can read; they are never cloned and never listed;
- **archived** repositories, and **forks** unless `--include-forks`;
- repositories with fewer stars than `--min-stars`;
- repositories with no push in the last `--pushed-within` days, 365 by default;
- repositories **larger than 500 MB**, which are listed as skipped, most stars first;
- repositories GitHub no longer shows.

The rest is sorted by stars, and the first `--limit`, 20 by default, go on.

### 3. Verify

Each of those is cloned with `git clone --depth 1 --no-tags --single-branch`, with every
git hook off, Git LFS files left as pointers, no credential prompt and five minutes at
most. The clone is then searched for this one notice exactly as `tiltmeter scan` searches
a folder: code decides, tests and docs are listed apart and never count on their own,
comments do not count, and a file nothing else imports is marked `possibly unused`.
[What counts as a use](scan.md#what-counts-as-a-use) has the rules.

A clone that fails or times out is that repository's error: it is reported, and the
others go on. Clones go to a temporary folder and each is removed as soon as it has been
searched, so a hunt holds one clone at a time. With `--keep DIR` they stay, as
`DIR/OWNER/REPO`; a folder that is already there is not overwritten, and that repository
is reported as an error.

## What it sends to GitHub

Everything goes through your own `gh` login and `git`, to github.com only, and counts
against your rate limits:

- **The code-search queries**: the values, paths and field names the notice changes, and
  its provider's domain and SDK names. So the terms of a notice you have not published
  yet reach GitHub's search.
- **The names of the repositories the search found**, in the GraphQL requests that ask
  for their stars and dates.
- **`git clone`** of the public repositories it checks, over https.

Nothing else: no file contents, no results, and nothing to Tiltmeter or to the provider.
The report stays on your machine, and the repositories are not told.

## Reading the output

While the hunt runs, a line per step goes to stderr (not with `--json`). Then:

- two lines on the notice: the provider, the first sentence of its summary and when the
  change took or takes effect, with how far away that is, then its id and whether it is
  signed by the provider, unsigned, or an unsigned draft from Tiltmeter's catalog;
- `only for some callers`, when a matched change affects only some of them, such as one
  plan. Tiltmeter can't tell which of them a repository serves;
- each **affected** repository, most stars first: its name, stars, last push and default
  branch, its URL, and up to three places as `file:line` with what changes for them and
  the line itself, the surest first. The rest is counted, and `also in tests` lists the
  places in test files. When the notice names SDK releases, the release the repository
  installs or asks for comes next, with whether it is older than the first one that
  follows the change, and so do the dated API versions its code pins
  ([details](scan.md#the-release-and-api-version-you-are-on));
- each repository that is **not affected**, in one line with the reason, and where its
  tests or docs mention the change anyway. These are the false alarms of the code search;
- each repository that **could not be checked**, with git's reason;
- the summary: how many of the repositories that could be checked still use the change,
  then how many the code search named and why the others were left out.

Repository names, file lines and the notice's text are printed as plain text, never as
markup.

`--json` prints one object instead: `notice_id`, `provider`, `headline`, `published_at`,
`effective_at` and `days_left` (the days until the change takes effect, zero or less once
it has), `source`, `signed` and `drafted`; the `filters` used; `queries`, each with its `kind`, `term`, the
`q` sent, the `total` GitHub counted, the `pages` read and the `repositories` they named;
`queries_not_run`; `counts` (`found`, `dropped` by reason, `eligible`, `checked`,
`affected`, `unaffected`, `failed`); `skipped`, the repositories too large to clone; and
`repositories`, the ones checked, each with `full_name`, `url`, `stars`, `pushed_at`,
`default_branch`, `fork`, `affected`, `reason`, `error`, its `matches` (up to 50, as in
[`scan --json`](scan.md#reading-the-output)), `count`, `files`, `tests`, `docs`,
`sdk_pins`, `api_versions`, `truncated` and, with `--keep`, where its `clone` is. `error` at the top says why the
search could not run.

## Options and exit codes

```console
$ tiltmeter hunt NOTICE [--limit N] [--min-stars N] [--pushed-within DAYS] [--include-forks] [--keep DIR] [--json]
```

| Option | Description |
|---|---|
| `NOTICE` | The change to look for: a notice file or https URL, `FEED#ID`, or the id of a catalog entry. |
| `--limit N`, `-n` | Clone and check at most this many repositories, the most starred first. 20 by default, 200 at most. |
| `--min-stars N` | Leave out repositories with fewer stars. 0 by default. |
| `--pushed-within DAYS` | Leave out repositories with no push in this many days. 365 by default. |
| `--include-forks` | Check forks too. |
| `--keep DIR` | Keep the clones in this folder, as `DIR/OWNER/REPO`, instead of a temporary folder that is removed. |
| `--json` | Print JSON instead. |

| Exit code | Meaning |
|---|---|
| 0 | No repository that was checked uses what the notice changes. |
| 1 | At least one does. |
| 3 | The search could not run: `gh` is missing or not logged in, GitHub's rate limit was reached, or every clone failed. The message says what to do, such as `gh auth login` or waiting a minute. |
| 4 | The notice cannot be read, `--keep` is not a folder, or another usage error. |

## Limits

A hunt finds repositories; it does not count them. Read "7 of 20" as "at least 7", never
as a share of an API's users.

- **The code search is a heuristic.** It matches words, so its hits sit in docs, tests,
  comments and other vendors' code as often as in a call; that is why every repository is
  cloned and checked. The other way round, a repository whose code never spells the value,
  path or field is never named: the model id may come from an environment variable, the
  path from an SDK.
- **Default branches and small files only.** GitHub's code search indexes the default
  branch of a repository, and files under 384 KB. Code on other branches is not found,
  and the clone is of the default branch too.
- **1,000 results per query, and a hunt reads fewer.** GitHub serves at most the first
  1,000 results of a query; a hunt reads up to 500 per query and 3,000 in all, to stay
  within the rate limit. For a common value, such as a model id with tens of thousands of
  results, that is a sample in GitHub's own order, which is not by stars: the ranking
  picks the most starred of the repositories found, not of the ones that exist.
- **The rate limit is shared.** The 10 code searches a minute are your account's. If
  something else used them in the last minute, GitHub refuses, and the hunt stops with
  exit code 3: wait a minute and run it again. A hunt does not retry on its own.
- **Stars and pushes are a proxy.** A repository with many stars may be a tutorial, and
  one with none may run in production.
- **Size is GitHub's figure**, which counts the whole history. A repository over 500 MB
  is skipped even when its latest commit alone would be small.
- **The check has `scan`'s blind spots.** It reads text and does not run code:
  [the list](scan.md#blind-spots) applies to every clone. A change that applies to some
  callers only, such as one plan or API version, is reported for every repository that
  spells it.
- **GitHub only.** Repositories hosted elsewhere are not searched.
