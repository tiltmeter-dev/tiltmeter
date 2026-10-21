# Examples

| File | What it is |
|---|---|
| [`tiltmeter.yaml`](tiltmeter.yaml) | A config with three public APIs that need no keys (GitHub, JSONPlaceholder and Open-Meteo), plus commented Stripe and GraphQL endpoints. Run it with `tiltmeter check -c examples/tiltmeter.yaml`. |
| [`github-action.yml`](github-action.yml) | A scheduled GitHub Actions job that keeps the baseline in the Actions cache and fails on drift. Copy it to `.github/workflows/` in the repository that holds your config. |

The [getting started guide](../docs/getting-started.md) walks through a full drift cycle
against a fake API on your machine.
