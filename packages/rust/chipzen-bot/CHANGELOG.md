# Changelog

All notable changes to the `chipzen-bot` Rust SDK library will be
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — Initial public release

First release of `chipzen-bot` to crates.io. Mirrors the Python
([`packages/python/CHANGELOG.md`](../../python/CHANGELOG.md)) and
JavaScript ([`packages/javascript/CHANGELOG.md`](../../javascript/CHANGELOG.md))
SDKs' shape.

### Scope

A protocol adapter (`Bot` trait + async WebSocket client + protocol-
conformance harness) so your bot doesn't hand-roll the wire protocol.
The CLI (`init` / `validate`) ships as the sibling
[`chipzen-sdk`](../chipzen-sdk/) binary crate; the IP-protected
starter recipe lives at [`packages/rust/starters/rust/`](../starters/rust/).

### Public API

- **`Bot`** trait (`Send + 'static`). Required `decide(&mut self,
  &GameState) -> Action`. Default-no-op lifecycle hooks
  (`on_match_start`, `on_round_start`, `on_phase_change`,
  `on_turn_result`, `on_round_result`, `on_match_end`) take
  `&serde_json::Value` for forward-compat.
- **`Action`** enum: `Fold` / `Check` / `Call` / `Raise(u64)` /
  `AllIn`. `action.to_wire() -> (&'static str, Value)` produces the
  two-layer `turn_action` payload.
- **`Card`** struct with `FromStr` + `Display`. **`GameState`** with
  all wire fields (camelCase Rust idiom: snake_case wire →
  snake_case Rust). **`parse_game_state(&Value) -> GameState`**.
- **`run_bot(url, bot, options) -> Result<(), Error>`** — async
  WebSocket client. Handshake (authenticate → server hello → client
  hello with `supported_versions=["1.0"]`), message loop until
  `match_end`, ping/pong, `action_rejected` retry, reconnect with
  bounded exponential backoff.
- **`run_conformance_checks(bot, options) -> Vec<ConformanceCheck>`**
  — drives `_run_session` through one canned full-match exchange
  against an in-process mock socket. Same severity model as the
  Python and JavaScript harnesses.
- **`MessageReader`** / **`MessageWriter`** traits — pull/push async
  socket abstractions. The session loop is generic over them so the
  conformance harness (and user tests) can mock the transport.
- **`Error`** — typed error enum with boxed large variants so
  `Result<T, Error>` stays small.

### Two-layer wire protocol

The client speaks the same Chipzen two-layer protocol the Python and
JavaScript SDKs do, defined in
[`docs/protocol/TRANSPORT-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/TRANSPORT-PROTOCOL.md)
and
[`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/POKER-GAME-STATE-PROTOCOL.md).

### Toolchain + packaging

- MSRV: **Rust 1.75** (pinned in workspace `[workspace.package]`).
- Async runtime: **tokio 1**, transport: **tokio-tungstenite 0.24**
  with `native-tls`.
- Released to crates.io via **Trusted Publishing** (OIDC) — see
  [`packages/rust/RELEASING.md`](../RELEASING.md). No long-lived
  `CARGO_REGISTRY_TOKEN` secret.

### License

Apache-2.0.

[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/rust-v0.2.0
