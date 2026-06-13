# Changelog

All notable changes to the `chipzen-bot` Rust SDK library will be
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **External-API remote-play path** — parity with the Python SDK 0.3.0
  ([#57](https://github.com/chipzen-ai/chipzen-sdk/issues/57)). A developer
  can now run a bot on their own machine with a long-lived `cz_extbot_`
  token and let the platform match + dispatch them, instead of only the
  containerized/single-match-URL path. New surface:
  - **`run_external_bot(factory, RunExternalOptions { .. })`** — connects to
    the lobby (`/ws/external/bot/{bot_id}`), then plays every `matched` it's
    dispatched (a single challenge, or every round of a tournament) over a
    per-match gateway WS (`/ws/external/match/{mid}/{pid}`) until the lobby
    closes, the bot is evicted, or `max_matches` complete. The match data
    plane reuses the existing session loop. A single persistent lobby
    connection keeps its heartbeat answered while matches are in flight, and
    each match runs in its own task on its own gateway socket. Includes the
    mid-match **gateway reconnect-resume** (a dropped gateway socket
    reconnects and resumes via the server's `reconnected` / `pending_request`
    frame), **hoisted match-task ownership** (a lobby reconnect doesn't kill
    in-flight matches), and **drain-then-cancel teardown** (no orphaned
    tasks). The token travels in the `Sec-WebSocket-Protocol` header
    (sentinel `chipzen-bot-token`, CZ#2932), never the query string.
  - **`connect_to_chipzen(bot_id, env, ..)`** — env→lobby-URL helper
    (`prod` / `staging` / `local`, honoring `$CHIPZEN_ENV`), returning a
    `ConnectionConfig`.
  - **`ChipzenConfig` + `load_chipzen_config`** — `chipzen.toml` discovery
    (cwd → `~/.chipzen/` → `/etc/chipzen/`) + `[external_api]`
    token/url/bot_id parsing, with explicit args winning over the file.
  - **`RetryPolicy`** — reconnect/backoff knobs (5 / 500ms / 30000ms / 2.0
    defaults) accepted by both `run_bot` and `run_external_bot`.
  - **`run_external_cli(factory, RunExternalArgs)`** — the Rust equivalent of
    the Python `chipzen run-external <bot.py>` CLI. A Rust bot is compiled
    into its own binary (no dynamic file loading), so this is a library
    helper you wire into your bot binary's `main`; the scaffolded starter
    ships a `run-external` mode that calls it with flags mirroring Python
    (`--env` / `--token` / `--bot-id` / `--max-matches` / `--no-safe-mode`).
- **Shared session primitives brought to parity:**
  - `run_bot` and `_run_session` now **return the `match_end` payload**
    (`Option<serde_json::Value>`) so the lobby loop can collect results and
    distinguish a clean end from a drop.
  - **`safe_mode`** knob on `RunBotOptions` / `RunExternalOptions` (default
    `true`). When `false`, a panic in `decide()` surfaces as the terminal
    `Error::BotDecision` (no reconnect-retry) rather than being folded to a
    safe action ([#52](https://github.com/chipzen-ai/chipzen-sdk/issues/52)).
  - **`client_version`** now defaults to the crate version instead of a
    hardcoded string ([#41](https://github.com/chipzen-ai/chipzen-sdk/issues/41)).
  - **`Bot::on_decision_latency(latency_ms)`** default-no-op hook, fired
    after each `turn_action` with the wall-clock `decide` time
    ([#46](https://github.com/chipzen-ai/chipzen-sdk/issues/46)).
  - A non-default **`User-Agent`** (`chipzen-sdk-rust/<version>`) is set on
    the WebSocket handshake, which also clears the platform's Cloudflare
    bot-fight rule ([#46](https://github.com/chipzen-ai/chipzen-sdk/issues/46)).
- Three new conformance scenarios in `run_conformance_checks`,
  bringing the total from 1 to 4. The previously-shipped scenario only
  covered a clean handshake + 1 hand + match_end; bots could pass it
  and still crash in production. The new scenarios are:
  - `multi_turn_request_id_echo` — drives 3 `turn_request`s across
    preflop/flop/turn and verifies the SDK echoes each `request_id`
    correctly (the previous harness only checked the first action).
  - `action_rejected_recovery` — verifies the SDK retries with a
    safe-fallback `check`/`fold` and the original `request_id` when
    the server sends `action_rejected` (a routine production code
    path that had no harness coverage).
  - `retry_storm_bounded` — verifies the SDK responds reactively to 3
    back-to-back `action_rejected` messages without hanging or
    entering an unbounded send loop.
  - Closes part of
    [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28) for the
    Rust SDK.
- Public `SCENARIO_NAMES` constant exporting the registered scenario
  names in execution order. Lets downstream tooling enumerate
  scenarios programmatically.

### Changed

- **BREAKING:** `run_bot` now returns
  `Result<Option<serde_json::Value>, Error>` (the `match_end` payload, or
  `None` if the socket closed without a clean `match_end`) instead of
  `Result<(), Error>`. Existing callers that ignored the success value
  (`run_bot(...).await?`) keep compiling; callers that bound `()` need
  `let _ = run_bot(...).await?;` or to consume the payload.
- **BREAKING:** `RunBotOptions` replaced the `max_retries: u32` field with a
  `retry_policy: RetryPolicy` (carrying the attempt cap **and** the
  backoff progression) plus `safe_mode: bool` and `user_agent:
  Option<String>` fields. `RunBotOptions::default()` is unchanged in spirit
  (5 attempts, exponential backoff, safe_mode on). The default client name
  is now `chipzen-sdk-rust`.
- `SessionContext` gained a `safe_mode` field; construct via
  `SessionContext::new(..)` (safe_mode on) to avoid naming it.
- `run_conformance_checks` now consumes the bot once but borrows it
  internally for each scenario. Callers don't need to re-construct
  the bot between scenarios; the lifetime contract from a single
  `B: Bot` argument is preserved.

### Documentation

- Documented a known limitation in `run_conformance_checks`: the
  Rust harness uses `tokio::time::timeout`, which cancels at await
  points. A bot whose `decide()` synchronously busy-loops or calls a
  long-blocking non-async function starves the tokio runtime task
  and prevents the timeout from firing. The Python SDK has a
  daemon-thread hard watchdog for this; the Rust equivalent
  (`tokio::task::spawn_blocking`) is more invasive and deferred.

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
