# CI and scheduling

Tiltmeter is a command that exits with a meaningful code, so anything that can run a
command on a schedule can run it: GitHub Actions, GitLab CI, cron or a long-running
service.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No drift. Info-only changes and new baselines count as no drift. |
| 1 | Warning-level drift. |
| 2 | Breaking drift. |
| 3 | A check could not run (network error, unexpected status, invalid JSON) and nothing broke. |
| 4 | Usage or configuration error. |

When several endpoints are checked at once, breaking drift wins over a failed check,
which wins over a warning.

## Keep the baseline between runs

Each change is [reported once](how-it-works.md#report-once): the check that sees it
reports it, then it becomes part of the baseline. That only works if `.tiltmeter/`
survives from one run to the next. Without it, every run records a fresh baseline and
never reports anything.

## GitHub Actions

The [Tiltmeter Action](https://github.com/tiltmeter-dev/tiltmeter-action) installs
Tiltmeter, keeps `.tiltmeter/` in the Actions cache, writes a table of changes to the job
summary and fails the step on drift.
[`examples/github-action.yml`](../examples/github-action.yml) checks every hour:

```yaml
name: API drift

on:
  schedule:
    - cron: "17 * * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  tiltmeter:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: tiltmeter-dev/tiltmeter-action@v1
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          STRIPE_API_KEY: ${{ secrets.STRIPE_API_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

- **Secrets.** Every `${VAR}` in your config needs a matching variable in `env`. Store the
  values as repository secrets.
- **Inputs.** `config`, `endpoints`, `version`, `fail-on`, `explain`, `notify`, `cache`
  and `summary`; outputs `exit-code`, `result` and `json`. The
  [action's README](https://github.com/tiltmeter-dev/tiltmeter-action#inputs) lists them
  all.
- **Pin the versions.** The action's `version` input pins Tiltmeter itself. For the action,
  `@v1` follows compatible releases; use a commit SHA if your policy asks for it.
- **Schedules are best effort.** GitHub runs `cron` in UTC and can delay busy hours by a
  few minutes; avoid the top of the hour.

### Fail only on breaking drift

```yaml
      - uses: tiltmeter-dev/tiltmeter-action@v1
        with:
          fail-on: breaking   # or "never" to only report
```

### Without the action

Run the command yourself and keep the baseline with `actions/cache`. Restoring with the
`tiltmeter-` prefix picks the newest saved baseline, and saving under the run id with
`if: always()` stores the new one even when drift fails the job:

```yaml
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
      - uses: actions/cache/restore@v6
        with:
          path: .tiltmeter
          key: tiltmeter-${{ github.run_id }}
          restore-keys: tiltmeter-
      - name: Check APIs
        run: uvx tiltmeter@0.2.0 check
        env:
          STRIPE_API_KEY: ${{ secrets.STRIPE_API_KEY }}
      - uses: actions/cache/save@v6
        if: always()
        with:
          path: .tiltmeter
          key: tiltmeter-${{ github.run_id }}
```

Exit code 1 fails a job like any other non-zero code. To fail only on breaking drift
without the action, keep the code and decide yourself:

```yaml
      - name: Check APIs
        run: |
          tiltmeter_status=0
          uvx tiltmeter@0.2.0 check || tiltmeter_status=$?
          case "$tiltmeter_status" in
            2|4) exit "$tiltmeter_status" ;;  # breaking drift or a broken config
            *)   exit 0 ;;                    # clean, warnings, or a check that could not run
          esac
```

## GitLab CI

```yaml
api-drift:
  image: ghcr.io/astral-sh/uv:python3.13-bookworm-slim
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
  cache:
    key: tiltmeter-state
    paths:
      - .tiltmeter/
    when: always
  script:
    - uvx tiltmeter@0.2.0 check
```

Create the schedule under **Build > Pipeline schedules**, and add the variables your config
references as masked CI/CD variables.

## Cron

```cron
*/15 * * * * cd /srv/apis && . ./secrets.env && /home/me/.local/bin/tiltmeter check --json >> checks.jsonl 2>> tiltmeter.log
```

Use the absolute path that `which tiltmeter` prints, because cron runs with a minimal
`PATH`. `secrets.env` exports the variables your config references.

## A long-running service

`tiltmeter watch` checks every `--interval` seconds, 300 by default, until it is stopped.
With systemd:

```ini
[Unit]
Description=Tiltmeter API drift monitor
After=network-online.target

[Service]
WorkingDirectory=/srv/apis
EnvironmentFile=/srv/apis/secrets.env
ExecStart=/home/me/.local/bin/tiltmeter watch --interval 300
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Fixes

`tiltmeter fix` is meant for a developer's machine for now. It needs a full git checkout,
`ANTHROPIC_API_KEY` and a `gh` that can push and open pull requests. It runs unattended
with `--yes`, so a job can call it, but the Tiltmeter Action does not do it yet. See the
[fix guide](fix.md).

## Machine-readable output

| Command | Output |
|---|---|
| `tiltmeter check --json` | A JSON array with one check result per endpoint. |
| `tiltmeter watch --json` | One check result per line ([JSON Lines](https://jsonlines.org)), for log pipelines. |
| `tiltmeter history --json` | Recent check results, newest first. |
| `tiltmeter show <id> --json` | One check result. |

A check result has the same fields as the `result` object of the
[webhook payload](configuration.md#webhook).
