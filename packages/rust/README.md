# chipzen-bot — Rust SDK for the Chipzen poker platform

Build, test, and deploy poker bots in Rust for the
[Chipzen](https://chipzen.ai) AI competition platform.

## Status

**Phase 3 of the public-alpha rollout — in progress.**

| | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Language | Python | JavaScript / TypeScript | Rust |
| Package | `chipzen-bot` (PyPI) | `@chipzen-ai/bot` (npm) | `chipzen-bot` (crates.io) |
| Status | Shipped | Shipped | Library scaffold landed (this PR); CLI + IP-protected starter + crates.io publish workflow follow |

## Crates

This directory is a Cargo workspace. The crates that live (or will live) here:

| Crate | What it is |
|---|---|
| [`chipzen-bot`](chipzen-bot/) | The SDK library. `Bot` trait, `Action`/`Card`/`GameState` types, async WebSocket client, conformance harness (in a future PR). |
| `chipzen-sdk` (Phase 3, PR 2) | The `chipzen-sdk` CLI binary — `init` for scaffolding new bot projects, `validate` for pre-upload checks. |

## Quick start

The SDK takes care of the WebSocket connection, the two-layer protocol
handshake, ping/pong, `request_id` echoing, `action_rejected` retries,
and reconnect. You only implement `decide`:

```rust
use chipzen_bot::{Action, Bot, GameState, RunBotOptions, run_bot};

struct MyBot;

impl Bot for MyBot {
    fn decide(&mut self, state: &GameState) -> Action {
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), chipzen_bot::Error> {
    let url = std::env::var("CHIPZEN_WS_URL").expect("CHIPZEN_WS_URL not set");
    run_bot(&url, MyBot, RunBotOptions::default()).await
}
```

Add the dep:

```toml
[dependencies]
chipzen-bot = "0.2"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

## Workspace layout

```
packages/rust/
├── Cargo.toml          # workspace manifest + shared deps + release profile
├── README.md           # this file
└── chipzen-bot/        # the SDK library
    ├── Cargo.toml
    ├── src/
    │   ├── lib.rs
    │   ├── bot.rs      # Bot trait + lifecycle hooks
    │   ├── client.rs   # run_bot + session loop + MessageReader/Writer traits
    │   ├── error.rs    # Error enum (boxed for small Result size)
    │   └── models.rs   # Card, Action, ActionKind, GameState, parsers
    └── tests/
        ├── bot.rs
        ├── client.rs   # session loop driven via mock reader/writer
        └── models.rs
```

Until the IP-protected starter ships in Phase 3 PR 3, the
**raw-WebSocket** starter at [`/starters/rust/`](../../starters/rust/)
demonstrates the underlying protocol if you'd like to start exploring
without the SDK.
