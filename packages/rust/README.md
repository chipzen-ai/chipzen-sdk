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
| [`chipzen-bot`](https://github.com/chipzen-ai/chipzen-sdk/tree/main/packages/rust/chipzen-bot) | The SDK library. `Bot` trait, `Action`/`Card`/`GameState` types, async WebSocket client, and the conformance harness. |
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

See [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/EXTERNAL-API-BOT-PROTOCOL.md)
and [`docs/PORTING-BETWEEN-SDKS.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/PORTING-BETWEEN-SDKS.md) §7.
Run `chipzen-sdk run-external` to verify your `chipzen.toml` + env setup
before connecting.

### Other games

A bot plays No-Limit Hold'em unless it declares otherwise. To declare the
games it can play, set `supported_games` on `RunBotOptions`,
`RunExternalOptions` or `RunExternalArgs` to a list of `game_type` strings;
it goes on the client `hello` as `supported_games`:

```rust
let options = RunBotOptions {
    supported_games: Some(vec!["poker".to_string(), "draw27".to_string()]),
    ..Default::default()
};
```

Leaving it `None` (the default) declares nothing ("poker only") and keeps
the `hello` unchanged; an empty list is sent as is and means "supports
nothing".

**2-7 Triple Draw** (`draw27`) is fixed-limit lowball with three draws. One
`GameState` covers both kinds of turn: branch on `state.is_draw_phase`. On a
draw turn `valid_actions` is exactly `["draw"]`; answer with
`Action::discard(...)` (card strings, `Card`s or 0-based positions, at most
`state.max_discard`) or `Action::stand_pat()`. The optional
`Bot::decide_draw` hook defaults to a stand pat and is only called if your
`decide` calls it:

```rust
fn decide(&mut self, state: &GameState) -> Action {
    if state.is_draw_phase {
        return self.decide_draw(state);
    }
    // betting turn: fixed limit, so min_raise == max_raise
    if state.valid_actions.iter().any(|a| a == "check") { Action::Check } else { Action::Fold }
}

fn decide_draw(&mut self, state: &GameState) -> Action {
    Action::discard(&state.hole_cards[..2]) // throw the first two cards
}
```

**Pineapple OFC** (`ofc`) has no betting: every turn places cards. On an
OFC turn `valid_actions` is exactly `["place"]` and the shape of a legal
answer lives in the state: `state.cards_to_place`, `state.place` (how many
to place), `state.must_discard` and `state.row_capacity` (free slots per
row). Answer with `Action::place(placements, discard)`. An illegal
placement is **rejected, not clamped**, so check `row_capacity` first. The
optional `Bot::decide_placement` hook returns a legal (not good) placement
and is only called if your `decide` calls it:

```rust
fn decide(&mut self, state: &GameState) -> Action {
    if state.valid_actions.iter().any(|a| a == "place") {
        return self.decide_placement(state); // Pineapple OFC: override it
    }
    if state.valid_actions.iter().any(|a| a == "check") { Action::Check } else { Action::Fold }
}
```

Building a placement by hand:

```rust
// A pineapple street (place 2, discard 1):
Action::place([("Ks", "bottom"), ("Js", "middle")], ["9s"]);
// The opening set (place 5, nothing to discard):
Action::place(
    [("2c", "top"), ("3d", "middle"), ("4h", "middle"), ("5s", "bottom"), ("6s", "bottom")],
    None::<&str>,
);
```

The OFC fields (`your_rows`, `opponent_rows`, `royalties`, `point_value`,
`in_fantasy_land`, `phase_sequence` and the rest) are always filled and
hold their defaults at other tables. A Fantasy Land turn is the same shape:
14 cards, place 13, discard 1. If `decide` panics or returns an action the
turn does not offer, the SDK sends the protocol's default placement for that
turn (each card to the row with the most room, ties toward the bottom), and
does the same when the server rejects an answer.

`Action` and `ActionKind` are `#[non_exhaustive]`: a `match` on them needs a
`_` arm, so a future game's action is not a breaking change.

See [DEV-MANUAL §2.8](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#28-playing-other-games).

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

The IP-protected starter at [`starters/rust/`](https://github.com/chipzen-ai/chipzen-sdk/tree/main/packages/rust/starters/rust) uses the
SDK's `Bot` trait and ships a multi-stage Dockerfile that compiles your
`lib.rs` to a single statically-linked release binary (your `.rs` source
is not in the uploaded image). The protocol spec at
[`docs/protocol/`](https://github.com/chipzen-ai/chipzen-sdk/tree/main/docs/protocol) remains authoritative if
you ever need to write a non-Rust client from scratch.
