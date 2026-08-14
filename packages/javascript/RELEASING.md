# Releasing `@chipzen-ai/bot` to npm

The release workflow lives at
[`.github/workflows/release-javascript.yml`](../../.github/workflows/release-javascript.yml).
It uses **npm Trusted Publishing (OIDC)** so there is no long-lived
`NPM_TOKEN` secret to manage.

## One-time setup

`@chipzen-ai/bot` is published (`0.3.0`), so this is the steady-state
setup: add a Trusted Publisher to the **existing** package. Until it
exists, the publish job fails with `ENEEDAUTH`.

### 1. Why the first publish did not use OIDC

Recorded because the previous version of this page assumed otherwise, and
the assumption cost a release.

npm can only attach a Trusted Publisher to a package that **already
exists** — creating the scope is not enough. So the first publish of a new
package name is necessarily bootstrapped with a short-lived granular
token, which is what happened for `0.3.0`. Every release after that is
token-free.

### 2. Configure the Trusted Publisher

1. Open https://www.npmjs.com/package/@chipzen-ai/bot/access →
   **Trusted Publisher**.
2. **Add a new GitHub trusted publisher**:
   - **Organization**: `chipzen-ai`
   - **Repository**: `chipzen-sdk`
   - **Workflow filename**: `release-javascript.yml`
   - **Environment**: `npm` (must match the `environment.name` in the
     workflow's `publish` job)

npm will accept publishes from this exact `(repo, workflow, environment)`
triple via OIDC. No secret is stored on either side.

Trusted Publishing has a **client** floor as well as a registry-side
configuration: npm >= 11.5.1 on Node >= 22.14.0. The publish job therefore
runs on Node 22 and upgrades npm before publishing, even though the build
job stays on Node 20 (the runtime the package is tested against). An older
npm ignores OIDC entirely and fails looking for a token.

### 3. Retire the bootstrap token

Once the Trusted Publisher above is configured — and **not before**, or
the next release fails to authenticate:

- Revoke the npm Automation token used for the `0.3.0` bootstrap
  (https://www.npmjs.com/settings/~/tokens).
- Delete the `NPM_TOKEN` repository secret. The workflow no longer reads
  it.

### 4. (Optional) Add reviewers / wait timers to the GitHub environment

In the chipzen-sdk repo on GitHub:

- Settings → Environments → New environment → name `npm`
- Add `Required reviewers` (at least one maintainer, e.g. yourself)
  — every publish then requires explicit approval before it runs.
- Add `Wait timer` if you want a forced cooling-off window between the
  trigger and the publish.

## Cutting a release

1. **Bump the version** in
   [`packages/javascript/package.json`](package.json) and add the
   release notes to [`CHANGELOG.md`](CHANGELOG.md). Open a normal PR
   and merge.
2. **Verify locally**:
   ```bash
   cd packages/javascript
   pnpm install --frozen-lockfile
   pnpm build
   pnpm test
   npm pack --dry-run    # see what would be published
   ```
3. **Tag the release** (after merging the version-bump PR to `main`):
   ```bash
   git checkout main
   git pull
   git tag javascript-v0.2.0   # match the package.json version exactly
   git push origin javascript-v0.2.0
   ```
   Pushing the tag triggers the workflow, which builds + publishes.
4. **Approve** the publish (if reviewers were added in setup step 4).
5. **Verify**: `npm install @chipzen-ai/bot@0.2.0` in a clean dir, run
   `npx chipzen-sdk validate <scaffolded bot>` to confirm.

## Cutting a `next`-tag release first

npm doesn't have a TestPyPI equivalent, but you can publish under a
non-default dist-tag (e.g. `next`) without affecting `latest`. Useful
before a major bump.

1. Actions → "Release JavaScript" → Run workflow.
2. Set `dist_tag` to `next`, leave `dry_run` unchecked.
3. Approve the publish.
4. Install from the `next` tag:
   ```bash
   npm install @chipzen-ai/bot@next
   ```
5. Once happy, push the real tag (`javascript-v0.2.0`) which publishes
   to `latest`.

## Dry-run a build without publishing

For inspecting what the npm tarball actually contains before the first
real release.

1. Actions → "Release JavaScript" → Run workflow.
2. Check `dry_run` (publish job will be skipped).
3. Download the `chipzen-bot-npm` artifact from the workflow run.
4. Inspect:
   ```bash
   tar tzvf chipzen-ai-bot-0.2.0.tgz | head -30
   ```

## Provenance

The workflow publishes with `npm publish --provenance`, which:

- Signs the tarball with sigstore using the workflow's OIDC token.
- Records the `(repo, workflow, environment, commit-SHA)` provenance
  attestation on npm and at https://search.sigstore.dev/.
- Surfaces a green "Provenance" badge on the npm package page.

Consumers can verify provenance with `npm audit signatures` after
installing.

## Deprecating a bad release

npm doesn't support yanking the way PyPI does, but you can deprecate a
specific version, which makes `npm install` print a warning:

1. Cut a fixed version (`0.2.1`) and publish it via the same flow.
2. Deprecate the bad version:
   ```bash
   npm deprecate @chipzen-ai/bot@0.2.0 \
     "Critical bug — upgrade to 0.2.1 (https://github.com/chipzen-ai/chipzen-sdk/issues/NNN)"
   ```

The deprecated version remains installable for users who explicitly
request it, preserving reproducibility.

Do **not** unpublish a published version unless you absolutely must
(npm's 72-hour unpublish window). Deletion can break downstream
consumers depending on that exact version.
