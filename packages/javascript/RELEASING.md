# Releasing `@chipzen-ai/bot` to npm

The release workflow lives at
[`.github/workflows/release-javascript.yml`](../../.github/workflows/release-javascript.yml).
It uses **npm Trusted Publishing (OIDC)** so there is no long-lived
`NPM_TOKEN` secret to manage.

## One-time setup (before the first release)

Done once. Until both of these exist, the publish job will fail with
`OIDC trusted publisher not configured`.

### 1. Reserve the npm package name

The scope `@chipzen-ai` must be created on npmjs.com first; the package
name `@chipzen-ai/bot` will be minted on first publish.

- Sign in at https://www.npmjs.com/ as the `chipzen-ai` org owner.
- If the `chipzen-ai` org doesn't exist yet, create it (npmjs.com →
  Add Organization → name `chipzen-ai`).
- The org must be on the **Free** tier or higher; npm Trusted
  Publishing works on Free.
- You do not need to upload a placeholder version. Trusted Publishing
  can mint the package on first publish, as long as the scope exists.

### 2. Configure the Trusted Publisher

1. Open https://www.npmjs.com/package/@chipzen-ai/bot/access (the
   page exists once the package has at least one publish, but the
   trusted publisher form is also available at
   https://www.npmjs.com/settings/chipzen-ai/packages → select package
   → Trusted Publisher tab — even before the first publish for
   reservations).
2. **Add a new GitHub trusted publisher**:
   - **Organization**: `chipzen-ai`
   - **Repository**: `chipzen-sdk`
   - **Workflow filename**: `release-javascript.yml`
   - **Environment**: `npm` (must match the `environment.name` in the
     workflow's `publish` job)

npm will accept publishes from this exact `(repo, workflow, environment)`
triple via OIDC. No secret is stored on either side.

### 3. (Optional) Add reviewers / wait timers to the GitHub environment

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
4. **Approve** the publish (if reviewers were added in setup step 3).
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
