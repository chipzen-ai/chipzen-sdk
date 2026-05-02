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

## One-time setup (before the first release)

Done once per crate. Until both crates have a Trusted Publisher
configured, the publish job will fail with `403 Forbidden` from
crates.io.

### 1. Reserve the crate names

If `chipzen-bot` and `chipzen-sdk` are not already taken, you do not
need to upload a placeholder — Trusted Publishing can mint them on
first publish. If a name is taken, contact crates.io support; squatted
names can sometimes be transferred.

### 2. Configure the Trusted Publisher

Per crate (do this twice — once for `chipzen-bot`, once for `chipzen-sdk`):

1. Open https://crates.io/me and sign in with the GitHub account that
   should own the crate.
2. Navigate to https://crates.io/crates/chipzen-bot/settings (or the
   `chipzen-sdk` equivalent). For crates that don't exist yet, the
   Trusted Publisher form is available before the first publish — see
   https://crates.io/me/pending-publishers.
3. **Add a new GitHub Trusted Publisher**:
   - **Repository owner**: `chipzen-ai`
   - **Repository name**: `chipzen-sdk`
   - **Workflow filename**: `release-rust.yml`
   - **Environment**: `crates-io` (must match the `environment.name`
     in the workflow's `publish` job)

crates.io will accept publishes from this exact `(repo, workflow,
environment)` triple via OIDC. No secret is stored on either side.

### 3. (Optional) Add reviewers / wait timers to the GitHub environment

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
   git tag rust-v0.2.0   # match the workspace.package version exactly
   git push origin rust-v0.2.0
   ```
   Pushing the tag triggers the workflow, which builds + publishes
   both crates.
4. **Approve** the publish (if reviewers were added in setup step 3).
5. **Verify**:
   ```bash
   cargo install chipzen-sdk --version 0.2.0
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

`chipzen-sdk` (the CLI) does not depend on `chipzen-bot` — it's a
standalone crate that lives in the same workspace for shared
metadata. Either crate can be republished independently if needed
(skip the other in `cargo publish -p ...`).

The IP-protected starter at [`starters/rust/`](starters/rust/) uses
`chipzen-bot = "0.2"` from the registry and is intentionally NOT a
workspace member, so it builds cleanly anywhere the user copies it
once `chipzen-bot` is on crates.io.
