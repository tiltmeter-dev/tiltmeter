# Fixing code

A check tells you that `phone` became `phone_number`. Somewhere in your repository, code
still reads `phone`. `tiltmeter fix` closes that gap: it hands the drift to an agent that
finds the code reading that API, adapts it, shows you the diff and, once you agree, opens a
pull request.

- [Install](#install)
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

The agent is built on [LangChain Deep Agents](https://github.com/langchain-ai/deepagents),
which comes as an optional extra so the drift monitor stays small:

```bash
pip install "tiltmeter[fix]"          # or: uv tool install "tiltmeter[fix]"
export ANTHROPIC_API_KEY=sk-ant-...
```

Opening pull requests uses the [GitHub CLI](https://cli.github.com) (`gh`) and the
credentials it already has. Without it, Tiltmeter commits and pushes the branch and prints
the command to open the pull request yourself.

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
| `--repo PATH`, `-r` | The repository to fix. Defaults to the current folder. |
| `--config PATH`, `-c` | The `tiltmeter.yaml` whose history holds the check. |
| `--branch NAME` | Name of the new branch. Default: `tiltmeter/fix-<endpoint>-<check id>`. |
| `--base NAME` | Branch the pull request targets. Default: the branch you are on. |
| `--yes`, `-y` | Commit, push and open the pull request without asking. |
| `--no-pr` | Commit to a local branch only. Nothing is pushed. |
| `--dry-run` | Show the diff, then discard it. |
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
4. **Review.** Tiltmeter stages the edits and shows you the diff with the agent's report.
5. **Deliver.** On your approval it commits with your git identity, pushes the branch and
   opens a pull request with `gh`. Local git hooks are skipped, as for any bot commit; your
   CI runs on the pull request instead. Each step that fails leaves what was already done in
   place and tells you what to run by hand.
6. **Clean up.** The worktree is always removed. The branch is kept only if it holds a
   commit, and the check's notes record the pull request (`tiltmeter show <id>`).

## What the agent can and cannot do

The agent is a LangChain Deep Agent with file tools rooted at the worktree:

- **Can:** list, read, glob and grep files, and edit or write them.
- **Cannot:** run commands, reach the network, start subagents or delete files. Those tools
  are removed before the model sees the tool list, and refused if it calls them anyway.
- **Never sees:** files outside the worktree, untracked files (a fresh worktree has none, so
  your `.env` is not there), and anything matching `fix.ignore` or `.git`, which the file
  tools refuse to read or write.

Tiltmeter checks the result too: if the diff touches an ignored path anyway, it discards
everything and exits with code 1.

The agent works from types, never values. A balance of `1200` that became a string could
now be `"1200"` or `"12.00"`, and only the vendor knows. So for type changes the agent
updates the type definitions, avoids conversions that assume a format, and puts the
question in the "Left for a human" section of the pull request.

## What leaves your machine

Sent to Anthropic:

- the change list: paths, change kinds and types, plus Claude's earlier explanation if the
  check has one;
- the endpoint id, method and URL without its query string;
- the content of the files the agent decides to read, and the edits it makes.

Never sent: response values, header values, `${VAR}` values, untracked files and paths
matching `fix.ignore`. With `LANGSMITH_TRACING=true`, the run is traced to your LangSmith
project as `tiltmeter.fix`, with the same content.

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
  model: claude-sonnet-5-5     # the model the agent runs on
  effort: medium               # low, medium, high, xhigh, max; null turns thinking off
  max_model_calls: 40          # stop a runaway agent; raise it for large repositories
  branch_prefix: tiltmeter/fix
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
| 4 | Usage error: unknown check, not a git repository, a branch that already exists, no `ANTHROPIC_API_KEY`, or the `fix` extra is missing. |

## Limits

- One check per run. Fix older checks first if several are pending for the same API.
- The agent finds code by name. Code that builds field names dynamically, or reads the
  response through a generated client, may need a human.
- It does not run your tests. Your CI does, on the pull request.
- It is not available from the GitHub Action or the MCP server yet. Both are on the
  [roadmap](../README.md#roadmap).
