# Releasing `chipzen-mcp` to PyPI

The release workflow lives at
[`.github/workflows/release-mcp.yml`](../../.github/workflows/release-mcp.yml).
It uses **PyPI Trusted Publishers (OIDC)** so there is no long-lived
`PYPI_TOKEN` secret to manage.

`chipzen-mcp` **is published** (0.1.0 through 0.1.5, all via Trusted
Publishing), so the one-time setup below is already done. It is kept as
the record of what was configured, and as the recipe if the publisher
ever has to be re-created or a TestPyPI entry added.

## PyPI Trusted Publisher values — what is configured

The project exists, so use the **per-project** publisher form at
https://pypi.org/manage/project/chipzen-mcp/settings/publishing/ rather
than the pending-publisher form. The values are:

| Field | Value |
|---|---|
| **PyPI Project Name** | `chipzen-mcp` |
| **Owner** | `chipzen-ai` |
| **Repository name** | `chipzen-sdk` |
| **Workflow name** | `release-mcp.yml` |
| **Environment name** | `pypi` |

TestPyPI is a separate index with its own publisher. If you want a dry
run there, add the same values at
https://test.pypi.org/manage/account/publishing/ with **Environment
name** `testpypi` (everything else identical). Because `chipzen-mcp`
does not exist on TestPyPI, that one uses the **pending publisher**
form, which mints the project on first publish with no placeholder
upload and no API token.

PyPI is one of the few registries offering a pending-publisher form, which
is why `chipzen-mcp` has been OIDC-published from its very first release.
npm and crates.io require the package to exist first, so the JavaScript
and Rust packages needed a token bootstrap — see their `RELEASING.md`.

## Setup reference (already done for PyPI)

Done once per package + per index. Until the publisher exists, the
publish job fails with `Trusted Publisher not found`.

### 1. The PyPI project

- Sign in at https://pypi.org/ as the maintainer account.
- `chipzen-mcp` was minted on the first successful publish by the pending
  Trusted Publisher, with no placeholder upload and no API token.
- Repeat on https://test.pypi.org/ for TestPyPI if you want a dry run
  there first.

### 2. Configure the Trusted Publisher

For **TestPyPI**, or if the PyPI publisher ever has to be re-created,
use the exact values from the table at the top of this file:

1. Open https://pypi.org/manage/project/chipzen-mcp/settings/publishing/
   (or, for TestPyPI, the pending-publisher form at
   https://test.pypi.org/manage/account/publishing/).
2. Add a **GitHub** publisher:
   - **PyPI Project Name**: `chipzen-mcp`
   - **Owner**: `chipzen-ai`
   - **Repository name**: `chipzen-sdk`
   - **Workflow name**: `release-mcp.yml`
   - **Environment name**: `pypi` (or `testpypi` for the TestPyPI
     entry — must match the `environment.name` in the workflow's
     `publish` job)

PyPI will accept publishes from this exact `(repo, workflow, environment)`
triple via OIDC. No secret is stored on either side.

### 3. (Optional) Add reviewers / wait timers to the GitHub environment

In the chipzen-sdk repo on GitHub:

- Settings → Environments → New environment → name `pypi`
- Add `Required reviewers` (at least one maintainer, e.g. yourself)
  — every publish then requires explicit approval before it runs.
- Add `Wait timer` if you want a forced cooling-off window between
  the trigger and the publish.

Repeat for the `testpypi` environment if you want TestPyPI publishes
to be gated similarly. (The `pypi` / `testpypi` environments are shared
with the chipzen-bot `release-python.yml` workflow — if you already
configured them for that package, no new environment is needed here.)

## Cutting a release

1. **Bump the version in BOTH files**, and add the release notes to
   [`CHANGELOG.md`](CHANGELOG.md). Open a normal PR and merge.
   - [`pyproject.toml`](pyproject.toml) `version` — what gets built and
     published to PyPI.
   - [`server.json`](server.json) — **two** `version` fields, the
     top-level one and the one inside `packages[0]`. The
     `registry-publish` job reads the top-level field to decide which PyPI
     release to wait for the ownership marker on, so a bump that misses
     this file publishes the wheel and then hangs the registry step on the
     previous version.

   All three must agree with the tag you push in step 3.
2. **Verify locally**:
   ```bash
   cd packages/mcp
   pip install --upgrade build
   python -m build
   ls dist/  # expect chipzen_mcp-X.Y.Z.tar.gz + .whl
   ```
3. **Tag the release** (after merging the version-bump PR to `main`):
   ```bash
   git checkout main
   git pull
   git tag mcp-v0.2.1   # match the pyproject / server.json version exactly
   git push origin mcp-v0.2.1
   ```
   Pushing the tag triggers the workflow, which builds + publishes. The
   `mcp-` prefix keeps this distinct from the chipzen-bot `python-v*`
   tags in the same repo.
4. **Approve** the publish (if reviewers were added in setup step 3).
5. **Verify**: `pip install chipzen-mcp==0.2.1` in a clean venv, run
   `chipzen-mcp --help` to confirm the console script resolves.

## Cutting a TestPyPI release first

Useful before a major bump to verify the wheel installs cleanly
without polluting the real PyPI namespace.

1. Actions → "Release MCP" → Run workflow.
2. Set `target_index` to `testpypi`, leave `dry_run` unchecked.
3. Approve the publish.
4. Install from TestPyPI (the `--extra-index-url` lets pip resolve the
   `chipzen-bot` runtime dependency from real PyPI):
   ```bash
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ \
     chipzen-mcp==0.2.1
   ```
5. Once happy, push the real tag (`mcp-v0.2.1`).

## Dry-run a build without publishing

For inspecting what the wheel + sdist actually contain before the
first real release.

1. Actions → "Release MCP" → Run workflow.
2. Check `dry_run` (publish job will be skipped).
3. Download the `chipzen-mcp-dist` artifact from the workflow run.
4. Inspect:
   ```bash
   tar tzvf chipzen_mcp-0.2.1.tar.gz | head -30
   unzip -l chipzen_mcp-0.2.1-py3-none-any.whl | head -30
   ```

## Yanking a bad release

If you publish a release with a critical bug:

1. Cut a fixed version (`0.2.2`) and publish it via the same flow.
2. Yank the bad version on PyPI:
   - https://pypi.org/manage/project/chipzen-mcp/releases/
   - Open the bad release → Yank → confirm.
3. Yanked releases stay installable for users who explicitly request
   the version (so reproducibility holds), but pip will skip them by
   default and emit a warning.

Do **not** delete a published release unless you absolutely must —
deletion blocks anyone reproducing an old build.
