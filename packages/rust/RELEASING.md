# Releasing `chipzen-bot` + `chipzen-sdk` to crates.io

The release workflow lives at
[`.github/workflows/release-rust.yml`](../../.github/workflows/release-rust.yml).
It uses **crates.io Trusted Publishing (OIDC)** so there is no
long-lived `CARGO_REGISTRY_TOKEN` secret to manage.

The workspace publishes two crates per release:

| Crate | What it is |
|---|---|
| [`chipzen-bot`](chipzen-bot/) | The SDK library — `Bot` trait, types, async WebSocket client, conformance harness. |
| [`chipzen-sdk`](chipzen-sdk/) | The `chipzen-sdk` CLI binary — `init` for scaffolding, `validate` for pre-upload checks. |

Both crates share the same `version` via `[workspace.package]`, so a
release ships the matched pair.

## One-time setup

Both crates are published (`0.3.0`), so this is the steady-state setup:
add a Trusted Publisher to each **existing** crate. Until both have one,
the publish job fails its authentication step.

### 1. Why the first publish did not use OIDC

Recorded because the previous version of this page assumed otherwise, and
the assumption cost a release.

crates.io can only attach a Trusted Publisher to a crate that **already
exists** — there is no pending-publisher form of the kind PyPI offers. So
the very first publish of a new crate name is necessarily bootstrapped
with a short-lived API token, which is what happened for `0.3.0`. Every
release after that is token-free.

If you ever add a *third* crate to this workspace, expect the same
two-step dance: token-bootstrap the first publish, configure the Trusted
Publisher, then drop back to OIDC.

### 2. Configure the Trusted Publisher

Per crate (do this twice — once for `chipzen-bot`, once for `chipzen-sdk`):

1. Open https://crates.io/me and sign in with the GitHub account that
   owns the crate.
2. Navigate to https://crates.io/crates/chipzen-bot/settings (or the
   `chipzen-sdk` equivalent) → **Trusted Publishing**.
3. **Add a new GitHub Trusted Publisher**:
   - **Repository owner**: `chipzen-ai`
   - **Repository name**: `chipzen-sdk`
   - **Workflow filename**: `release-rust.yml`
   - **Environment**: `crates-io` (must match the `environment.name`
     in the workflow's `publish` job)

crates.io will accept publishes from this exact `(repo, workflow,
environment)` triple via OIDC. No secret is stored on either side: the
workflow's `rust-lang/crates-io-auth-action` step swaps the job's OIDC
token for a short-lived crates.io token and revokes it again in its post
step.

### 3. Retire the bootstrap token

Once both Trusted Publishers above are configured — and **not before**,
or the next release fails to authenticate:

- Revoke the crates.io API token used for the `0.3.0` bootstrap
  (https://crates.io/settings/tokens).
- Delete the `CARGO_REGISTRY_TOKEN` repository secret. The workflow no
  longer reads it.

### 4. (Optional) Add reviewers / wait timers to the GitHub environment

In the chipzen-sdk repo on GitHub:

- Settings → Environments → New environment → name `crates-io`
- Add `Required reviewers` (at least one maintainer, e.g. yourself) —
  every publish then requires explicit approval before it runs.
- Add `Wait timer` if you want a forced cooling-off window between
  the trigger and the publish.

## Cutting a release

1. **Bump the version** in
   [`packages/rust/Cargo.toml`](Cargo.toml) (the `[workspace.package]`
   `version` field — applies to both crates) and add the release notes
   to [`CHANGELOG.md`](CHANGELOG.md). Open a normal PR and merge.
2. **Verify locally**:
   ```bash
   cd packages/rust
   cargo fmt --all --check
   cargo clippy --workspace --all-targets -- -D warnings
   cargo test --workspace
   cargo package -p chipzen-bot --allow-dirty
   cargo package -p chipzen-sdk --allow-dirty
   ls target/package/   # expect chipzen-bot-X.Y.Z.crate + chipzen-sdk-X.Y.Z.crate
   ```
3. **Tag the release** (after merging the version-bump PR to `main`):
   ```bash
   git checkout main
   git pull
   git tag rust-v0.3.0   # match the workspace.package version exactly
   git push origin rust-v0.3.0
   ```
   Pushing the tag triggers the workflow, which builds + publishes
   both crates.
4. **Approve** the publish (if reviewers were added in setup step 4).
5. **Verify**:
   ```bash
   cargo install chipzen-sdk --version 0.3.0
   chipzen-sdk init verify-bot
   chipzen-sdk validate verify-bot
   ```

## Dry-run a build without publishing

For inspecting what the `.crate` tarballs actually contain before the
first real release.

1. Actions → "Release Rust" → Run workflow.
2. Check `dry_run` (publish job will be skipped).
3. Inspect the build logs — `cargo package` runs in the build job and
   logs the included files.

You can also run `cargo package --list -p chipzen-bot` locally to see
exactly what would be uploaded.

## Yanking a bad release

If you publish a release with a critical bug:

1. Cut a fixed version (`0.2.1`) and publish it via the same flow.
2. Yank the bad version on crates.io:
   ```bash
   cargo yank --version 0.2.0 chipzen-bot
   cargo yank --version 0.2.0 chipzen-sdk
   ```
3. Yanked versions stay installable for builds with a pinned lockfile
   (so reproducibility holds), but cargo refuses them for new resolves
   and emits a warning.

Do **not** delete a published version unless absolutely required —
crates.io's deletion process is rare, slow, and breaks downstream
reproducibility.

## Notes on the two-crate workspace

`chipzen-sdk` (the CLI) **depends on** `chipzen-bot`
(`chipzen-bot = { version = "0.3", path = "../chipzen-bot" }`), which
fixes the publish order: `chipzen-bot` must reach the crates.io index
first or `cargo publish -p chipzen-sdk` cannot resolve it. The workflow
publishes them in that order for exactly this reason, and `cargo publish`
blocks until the crate it just uploaded is indexed.

The same dependency is why the build job only runs
`cargo package -p chipzen-bot`: packaging `chipzen-sdk` resolves its
dependency from the registry, which is not possible for a version that
has not been published yet. `chipzen-sdk` is verified at publish time by
`cargo publish` itself.

`chipzen-bot` can be republished on its own; `chipzen-sdk` cannot be
published ahead of a `chipzen-bot` version that satisfies its `"0.3"`
requirement.

The IP-protected starter at [`starters/rust/`](starters/rust/) uses
`chipzen-bot = "0.3"` from the registry and is intentionally NOT a
workspace member, so it builds cleanly anywhere the user copies it
once `chipzen-bot` is on crates.io.
