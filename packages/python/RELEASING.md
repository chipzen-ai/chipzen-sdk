# Releasing `chipzen-bot` to PyPI

The release workflow lives at
[`.github/workflows/release-python.yml`](../../.github/workflows/release-python.yml).
It uses **PyPI Trusted Publishers (OIDC)** so there is no long-lived
`PYPI_TOKEN` secret to manage.

## One-time setup (before the first release)

Done once per package + per index (PyPI and TestPyPI). Until both of
these exist, the publish job will fail with `Trusted Publisher not
found`.

### 1. Reserve the PyPI project name (PyPI does not allow account
   verification on a name no one owns yet)

- Sign in at https://pypi.org/ as the maintainer account.
- If `chipzen-bot` is not already taken, you do not need to upload a
  placeholder — Trusted Publishers can mint the project on first
  publish. If the name is taken or you want to be safe, upload an
  empty version manually: `python -m build && twine upload dist/*`
  with a one-time API token, then revoke the token.
- Repeat on https://test.pypi.org/ for TestPyPI.

### 2. Configure the Trusted Publisher

Per index (PyPI + TestPyPI):

1. Open https://pypi.org/manage/account/publishing/ (or the TestPyPI
   equivalent at https://test.pypi.org/manage/account/publishing/).
2. **Add a new publisher** of type **GitHub**:
   - **PyPI Project Name**: `chipzen-bot`
   - **Owner**: `chipzen-ai`
   - **Repository name**: `chipzen-sdk`
   - **Workflow name**: `release-python.yml`
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
to be gated similarly.

## Cutting a release

1. **Bump the version** in
   [`packages/python/pyproject.toml`](pyproject.toml) and add the
   release notes to [`CHANGELOG.md`](CHANGELOG.md). Open a normal PR
   and merge.
2. **Verify locally**:
   ```bash
   cd packages/python
   pip install --upgrade build
   python -m build
   ls dist/  # expect chipzen_bot-X.Y.Z.tar.gz + .whl
   ```
3. **Tag the release** (after merging the version-bump PR to `main`):
   ```bash
   git checkout main
   git pull
   git tag python-v0.2.0   # match the pyproject version exactly
   git push origin python-v0.2.0
   ```
   Pushing the tag triggers the workflow, which builds + publishes.
4. **Approve** the publish (if reviewers were added in setup step 3).
5. **Verify**: `pip install chipzen-bot==0.2.0` in a clean venv, run
   `chipzen-sdk validate <scaffolded bot>` to confirm.

## Cutting a TestPyPI release first

Useful before a major bump to verify the wheel installs cleanly
without polluting the real PyPI namespace.

1. Actions → "Release Python" → Run workflow.
2. Set `target_index` to `testpypi`, leave `dry_run` unchecked.
3. Approve the publish.
4. Install from TestPyPI:
   ```bash
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ \
     chipzen-bot==0.2.0
   ```
5. Once happy, push the real tag (`python-v0.2.0`).

## Dry-run a build without publishing

For inspecting what the wheel + sdist actually contain before the
first real release.

1. Actions → "Release Python" → Run workflow.
2. Check `dry_run` (publish job will be skipped).
3. Download the `chipzen-bot-dist` artifact from the workflow run.
4. Inspect:
   ```bash
   tar tzvf chipzen_bot-0.2.0.tar.gz | head -30
   unzip -l chipzen_bot-0.2.0-py3-none-any.whl | head -30
   ```

## Yanking a bad release

If you publish a release with a critical bug:

1. Cut a fixed version (`0.2.1`) and publish it via the same flow.
2. Yank the bad version on PyPI:
   - https://pypi.org/manage/project/chipzen-bot/releases/
   - Open the bad release → Yank → confirm.
3. Yanked releases stay installable for users who explicitly request
   the version (so reproducibility holds), but pip will skip them by
   default and emit a warning.

Do **not** delete a published release unless you absolutely must —
deletion blocks anyone reproducing an old build.
