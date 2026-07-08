# chipzen-bot — Rust SDK for the Chipzen poker platform

> [!WARNING]
> **Beta software (pre-1.0).** This SDK is in active development; the
> public API may change between minor versions before 1.0. Pin to a
> specific version in production. Report issues at
> [chipzen-ai/chipzen-sdk/issues](https://github.com/chipzen-ai/chipzen-sdk/issues).

Build, test, and deploy poker bots in Rust for the
[Chipzen](https://chipzen.ai) AI competition platform.

## Status

The full 3-language SDK rollout is complete on `main`:

| | Python | JavaScript / TypeScript | Rust |
|---|---|---|---|
| Library | `chipzen-bot` (PyPI) | `@chipzen-ai/bot` (npm) | `chipzen-bot` (crates.io) |
| CLI | `chipzen-sdk` | `chipzen-sdk` | `chipzen-sdk` |
| IP-protected starter | Cython multi-stage | `bun build --compile` | `cargo build --release` |
| Conformance harness | `chipzen.conformance` | `runConformanceChecks` | `chipzen_bot::run_conformance_checks` |
| Publish workflow | PyPI Trusted Publishing | npm Trusted Publishing | crates.io Trusted Publishing |

## Crates

This directory is a Cargo workspace. The crates in it:

| Crate | What it is |
|---|---|
| [`chipzen-bot`](chipzen-bot/) | The SDK library. `Bot` trait, `Action`/`Card`/`GameState` types, async WebSocket client, and the conformance harness. |
| `chipzen-sdk` | The `chipzen-sdk` CLI binary — `init` for scaffolding new bot projects, `validate` for pre-upload checks. |

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
    // run_bot returns the match_end payload (or None on a drop); ignore it
    // here, or inspect the final standings.
    let _match_end = run_bot(&url, MyBot, RunBotOptions::default()).await?;
    Ok(())
}
```

Add the dep:

```toml
[dependencies]
chipzen-bot = "0.3"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

### External-API remote-play

The example above is the **containerized** path (the platform runs your
binary and injects `CHIPZEN_WS_URL`). To instead run a bot on **your own
machine** and have the platform match + dispatch you, use
`run_external_bot` with a long-lived `cz_extbot_` token:

```rust
use chipzen_bot::{run_external_cli, RunExternalArgs};

#[tokio::main]
async fn main() -> Result<(), chipzen_bot::Error> {
    // Reads token / bot_id / url from chipzen.toml (or pass them on args).
    // A fresh MyBot per match; plays until the lobby closes or you're evicted.
    run_external_cli(|| MyBot, RunExternalArgs::new()).await?;
    Ok(())
}
```

See [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../../docs/EXTERNAL-API-BOT-PROTOCOL.md)
and [`docs/PORTING-BETWEEN-SDKS.md`](../../docs/PORTING-BETWEEN-SDKS.md) §7.
Run `chipzen-sdk run-external` to verify your `chipzen.toml` + env setup
before connecting.

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
    │   ├── external.rs # run_external_bot — external-API lobby/gateway flow
    │   ├── connect.rs  # connect_to_chipzen — env→lobby-URL helper
    │   ├── config.rs   # chipzen.toml discovery + [external_api] parsing
    │   ├── retry.rs    # RetryPolicy — reconnect/backoff knobs
    │   ├── error.rs    # Error enum (boxed for small Result size)
    │   └── models.rs   # Card, Action, ActionKind, GameState, parsers
    └── tests/
        ├── bot.rs
        ├── client.rs   # session loop driven via mock reader/writer
        └── models.rs
```

The IP-protected starter at [`starters/rust/`](starters/rust/) uses the
SDK's `Bot` trait and ships a multi-stage Dockerfile that compiles your
`lib.rs` to a single statically-linked release binary (your `.rs` source
is not in the uploaded image). The protocol spec at
[`../../docs/protocol/`](../../docs/protocol/) remains authoritative if
you ever need to write a non-Rust client from scratch.
