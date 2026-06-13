# Reference bot — JavaScript

A non-trivial **demonstration** Chipzen bot built on top of the public
`@chipzen-ai/bot` SDK. ~250 lines of plain, readable JavaScript that
shows off everything the protocol exposes:

- Per-match state via `onMatchStart` (seat assignment).
- Per-hand state via `onRoundStart` (resets per-hand trackers).
- Live observation via `onTurnResult` (counts opponent aggression in
  the current hand).
- Branching on `state.phase` for preflop vs. postflop.
- Heuristic preflop bucketing using `state.holeCards` (`premium` /
  `strong` / `medium` / `weak`).
- Made-hand class detection from `state.holeCards` + `state.board`
  (no pair / pair / two pair / trips+).
- Action-history awareness — the per-hand counter is reconciled
  against `state.actionHistory` for cross-validation.
- Strict `state.validActions` and `minRaise` / `maxRaise` checking —
  the bot will never return an action the server hasn't offered.

**It's not a strong bot.** It folds too much, doesn't bluff, ignores
pot odds, and has no draw recognition. The point of this file is to
show that the SDK can carry real strategy state cleanly, not to win
matches. If you're starting your own bot, run
`chipzen-sdk init <name>` instead — that gives you a thin scaffold
with the IP-protected Bun-compile Dockerfile.

The Python and Rust ports of this same logic live at
[`../reference-bot/`](../reference-bot/) and
[`../reference-bot-rust/`](../reference-bot-rust/) respectively.

## Running

```bash
cd examples/reference-bot-js
npm install
CHIPZEN_WS_URL=ws://localhost:8001/ws/match/<match_id>/bot \
  CHIPZEN_TOKEN= \
  node bot.js
```

Or via Docker (see the IP-protected starter at
[`packages/javascript/starters/javascript/`](../../packages/javascript/starters/javascript/)
for the production-shaped image; this reference bot deliberately ships
no Dockerfile of its own).

## Installing from npm

This bot's `package.json` pulls `@chipzen-ai/bot ^0.3.0` straight from
the npm registry, so `npm install` above just works — the package is
published at
[npmjs.com/package/@chipzen-ai/bot](https://www.npmjs.com/package/@chipzen-ai/bot).

To develop against an unreleased local build instead, point the
dependency at [`packages/javascript/`](../../packages/javascript/) via a
relative `file:` path (or a workspace link).
