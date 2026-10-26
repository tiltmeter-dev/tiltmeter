# Fixing code

A check tells you that `phone` became `phone_number`. Somewhere in your repository, code
still reads `phone`. `tiltmeter fix` closes that gap: it hands the drift to an agent that
finds the code reading that API, adapts it, shows you the diff and, once you agree, opens a
pull request. The change can also come from the API's provider, as a
[change notice](notices.md): `tiltmeter fix --notice <file or URL>`.

- [Install](#install)
- [Choosing the agent](#choosing-the-agent)
- [Run it](#run-it)
- [Flags](#flags)
- [What happens, step by step](#what-happens-step-by-step)
- [What the agent can and cannot do](#what-the-agent-can-and-cannot-do)
- [What leaves your machine](#what-leaves-your-machine)
- [Reviewing the pull request](#reviewing-the-pull-request)
- [Configuration](#configuration)
- [Exit codes](#exit-codes)
- [Limits](#limits)

## Install

Pick the agent that edits your code. The default one is built on
[LangChain Deep Agents](https://github.com/langchain-ai/deepagents) and comes as an optional
extra, so the drift monitor stays small:

```bash
pip install "tiltmeter[fix]"          # or: uv tool install "tiltmeter[fix]"
export ANTHROPIC_API_KEY=sk-ant-...
```

If you already use [Claude Code](https://code.claude.com) or
[Codex](https://developers.openai.com/codex/cli), skip both lines: the base install is
enough, and `--agent claude-code` or `--agent codex` runs the fix on your own login.

```bash
pip install tiltmeter
tiltmeter fix 2 --agent claude-code   # or set fix.agent: claude-code in tiltmeter.yaml
tiltmeter fix 2 --agent codex         # or set fix.agent: codex
```

Opening pull requests uses the [GitHub CLI](https://cli.github.com) (`gh`) and the
credentials it already has. Without it, Tiltmeter commits and pushes the branch and prints
the command to open the pull request yourself.

## Choosing the agent

| | `deepagents` (default) | `claude-code` | `codex` |
|---|---|---|---|
| Needs | `pip install "tiltmeter[fix]"` and `ANTHROPIC_API_KEY` | `claude` on your `PATH`, logged in, or `ANTHROPIC_API_KEY` | `codex` 0.156 or later on your `PATH`, logged in, or `CODEX_API_KEY` |
| Billed to | your Anthropic API key | your Claude Code plan, or the API key when it is set | your ChatGPT plan, or the API key when it is set |
| Model | `fix.model` | `fix.model` | Codex's default, or `fix.model` when it names an OpenAI model |
| Runs as | a LangChain Deep Agent inside Tiltmeter | `claude -p` in the worktree, with `--restricted` and only the Read, Edit, Write, Glob and Grep tools | `codex exec` in the worktree, with its shell inside Codex's sandbox and a permission profile that only opens the worktree |
| Traces | LangSmith, when `LANGSMITH_TRACING=true` | none; the session is not saved in `~/.claude` | none; the session is not saved (`--ephemeral`) |

All three get the same instructions and the same change list, work only in the worktree and
end with the same report. Whatever you pick, the review, the ignored-path check and the
pull request work the same way. OpenCode is tracked in
[#8](https://github.com/tiltmeter-dev/tiltmeter/issues/8).

## Run it

Find the check that saw the drift, then point `fix` at the repository whose code reads the
API:

```console
$ tiltmeter history billing_customer -n 3
check  endpoint          checked at            status  latency  result
#2     billing_customer  2026-10-21T14:50:02Z  200     10 ms    BREAKING · 3 changes
#1     billing_customer  2026-10-21T14:47:11Z  200     12 ms    baseline recorded (first check)

$ tiltmeter fix 2 --repo ~/code/billing-service
```

Tiltmeter prints the check, creates a branch, lets the agent work, and shows you the result:

```console
Working on a new branch, tiltmeter/fix-billing-customer-2, from main at 162dadd.
3 files changed  +5 -5
diff --git a/billing/client.py b/billing/client.py
@@ -15,9 +15,9 @@ def get_customer(customer_id: int) -> Customer:
 def contact_line(customer_id: int) -> str:
     customer = get_customer(customer_id)
-    return f"{customer['name']} <{customer['phone']}>"
+    return f"{customer['name']} <{customer['phone_number']}>"
...
╭─ agent ──────────────────────────────────────────────────────────────────────╮
│ Billing customer code now reads `phone_number` instead of `phone`, in the    │
│ client, the `Customer` TypedDict and the test fixture.                       │
│ Left for a human:                                                            │
│   - The format and units of the string `balance` are unknown ("1200" or      │
│ "12.00"). Confirm them with the billing provider.                            │
│ confidence medium                                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
Commit, push and open a pull request? [y/N]: y
✓ opened https://github.com/acme/billing-service/pull/41
```

Answer `n`, or press Enter, and everything is discarded: the branch and the worktree are
removed as if nothing happened.

## Flags

| Flag | Effect |
|---|---|
| `--notice SOURCE` | Fix what a provider's [change notice](notices.md) announces instead of a check: a file or an `https` URL. A notice from a URL must be signed by the provider's domain. |
| `--allow-unsigned` | With `--notice FILE`: accept a notice without a signature, such as a draft you wrote. |
| `--repo PATH`, `-r` | The repository to fix. Defaults to the current folder. |
| `--config PATH`, `-c` | The `tiltmeter.yaml` whose history holds the check. |
| `--branch NAME` | Name of the new branch. Default: `tiltmeter/fix-<endpoint>-<check id>`, or `tiltmeter/fix-<provider>-<notice id>-<digest>` for a notice. |
| `--base NAME` | Branch the pull request targets. Default: the branch you are on. |
| `--yes`, `-y` | Commit, push and open the pull request without asking. |
| `--no-pr` | Commit to a local branch only. Nothing is pushed. |
| `--dry-run` | Show the diff, then discard it. |
| `--agent NAME` | `deepagents`, `claude-code` or `codex`. Overrides `fix.agent` for this run. |
| `--json` | Print the outcome, including the diff and the agent's report, as JSON. With `--json`, nothing is committed unless you also pass `--yes`. |

## What happens, step by step

1. **Targets.** From the check, Tiltmeter keeps the changes code has to follow: removed
   and renamed fields, type changes, fields that became optional or nullable. New fields
   and status or latency changes are left out.
2. **A clean workspace.** It creates a new branch from your current commit and checks it
   out as a [git worktree](https://git-scm.com/docs/git-worktree) in a temporary folder.
   Your checkout, your index and your uncommitted changes are never touched, and the
   uncommitted changes are not part of the fix.
3. **The agent works.** It greps for the old field names and the API's address, reads the
   candidate files, and edits only the code that handles this API. It then reports what it
   changed, what it could not handle safely and how confident it is.
4. **Tests, if you configured them.** With `fix.test_command`, Tiltmeter runs your tests
   on the edits. When they fail, the agent gets the output and edits again, up to
   `fix.test_rounds` more times. The result goes into the pull request.
5. **Review.** Tiltmeter stages the edits and shows you the diff with the agent's report.
6. **Deliver.** On your approval it commits with your git identity, pushes the branch and
   opens a pull request with `gh`. Local git hooks are skipped, as for any bot commit; your
   CI runs on the pull request instead. Each step that fails leaves what was already done in
   place and tells you what to run by hand.
7. **Clean up.** The worktree is always removed. The branch is kept only if it holds a
   commit, and the check's notes record the pull request (`tiltmeter show <id>`).

## What the agent can and cannot do

The Deep Agent and Claude Code get file tools rooted at the worktree and nothing else:

- **Can:** list, read, glob and grep files, and edit or write them.
- **Cannot:** run commands, reach the network, start subagents or delete files. The Deep
  Agent never sees those tools, and refuses them if the model asks anyway. Claude Code runs
  with `--restricted` and an explicit tool list, without MCP servers, hooks or your own
  Claude Code settings.
- **Never sees:** files outside the worktree, untracked files (a fresh worktree has none, so
  your `.env` is not there), and anything matching `fix.ignore` or `.git`. Those paths are
  denied to the file tools: the Deep Agent's permissions refuse them, and Claude Code gets
  them as deny rules, which also hide them from searches.

Codex has no file tools of its own: it reads files by running commands such as `rg` and
`sed`, and edits them with `apply_patch`. So with `--agent codex` the shell stays, inside
Codex's own sandbox (bubblewrap, Landlock and seccomp on Linux; Seatbelt on macOS), and
Tiltmeter passes it a permission profile on the command line:

- **Reads:** denied everywhere except the worktree, the system paths commands need and
  Codex's own installation. Your home folder, other repositories and the main checkout's
  `.git` are not visible.
- **Writes:** only inside the worktree. `.git` and `fix.ignore` are denied there too.
- **Commands:** no network, and only a core set of environment variables (`PATH`, `HOME`
  and the like), so API keys in your environment do not reach the model.
- **Off:** web search, apps and connectors, the browser, computer use, plugins, image
  viewing and hooks, which would act outside the command sandbox. Your `config.toml` and
  rules are not loaded.

On Linux, Codex resolves the `fix.ignore` globs to the files that exist when the sandbox
starts. A file the agent creates under an ignored path during the run is caught by the
check below. Tested on Linux and WSL; macOS relies on the same profile through Seatbelt.

Tiltmeter checks the result too: if the diff touches an ignored path anyway, it discards
everything and exits with code 1.

The agent works from types, never values. A balance of `1200` that became a string could
now be `"1200"` or `"12.00"`, and only the vendor knows. So for type changes the agent
updates the type definitions, avoids conversions that assume a format, and puts the
question in the "Left for a human" section of the pull request.

## Running the tests

The agent cannot run anything, so it cannot see that `subscription.items` is a method in
stripe-python and not the field it meant. With `fix.test_command`, Tiltmeter runs the tests
for it:

```yaml
fix:
  test_command: python -m pytest -q   # or ["npm", "test"]
  test_rounds: 1                      # more edits allowed after a failure
```

The command runs code the agent just wrote, from a change that a third party described, so
it gets as little as possible:

- **No network.** On Linux it runs in a network namespace of its own (`unshare --net`), on
  macOS under `sandbox-exec`. Where neither works the tests are skipped, and the pull
  request says so; `test_network: true` lets them run with network access instead.
  Ubuntu 24.04 and later can keep unprivileged users from creating network namespaces
  (AppArmor's `apparmor_restrict_unprivileged_userns`); there the tests are skipped too.
- **No secrets.** Only `PATH`, the locale and the names in `test_env` are passed, with an
  empty folder as `HOME`, so tokens in your environment or your home folder's config files
  are not there. When Tiltmeter itself runs from an isolated environment (`uvx`, `pipx`,
  the GitHub Action), that environment is left out of `PATH`, so `python` is yours.
- **No leftovers.** It runs without a shell, in the worktree, and everything it writes
  there is thrown away afterwards: only the agent's edits are committed.
- **Not a full sandbox.** It runs as your user and can read your files. In CI that is the
  runner's; on your own machine, use it for repositories whose tests you would run anyway.

Install the dependencies before Tiltmeter runs, as your CI already does: the tests get no
network to download them. The worktree is a fresh checkout, so a `node_modules` folder or
virtual environment inside your checkout is not in it; dependencies installed in the
environment (`pip install`, a global Go module cache passed with `test_env`) are.

When the agent sees a failure, the output reaches it marked as data, with your earlier
instructions unchanged: it fixes the code or the fixtures that mirror the API, and must not
weaken or delete tests. The pull request has a "Tests" section with the command and whether
it passed, or the end of its output when it still fails, which also goes first in "Left for
a human".

## What leaves your machine

Sent to Anthropic, through your API key or your Claude Code login, or to OpenAI with
`--agent codex`:

- the change list: paths, change kinds and types, plus Claude's earlier explanation if the
  check has one, or the provider's summary and notes for a notice;
- the endpoint id, method and URL without its query string;
- the content of the files the agent decides to read, and the edits it makes.

Never sent: response values, header values, `${VAR}` values, untracked files and paths
matching `fix.ignore`. With the Deep Agent and `LANGSMITH_TRACING=true`, the run is traced
to your LangSmith project as `tiltmeter.fix`, with the same content. Claude Code runs with
`--no-session-persistence`, so the session is not written to `~/.claude`, and Codex with
`--ephemeral`.

## Reviewing the pull request

The pull request lists the API changes, the agent's summary, the files it touched and a
"Left for a human" section. Review it like any other change:

- Read the "Left for a human" items first. They are the questions the agent would not guess.
- Confirm the rename or type change with the vendor's changelog when there is one.
- Let your CI run: the agent does not run your tests.

## Configuration

The `fix` section of `tiltmeter.yaml` is optional:

```yaml
fix:
  agent: deepagents            # or claude-code, or codex
  model: claude-sonnet-5-5     # the model the agent runs on; Codex ignores Claude models
  effort: medium               # low, medium, high, xhigh, max; null turns thinking off
  max_model_calls: 40          # model calls, or Claude Code turns; raise for large repositories
  branch_prefix: tiltmeter/fix
  test_command: python -m pytest -q   # run the tests on the edits, and retry on a failure
  ignore:                      # never read or written by the agent
    - .env*
    - "*.pem"
    - "*.key"
    - secrets*
    - .tiltmeter
```

A pattern without a slash matches a file or folder name at any depth: `secrets*` covers
`secrets.yaml` and `config/secrets/db.txt`. A pattern with a slash matches from the
repository root, like `infra/prod`. `.git` is always blocked. Every option is in the
[configuration reference](configuration.md#fix).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The fix was committed (and pushed, and the pull request opened, when asked), discarded on purpose, or not needed. |
| 1 | The agent changed nothing but left questions for a human, or it touched an ignored path and everything was discarded. |
| 3 | The agent, the model provider or a git step failed. Anything already committed is kept, and the output says what is left to run. |
| 4 | Usage error: unknown check, a notice that is invalid, unsigned or does not verify, not a git repository, a branch that already exists, no `ANTHROPIC_API_KEY` or `fix` extra for the default agent, or Claude Code or Codex missing or logged out. |

## Limits

- One check per run. Fix older checks first if several are pending for the same API.
- The agent finds code by name. Code that builds field names dynamically, or reads the
  response through a generated client, may need a human.
- It does not run your tests. Your CI does, on the pull request.
- The GitHub Action runs it for provider notices (`mode: notices`), not yet for drift a
  scheduled check found, and the MCP server cannot run it yet. Both are on the
  [roadmap](../README.md#roadmap).
- Codex and OpenCode cannot drive it yet. Codex reads files through its shell, and OpenCode
  has no structured output on the command line. Both are tracked in
  [#8](https://github.com/tiltmeter-dev/tiltmeter/issues/8).
