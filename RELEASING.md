# Releasing Tiltmeter

Releases are cut by pushing a version tag. The **Release** workflow then tests the tagged
commit, builds the package, publishes it to PyPI with trusted publishing, attests the
build, and creates the GitHub release with the notes from CHANGELOG.md. Nobody uploads from
a laptop, and there is no PyPI token to leak.

## One-time setup

1. **Repository URLs.** `pyproject.toml` points at `github.com/tiltmeter/tiltmeter`, both
   in `[project.urls]` and in the README link rewrite under
   `[tool.hatch.metadata.hooks.fancy-pypi-readme]`. If the repository ever moves, update
   both. The release workflow refuses to publish while an `OWNER` placeholder is left.
2. **Code of conduct contact.** Replace `[INSERT CONTACT METHOD]` in CODE_OF_CONDUCT.md
   with an address that reaches the maintainers.
3. **GitHub environments.** Under *Settings > Environments*, create `pypi` and `testpypi`.
   On `pypi`, add yourself as a required reviewer and allow deployments only from tags
   matching `v*`, so every publish waits for an explicit approval.
4. **Trusted publishers.** On PyPI, go to *Your account > Publishing* and add a pending
   publisher: project `tiltmeter`, the repository owner and name, workflow `release.yml`,
   environment `pypi`. Do the same on [TestPyPI](https://test.pypi.org) with environment
   `testpypi`.
5. **Repository security.** Under *Settings > Security*, turn on private vulnerability
   reporting, which SECURITY.md relies on, plus Dependabot alerts. Protect the default
   branch and require the CI and CodeQL checks.
6. **Tag protection.** Add a tag ruleset so that only maintainers can create `v*` tags.

## Cutting a release

1. Make sure the default branch is green and has everything you want to ship.
2. Bump `__version__` in `src/tiltmeter/__init__.py`. Tiltmeter follows
   [semantic versioning](https://semver.org); until 1.0, a minor release may break things.
3. In CHANGELOG.md, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` with today's
   date, and add a new empty `## [Unreleased]` section above it.
4. Open a pull request named `Release X.Y.Z` with those two changes, and merge it once CI
   passes.
5. Tag the merge commit and push the tag:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Tiltmeter X.Y.Z"
   git push origin vX.Y.Z
   ```

6. Follow the **Release** workflow in the Actions tab. When it reaches the `pypi`
   environment, approve the deployment.
7. Check [the PyPI page](https://pypi.org/project/tiltmeter/), then run
   `uvx tiltmeter@X.Y.Z --version` on a clean machine.

## Rehearsing on TestPyPI

Tags with a pre-release suffix, such as `v0.2.0rc1`, are published to TestPyPI and become a
GitHub pre-release instead. The version in `__init__.py` must match (`0.2.0rc1`) and the
changelog needs a `## [0.2.0rc1] - YYYY-MM-DD` section. Try the result in a throwaway
environment:

```bash
python -m venv /tmp/tiltmeter-rc && . /tmp/tiltmeter-rc/bin/activate
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ tiltmeter==0.2.0rc1
tiltmeter --version
```

## When something goes wrong

- **The workflow failed before publishing.** Fix the problem on the default branch, delete
  the tag locally and on GitHub with `git tag -d vX.Y.Z` and
  `git push --delete origin vX.Y.Z`, then tag again.
- **A broken version reached PyPI.** PyPI never accepts the same version twice. Yank it on
  PyPI so installers skip it, fix the problem, and release the next patch version.
- **Only the GitHub release step failed.** Re-run that job from the workflow page. The
  package is already published and the built files are kept as a workflow artifact.

## What the workflow does

| Job | Purpose |
|---|---|
| `verify` | Checks that the tag matches `__version__`, that CHANGELOG.md has a dated section for it, and that no `OWNER` placeholder is left in `pyproject.toml`. |
| `test` | Runs the CI workflow on the tagged commit: lint, types, tests on every platform and a package build. |
| `build` | Builds the sdist and wheel with `uv build`, validates them with `twine check --strict`, and records build provenance attestations. |
| `publish-pypi` or `publish-testpypi` | Uploads through trusted publishing (OIDC). PyPI also stores PEP 740 attestations. |
| `github-release` | Creates the GitHub release with the changelog section as notes and the distributions attached. |

The helper behind `verify` and the release notes is `scripts/release.py`; it runs locally
too:

```bash
uv run python scripts/release.py check v0.1.0
uv run python scripts/release.py notes 0.1.0
```
