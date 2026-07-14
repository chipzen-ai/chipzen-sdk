# Releasing `chipzen-mcp` to PyPI

The release workflow lives at
[`.github/workflows/release-mcp.yml`](../../.github/workflows/release-mcp.yml).
It uses **PyPI Trusted Publishers (OIDC)** so there is no long-lived
`PYPI_TOKEN` secret to manage.

`chipzen-mcp` has **never been published** — the values below are the
one-time handoff a maintainer enters on PyPI to enable the first publish.

## PyPI "Add a pending publisher" form values — enter these EXACTLY

Go to https://pypi.org/manage/account/publishing/ → **Add a new pending
publisher** (GitHub), and enter:

| Field | Value |
|---|---|
| **PyPI Project Name** | `chipzen-mcp` |
| **Owner** | `chipzen-ai` |
| **Repository name** | `chipzen-sdk` |
| **Workflow name** | `release-mcp.yml` |
| **Environment name** | `pypi` |

For a TestPyPI dry run first, repeat the same form at
https://test.pypi.org/manage/account/publishing/ with **Environment
name** `testpypi` (everything else identical).

Because the project does not exist yet, use PyPI's **pending publisher**
form (not the per-project one) — Trusted Publishers will mint the
`chipzen-mcp` project on the first successful publish. No placeholder
upload and no API token are required.

## One-time setup (before the first release)

Done once per package + per index (PyPI and TestPyPI). Until the
pending publisher exists, the publish job will fail with `Trusted
Publisher not found`.

### 1. Reserve the PyPI project name

- Sign in at https://pypi.org/ as the maintainer account.
- `chipzen-mcp` is currently unregistered (PyPI 404), so you do not need
  to upload a placeholder — the pending Trusted Publisher configured
  above mints the project on first publish. If you would rather be
  explicit, upload an empty version manually with a one-time API token
  (`python -m build && twine upload dist/*`), then revoke the token.
- Repeat on https://test.pypi.org/ for TestPyPI if you want a dry run
  there first.

### 2. Configure the Trusted Publisher

Per index (PyPI + TestPyPI), add the pending publisher using the exact
values from the table at the top of this file:

1. Open https://pypi.org/manage/account/publishing/ (or the TestPyPI
   equivalent at https://test.pypi.org/manage/account/publishing/).
2. **Add a new pending publisher** of type **GitHub**:
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

1. **Bump the version** in
   [`packages/mcp/pyproject.toml`](pyproject.toml). Open a normal PR and
   merge.
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
   git tag mcp-v0.1.0   # match the pyproject version exactly
   git push origin mcp-v0.1.0
   ```
   Pushing the tag triggers the workflow, which builds + publishes. The
   `mcp-` prefix keeps this distinct from the chipzen-bot `python-v*`
   tags in the same repo.
4. **Approve** the publish (if reviewers were added in setup step 3).
5. **Verify**: `pip install chipzen-mcp==0.1.0` in a clean venv, run
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
     chipzen-mcp==0.1.0
   ```
5. Once happy, push the real tag (`mcp-v0.1.0`).

## Dry-run a build without publishing

For inspecting what the wheel + sdist actually contain before the
first real release.

1. Actions → "Release MCP" → Run workflow.
2. Check `dry_run` (publish job will be skipped).
3. Download the `chipzen-mcp-dist` artifact from the workflow run.
4. Inspect:
   ```bash
   tar tzvf chipzen_mcp-0.1.0.tar.gz | head -30
   unzip -l chipzen_mcp-0.1.0-py3-none-any.whl | head -30
   ```

## Yanking a bad release

If you publish a release with a critical bug:

1. Cut a fixed version (`0.1.1`) and publish it via the same flow.
2. Yank the bad version on PyPI:
   - https://pypi.org/manage/project/chipzen-mcp/releases/
   - Open the bad release → Yank → confirm.
3. Yanked releases stay installable for users who explicitly request
   the version (so reproducibility holds), but pip will skip them by
   default and emit a warning.

Do **not** delete a published release unless you absolutely must —
deletion blocks anyone reproducing an old build.
