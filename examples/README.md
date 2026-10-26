# Examples

| File | What it is |
|---|---|
| [`tiltmeter.yaml`](tiltmeter.yaml) | A config with three public APIs that need no keys (GitHub, JSONPlaceholder and Open-Meteo), plus commented Stripe and GraphQL endpoints. Run it with `tiltmeter check -c examples/tiltmeter.yaml`. |
| [`github-action.yml`](github-action.yml) | A scheduled GitHub Actions job that keeps the baseline in the Actions cache and fails on drift. Copy it to `.github/workflows/` in the repository that holds your config. |
| [`notices-workflow.yml`](notices-workflow.yml) | A scheduled GitHub Actions job that applies the change notices of the providers you subscribe to, one pull request per notice that affects the repository. |
| [`provider-notices.yml`](provider-notices.yml) | For API providers: a draft notice on every pull request that changes `openapi.yaml`, and the signed feed published to GitHub Pages on merge. |
| [`api-change-notice.json`](api-change-notice.json) | An unsigned [API change notice](../docs/notice-spec.md), the format providers publish. Try it with `tiltmeter fix --notice examples/api-change-notice.json --allow-unsigned --dry-run`. |

The [getting started guide](../docs/getting-started.md) walks through a full drift cycle
against a fake API on your machine.
