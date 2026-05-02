# @chipzen-ai/bot

> [!WARNING]
> **Alpha software.** This SDK is in active development; the public
> API may change between minor versions before 1.0. Pin to a specific
> version in production. Report issues at
> [chipzen-ai/chipzen-sdk/issues](https://github.com/chipzen-ai/chipzen-sdk/issues).

The JavaScript adapter for the [Chipzen](https://chipzen.ai) AI poker
competition platform. Wraps the WebSocket protocol so your bot only
has to implement `decide(state) -> action`.

## Install

```bash
npm install @chipzen-ai/bot     # or pnpm / yarn
```

Node 20+ supported. Also runs cleanly on Bun (the IP-protected starter
Dockerfile uses `bun build --compile` to ship your bot as a single
binary). Single runtime dependency: `ws`.

## Minimal bot

```typescript
import { Bot, Action, GameState, runBot } from "@chipzen-ai/bot";

class MyBot extends Bot {
  decide(state: GameState): Action {
    if (state.validActions.includes("check")) return Action.check();
    return Action.fold();
  }
}

await runBot(process.env.CHIPZEN_WS_URL!, new MyBot(), {
  token: process.env.CHIPZEN_TOKEN,
});
```

The SDK handles the Layer-1 transport handshake, Layer-2 game-state
parsing, ping/pong, request-id echoing, `action_rejected` retries,
and reconnect. Subclass `Bot`, override `decide()`, return an
`Action`. That's the entire surface for a working bot.

Lifecycle hooks (`onMatchStart`, `onRoundStart`, `onPhaseChange`,
`onTurnResult`, `onRoundResult`, `onMatchEnd`) are optional —
override them if you need to maintain per-match or per-hand state
between turns.

## Field naming

The user-facing API uses idiomatic camelCase
(`state.validActions`, `state.holeCards`). The on-the-wire JSON the
protocol uses is snake_case (`valid_actions`, `your_hole_cards`) —
defined in the
[protocol spec](https://github.com/chipzen-ai/chipzen-sdk/tree/main/docs/protocol).
The parser in `parseGameState` translates between the two.

## What the SDK is for (and what it isn't)

The SDK does three things and nothing else:

1. **Protocol adapter** — your bot doesn't hand-roll WebSockets.
2. **`chipzen-sdk validate`** (Phase 2 PR 2) — pre-upload conformance
   check, equivalent to the Python CLI.
3. **(Phase 2 PR 3) IP-protected Dockerfile recipe** — `bun build
   --compile` multi-stage build that produces a single executable
   instead of shipping your `.ts`/`.js` source.

It does **not** include a local match simulator, hand evaluator, or
opponent pool. Bot strength testing happens after upload; the
platform runs comprehensive bot-vs-bot evaluation as part of the
submission pipeline.

## Reference

Full developer documentation lives in the
[chipzen-sdk repo](https://github.com/chipzen-ai/chipzen-sdk):

- [DEV-MANUAL](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md)
  — SDK reference, lifecycle hooks, performance budgets, container
  contract, troubleshooting.
- [QUICKSTART](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/QUICKSTART.md)
  — write a bot, validate it, package it.
- [Protocol spec](https://github.com/chipzen-ai/chipzen-sdk/tree/main/docs/protocol)
  — Layer 1 (Transport) + Layer 2 (Poker game state). Authoritative.
- [Bot runtime security model](https://github.com/chipzen-ai/chipzen-sdk/blob/main/SECURITY.md#bot-runtime--what-the-platform-enforces-on-uploaded-bots)
  — what the platform enforces on uploaded bots.
- [Python adapter](https://github.com/chipzen-ai/chipzen-sdk/tree/main/packages/python)
  — same SDK shape in Python (pip install `chipzen-bot`).

## License

Apache-2.0. See the
[LICENSE](https://github.com/chipzen-ai/chipzen-sdk/blob/main/LICENSE)
file in the repo.
