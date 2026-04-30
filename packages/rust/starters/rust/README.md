# Rust starter — IP-protected

A minimal Chipzen Rust bot project, ready to copy + modify.

The included Dockerfile ships your strategy as a **single statically-
linked release binary** (via `cargo build --release`), not as readable
`.rs` source. See [`../../IP-PROTECTION.md`](../../IP-PROTECTION.md)
for what this protects (and what it doesn't).

## Files

| File | Purpose |
|---|---|
| [`src/lib.rs`](src/lib.rs) | Your bot. The `MyBot` struct + `impl Bot for MyBot`. |
| [`src/main.rs`](src/main.rs) | Thin entry-point shim — reads env vars and calls `run_bot`. |
| [`Cargo.toml`](Cargo.toml) | Pins the SDK version. Add your own deps here. |
| [`tests/conformance.rs`](tests/conformance.rs) | Drives `MyBot` through the SDK's canned full-match exchange. Run with `cargo test`. |
| [`Dockerfile`](Dockerfile) | Multi-stage `cargo build --release`. The runtime image contains no `.rs` source for the bot. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight (no target/, cache, secrets, etc.). |

## Try it

Copy this directory somewhere outside the SDK repo, then:

```bash
# 1. Edit src/lib.rs — replace decide() with your strategy.
$EDITOR src/lib.rs

# 2. Run the conformance test.
cargo test

# 3. Validate before packaging.
chipzen-sdk validate .

# 4. Build the image.
docker build -t my-bot:v1 .

# 5. Export the upload tarball.
docker save my-bot:v1 | gzip > my-bot.tar.gz

# 6. Upload via the Chipzen platform UI.
```

Recommended max upload size: **300 MB compressed**. Hard cap: **500 MB**.
The starter image (no extra deps beyond the SDK) typically lands at
~30 MB compressed — most of that is the libssl3 + ca-certs runtime
dependency in the debian-slim base, plus the statically-linked bot
binary itself.

## Verifying the IP-protection worked

After step 4, you can confirm your `.rs` source isn't in the image:

```bash
docker run --rm --entrypoint sh my-bot:v1 -c "ls /bot/"
# Expect: bot         (the compiled binary — strategy linked inside)
# Should NOT see: src, Cargo.toml, target
```

If you see `src/` or `Cargo.toml` in the listing, the `cp ... && rm -rf src/`
step failed silently. Re-build with `--progress=plain` to see why.

## Running the binary directly (for local debugging)

The compiled binary is a single self-contained executable. You can
run it outside Docker if your host platform matches:

```bash
docker create --name extract my-bot:v1
docker cp extract:/bot/bot ./bot-binary
docker rm extract

# Run it (Linux x64 host, or via Docker on macOS/Windows).
CHIPZEN_WS_URL=ws://localhost:8001/ws/match/m_test/bot ./bot-binary
```

For cross-architecture builds (e.g., arm64 for Apple Silicon dev), use
Docker Buildx with `--platform=linux/amd64,linux/arm64` to produce a
multi-arch image. The Dockerfile's debian-slim base supports both.
