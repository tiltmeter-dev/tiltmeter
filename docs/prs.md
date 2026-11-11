# Following the pull requests Tiltmeter opened

[`tiltmeter fix`](fix.md) opens pull requests, and a pull request that gets a review and
no answer stays open. With a few of them you remember which one a maintainer commented on.
With dozens, in other people's repositories, you look through every one for a new comment,
a red check or a branch its base left behind. `tiltmeter prs` does the looking: it finds
the pull requests Tiltmeter opened for you on GitHub, says what each open one waits for,
and puts the ones that wait for you first, each with the command that takes it up again:

```console
$ tiltmeter prs
! example-org/model-router#12  fix(llm): replace the models api.groq.com retired
    changes requested
    last word: octo-reviewer (member) requested changes 5 days ago: Please keep the old id as a fallback until we drop v1.
    tiltmeter fix --notice groq-retired-models-2026-08 --allow-unsigned --follow-up https://github.com/example-org/model-router/pull/12

! acme/support-bot#45  Replace the models api.groq.com retired
    waits for an answer
    last word: monalisa (owner) commented 2 days ago: Thanks! Could you also update the model in the README?
    tiltmeter fix --notice groq-retired-models-2026-08 --allow-unsigned --follow-up https://github.com/acme/support-bot/pull/45

! acme/weather-cli#7  Update OpenWeather calls to One Call API 3.0
    checks failed: lint, test (3.12)
    tiltmeter fix --notice openweathermap-one-call-2-5-closed --allow-unsigned --follow-up https://github.com/acme/weather-cli/pull/7

! example-org/jira-sync#3  Move issue search to /rest/api/3/search/jql
    conflicts with main
    update its branch by hand: `tiltmeter fix --follow-up` does not rebase or merge
    https://github.com/example-org/jira-sync/pull/3

✓ acme/chat-kit#88  Replace the models api.groq.com retired  ready: approved, checks passed · updated today
- acme/notes#5  Replace the models api.groq.com retired  waiting for the maintainers: review required · updated 12 days ago
- example-org/agent#31  fix: replace the models api.groq.com retired  waiting for the maintainers · updated 14 days ago

12 opened: 4 merged, 1 closed, 7 open (4 need you).
Run a command in a checkout of that repository that has the pull request's branch in a remote, such as your fork.
```

The repositories and people in this example are made up; the format is what the command
prints.

