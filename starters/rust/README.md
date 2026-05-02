# Rust starter — moved

The Rust starter has moved to **[`../../packages/rust/starters/rust/`](../../packages/rust/starters/rust/)**
now that the Rust SDK has shipped (`cargo add chipzen-bot`).

The new location:

- Uses the SDK's `Bot` trait — no hand-rolled WebSockets.
- Ships a multi-stage Dockerfile that **compiles your `lib.rs` to a
  single statically-linked release binary** for IP protection (your
  `.rs` source is not in the uploaded image). See
  [`packages/rust/IP-PROTECTION.md`](../../packages/rust/IP-PROTECTION.md).
- Includes a `tests/conformance.rs` that drives `MyBot` through the
  SDK's canned full-match exchange via `cargo test`.

The previous raw-WebSocket starter that lived here has been retired —
hand-rolling the wire protocol is no longer necessary. The protocol
spec at [`../../docs/protocol/`](../../docs/protocol/) remains
authoritative if you ever need to write a non-Rust client from
scratch.
