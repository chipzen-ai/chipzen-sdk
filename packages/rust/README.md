# Rust adapter — placeholder

The Rust adapter for the Chipzen SDK lands here in **Phase 3** of the
public-alpha rollout.

When it ships you'll find:

- `src/` — the `Bot` trait, `GameState` / `Action` types, the
  WebSocket client (tokio), `validate` CLI binary.
- `tests/` — unit + integration + protocol-conformance tests
  (`cargo test`).
- `Cargo.toml` — publishes as **`chipzen-bot`** on crates.io.
- Per-package `README.md` / `CHANGELOG.md`.
- `starters/rust/` — thin SDK-based starter plus an IP-protected
  multi-stage Dockerfile (compiled binary + distroless runtime).

Until then, the raw-WebSocket starter at
[`/starters/rust/`](../../starters/rust/) demonstrates the underlying
protocol if you'd like to start exploring.