**It only reads.** Nothing is commented, pushed, merged, closed, approved or rerun on
GitHub, and no agent runs. It tells you where to look; answering is
[`tiltmeter fix --follow-up`](fix.md#answering-a-review), which you run.

- [What it needs](#what-it-needs)
- [How it works](#how-it-works)
- [What a pull request waits for](#what-a-pull-request-waits-for)
- [What it asks GitHub](#what-it-asks-github)
- [Reading the output](#reading-the-output)
- [Options and exit codes](#options-and-exit-codes)
- [What it cannot see](#what-it-cannot-see)

## What it needs

The [GitHub CLI](https://cli.github.com), `gh`, logged in with `gh auth login`. `gh`
already holds your login, so Tiltmeter never sees a token.

It needs no `tiltmeter.yaml`, no API key and no checkout: run it anywhere.

## How it works

### 1. Search

Every pull request `tiltmeter fix` opens ends with a footer that starts "This pull request
was generated automatically by [Tiltmeter](https://github.com/tiltmeter-dev/tiltmeter)".
`tiltmeter prs` asks GitHub's issue search for your pull requests whose description
spells its first words:

```text
is:pr author:@me in:body "This pull request was generated automatically by"
```

`--author LOGIN` puts another login in place of `@me`, `--state` adds `is:open`,
`is:merged` or `is:closed is:unmerged`, and each `--repo OWNER/NAME` adds a `repo:`.
Results come 100 to a page, the newest first, until `--limit` of them are read: 200 by
default.

The search only nominates. A pull request counts when a line of its description starts
with the whole footer, link included. One that quotes the footer in a sentence, such as
a pull request about the footer itself, is left out.

### 2. Read

The search already says which ones are merged or closed, so nothing more is asked about
them. The open ones are read with GraphQL, ten per request: where the reviews stand, the
latest 30 comments and 30 reviews with who wrote each and when, the date of the latest
commit and how its checks went, and whether the branch still merges into its base.

Whether a branch merges and how its checks went are slow for GitHub to work out: count on
half a second per open pull request. A request GitHub cuts off is asked again in two
halves. GitHub works out whether a branch merges only once someone asks, so the pull
requests it had no answer for are read once more, three seconds later.

### 3. Decide

Each open pull request gets one answer to "what does it wait for?", from the table below.
The ones that wait for you are listed first.

## What a pull request waits for

The surest sign first. The first that applies is the answer:

| It waits for | When | In `--json` |
|---|---|---|
| **changes requested** | The reviews stand at "changes requested", and no commit followed the latest review that asked. GitHub decides where the base branch requires reviews; elsewhere, the latest review of each person who can merge says as much. | `changes_requested` |
| **an answer** | A person other than the author, and not a bot, commented or reviewed after the author's last comment and the last commit. An approval asks nothing, so it does not count. | `answer` |
| **checks failed** | A check failed on the latest commit: a failure, an error, a timeout, or one that asks for an action. These are the checks `fix --follow-up` hands to the agent. | `checks_failed` |
| **behind or conflicting** | The branch conflicts with its base, or is behind a base that must be up to date before merging. | `behind_or_conflicting` |
| **ready** | Approved, and no check failed or is still running. It waits for a maintainer to merge it. | `ready` |
| **waiting for the maintainers** | None of the above: nobody reviewed it, or you answered last. | `maintainers` |

The first four **need you**. The last two do not.

Two of these rules are worth a second look:

- **A commit answers a request for changes; a comment does not.** GitHub keeps saying
  "changes requested" until the reviewer looks again, however long ago you pushed. So once
  a commit follows the review, the pull request is back with the maintainers, and the
  line says `changes were requested; a commit followed`. A reply without a commit leaves
  it with you.
- **A commit or a comment answers a comment.** After `fix --follow-up` pushes and comments,
  the pull request leaves your list until someone speaks again.

A pull request can need you twice: a failed check and a conflict, say. The surer reason
names it, and the branch is said next to it (`checks failed: lint; conflicts with main`),
because answering the first does not update the branch.

## What it asks GitHub

Everything goes through your own `gh` login, to github.com only, with no shell in between,
and counts against your rate limits:

- **The search**, with `gh api -X GET search/issues`: your login or `@me`, the first
  words of the footer, and the repositories you named. One request per 100 results, 10 at
  the most.
- **The names and numbers of the open pull requests it found**, in the GraphQL requests
  (`gh api graphql`) that read their reviews, comments, checks and merge state: one per
  ten pull requests.

Both only read. Nothing else is sent: nothing to Tiltmeter, to an API provider or to a
model, and the report stays on your machine.

## Reading the output

While it runs, a line per step goes to stderr (not with `--json`). Then:

- each pull request that **needs you**, in the order of the table and the latest update
  first: its repository, number and title; what it waits for; `last word`, the latest
  person other than the author to comment or review, with their tie to the repository as
  GitHub gives it (owner, member, collaborator, contributor), when, and the first 140
  characters of what they wrote, on one line; and the command to run;
- the other **open** ones in a line each: `✓` for one that is ready to merge, `-` for one
  that waits for the maintainers, with the last update;
- with `--state merged`, `closed` or `all`, the **merged** (`✓`) and **closed** (`×`) ones
  in a line each, with the date;
- the summary: `84 opened: 21 merged, 5 closed, 58 open (3 need you)`. With `--state`,
  only that state is counted.

### The command

For a pull request that waits for changes, an answer or green checks, the last line is
[`tiltmeter fix --follow-up`](fix.md#answering-a-review) as you would type it: the change
the fix was made from, then the pull request. The change is read from the pull request's
footer:

| The footer says | The command |
|---|---|
| ``with `tiltmeter fix 12` `` | `tiltmeter fix 12 --follow-up URL`. The check is in your history database. |
| ``from `ID` in its catalog of known API changes``, or ``from the unsigned notice `ID` `` with the id of a [catalog](scan.md#what-the-catalog-holds) entry | `tiltmeter fix --notice ID --allow-unsigned --follow-up URL` |
| an unsigned notice the catalog does not have | `tiltmeter fix --notice NOTICE --allow-unsigned --follow-up URL` |
| anything else: a provider's signed notice, a spec diff, a footer someone rewrote | `tiltmeter fix --notice NOTICE --follow-up URL` |

`NOTICE` stands for the notice the fix was made from, as
[`fix --notice`](notices.md#one-notice-by-hand) takes it: a file, a URL, `FEED#ID` or a
catalog id. A footer does not say where a notice was read from, so that part is yours.

Run the command in a checkout of that repository that has the pull request's branch in a
remote, such as your fork: `fix --follow-up` works on that branch.

A pull request that is **behind or conflicting** gets no command. `fix --follow-up` edits
files; it does not rebase or merge, so the branch is yours to update, and the line gives
the pull request's URL.

Titles, logins, check names and comments are other people's text. They are printed as
plain text on one line, never as markup, and comments are cut short. The id in a footer
reaches a command only when it has the shape of a check or notice id.

### JSON

`--json` prints one object instead, with every pull request found, whatever its state:
`filters` (the `author`, `state`, `repositories` and `limit` asked for); the `query` sent;
`found`, the results GitHub counted; `not_read`, the ones past `--limit`; `unrelated`, the
ones left out because they only quote the footer; `incomplete`, when GitHub's search timed
out; `counts` (`opened`, `merged`, `closed`, `open`, `need_you`, `unread`); and `pulls`.
Each pull request has `repository`, `number`, `title`, `url`, `author`, `state` (`open`,
`merged` or `closed`), `draft`, `created_at`, `updated_at` and `closed_at`, and for an open
one `waits_for` (a value of the table above), `needs_you`, `detail` (for one that needs
you, the branch it conflicts with or is behind, if any; for the others, where the reviews
and checks stand), `last_word` (`author`, `role`, `kind`, `stance`, `at`, `excerpt`),
`failed_checks`, `mergeable` (null while GitHub has not worked it out), `change` (the check
or notice id the footer names), `follow_up` (the command, or null when there is none to
run) and `error`. `error` at the top says why GitHub could not be asked.

## Options and exit codes

```console
$ tiltmeter prs [--author LOGIN] [--state open|merged|closed|all] [--repo OWNER/NAME]... [--limit N] [--json]
```

| Option | Description |
|---|---|
| `--author LOGIN` | Whose pull requests. The login `gh` holds by default. |
| `--state STATE` | `open`, `merged`, `closed` or `all`: look only for pull requests in that state, and list them. Without it, all are counted and only the open ones are listed. |
| `--repo OWNER/NAME` | Only pull requests to this repository. Repeat it for several. |
| `--limit N`, `-n` | Read at most this many pull requests, the newest first. 200 by default, 1,000 at most. |
| `--json` | Print JSON instead. |

| Exit code | Meaning |
|---|---|
| 0 | No open pull request needs you. |
| 1 | At least one does. |
| 3 | GitHub could not be asked: `gh` is missing or not logged in, the rate limit was reached, GitHub refused the search, or none of the open pull requests could be read. The message says what to do, such as `gh auth login` or waiting a minute. |
| 4 | Usage error: `--author` is not a login, or `--repo` is not `OWNER/NAME`. |

So a scheduled job can run `tiltmeter prs` and tell you to look when it exits with 1.

## What it cannot see

- **A fix a maintainer carried into their own pull request.** Some maintainers take your
  commit into a branch of theirs and close yours, or merge theirs and leave yours open.
  GitHub records no link between the two, so yours shows as `closed`, not `merged`, or
  stays open. When they say so in a comment, an open one waits for `an answer`.
- **What a comment means.** "Thanks, I will look next week" waits for an answer as much as
  a question does. Only an approval and a bot are known to ask nothing.
- **Bots that pass for people.** An account counts as a bot when GitHub marks it as one,
  when its name ends in `[bot]`, `-bot` or `-robot`, or when it is one of a few services
  that post as a user, such as `CLAassistant` and `codecov-commenter`. Another one counts
  as a person, and its comment waits for an answer.
- **Why a check failed.** A deploy preview that needs the repository's secrets, or a
  contributor agreement to sign, fails for a reason no edit fixes. It still counts as
  `checks failed`; the names of the failed checks are there to tell.
- **Checks that have not run.** Workflows that wait for a maintainer's approval, as for a
  first contribution, show as no checks at all: the pull request waits for the maintainers.
  And a status that stays pending until you act, such as a contributor agreement nobody
  signed, looks like a check that is still running.
- **A branch that is only behind.** Almost every branch is behind its base after a day.
  It counts only where the base branch requires branches to be up to date, which is when
  GitHub itself says "behind", or when it conflicts.
- **Mergeability GitHub has not worked out.** GitHub works it out in the background, the
  first time someone asks, and Tiltmeter asks a second time. When GitHub still does not
  know, a conflict is not seen: the output says how many are unknown, and a later run
  knows.
- **When a commit was pushed.** GitHub gives the date a commit was made. A commit made
  before a review and pushed after it does not count as an answer.
- **Long conversations.** The latest 30 comments, the latest 30 reviews and the first 50
  checks of each pull request are read. A request for changes older than that still
  counts, but a commit after it cannot be told from one before.
- **Pull requests without the footer.** One whose description was rewritten without it is
  not found, and neither is one opened before Tiltmeter 0.7.0, when the footer said
  "Opened by Tiltmeter". A fix made with `--no-pr` has no pull request to find.
- **More than 1,000.** GitHub serves the first 1,000 results of a search. Past that,
  narrow it with `--state` or `--repo`; the output says how many were not read.
- **The last minute.** The search index can lag. The state of every open pull request is
  read again, so one merged a moment ago is counted as merged, but one opened a moment
  ago may be missing.
- **GitHub only.** Pull requests on other hosts, and on GitHub Enterprise Server, are not
  searched.
