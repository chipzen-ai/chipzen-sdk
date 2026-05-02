# Reference bot — Rust

A non-trivial **demonstration** Chipzen bot built on top of the public
`chipzen-bot` Rust crate. ~300 lines of plain, readable Rust that
shows off everything the protocol exposes:

- Per-match state via `on_match_start` (seat assignment from the
  `seats` array).
- Per-hand state via `on_round_start` (resets per-hand trackers).
- Live observation via `on_turn_result` (counts opponent aggression
  in the current hand).
- Branching on `state.phase` for preflop vs. postflop.
- Heuristic preflop bucketing using `state.hole_cards` (`Premium` /
  `Strong` / `Medium` / `Weak`).
- Made-hand class detection from `state.hole_cards` + `state.board`
  (no pair / pair / two pair / trips+).
- Action-history awareness — the per-hand counter is reconciled
  against `state.action_history` for cross-validation.
- Strict `state.valid_actions` and `min_raise` / `max_raise` checking
  — the bot will never return an action the server hasn't offered.

**It's not a strong bot.** It folds too much, doesn't bluff, ignores
pot odds, and has no draw recognition. The point of this file is to
show that the SDK can carry real strategy state cleanly, not to win
matches. If you're starting your own bot, run
`chipzen-sdk init <name>` instead — that gives you a thin scaffold
with the IP-protected `cargo build --release` Dockerfile.

The Python and JavaScript ports of this same logic live at
[`../reference-bot/`](../reference-bot/) and
[`../reference-bot-js/`](../reference-bot-js/) respectively.

## Running

```bash
cd examples/reference-bot-rust
CHIPZEN_WS_URL=ws://localhost:8001/ws/match/<match_id>/bot \
  CHIPZEN_TOKEN= \
  cargo run
```

Or via Docker (see the IP-protected starter at
[`packages/rust/starters/rust/`](../../packages/rust/starters/rust/)
for the production-shaped image; this reference bot deliberately ships
no Dockerfile of its own).

## Note: not yet runnable from crates.io

This bot's `Cargo.toml` declares `chipzen-bot = "0.2"` from
crates.io. Until the crate is published (see
[`packages/rust/RELEASING.md`](../../packages/rust/RELEASING.md)),
`cargo build` here will fail with `error: no matching package named
'chipzen-bot' found`. Use a `[patch.crates-io]` override pointing at
`packages/rust/chipzen-bot` if you need to run this before the first
publish.
