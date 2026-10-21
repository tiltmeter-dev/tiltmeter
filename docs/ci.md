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

[`examples/github-action.yml`](../examples/github-action.yml) checks every hour, keeps
`.tiltmeter/` in the Actions cache and fails the job on drift:

```yaml
name: API drift

on:
  schedule:
    - cron: "17 * * * *"
  workflow_dispatch:

jobs:
  tiltmeter:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
      - uses: actions/cache/restore@v6
        with:
          path: .tiltmeter
          key: tiltmeter-${{ github.run_id }}
          restore-keys: tiltmeter-
      - name: Check APIs
        run: uvx tiltmeter@0.1.0 check
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          STRIPE_API_KEY: ${{ secrets.STRIPE_API_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
      - uses: actions/cache/save@v6
        if: always()
        with:
          path: .tiltmeter
          key: tiltmeter-${{ github.run_id }}
```

- **The cache dance.** Restoring with the `tiltmeter-` prefix picks the newest saved
  baseline, and saving under the run id with `if: always()` stores the new one even when
  drift fails the job.
- **Secrets.** Every `${VAR}` in your config needs a matching variable in `env`. Store the
  values as repository secrets.
- **Pin the version.** `tiltmeter@0.1.0` keeps a new release from changing your CI
  without a review.
- **Schedules are best effort.** GitHub runs `cron` in UTC and can delay busy hours by a
  few minutes; avoid the top of the hour.
- **Pin the actions too, if your policy asks for it.** Replace each `@v7`-style tag with
  the commit SHA it points to, as this repository's own workflows do.

### Fail only on breaking drift

Exit code 1 fails a job like any other non-zero code. To report warnings and failed checks
without failing the job, keep the code and decide yourself:

```yaml
      - name: Check APIs
        run: |
          tiltmeter_status=0
          uvx tiltmeter@0.1.0 check || tiltmeter_status=$?
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
    - uvx tiltmeter@0.1.0 check
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

## Machine-readable output

| Command | Output |
|---|---|
| `tiltmeter check --json` | A JSON array with one check result per endpoint. |
| `tiltmeter watch --json` | One check result per line ([JSON Lines](https://jsonlines.org)), for log pipelines. |
| `tiltmeter history --json` | Recent check results, newest first. |
| `tiltmeter show <id> --json` | One check result. |

A check result has the same fields as the `result` object of the
[webhook payload](configuration.md#webhook).
