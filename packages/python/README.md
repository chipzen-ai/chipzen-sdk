# chipzen-bot

The Python adapter for the [Chipzen](https://chipzen.ai) AI poker
competition platform. Wraps the WebSocket protocol so your bot only
has to implement `decide(state) -> action`, and ships a `validate`
CLI that confirms your bot will be accepted by the upload pipeline.

## Install

```bash
pip install chipzen-bot
```

Python 3.10+ is supported. The runtime dependency is a single package
(`websockets`); your bot can pull in whatever else it needs (numpy,
torch, etc.) on top.

## Minimal bot

```python
from chipzen import Bot, Action, GameState

class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()

if __name__ == "__main__":
    MyBot().run()
```

The SDK handles the Layer-1 transport handshake, Layer-2 game-state
parsing, ping/pong, request-id echoing, `action_rejected` retries,
and reconnect. Subclass `Bot`, override `decide()`, return an
`Action`. That's the entire surface for a working bot.

Lifecycle hooks (`on_match_start`, `on_round_start`, `on_phase_change`,
`on_turn_result`, `on_round_result`, `on_match_end`) are optional —
override them if you need to maintain per-match or per-hand state
between turns.

## CLI

The `chipzen-sdk` CLI is installed alongside the Python package:

| Command | Purpose |
|---|---|
| `chipzen-sdk init <name>` | Scaffold a new bot project from a starter template. |
| `chipzen-sdk validate <path>` | Run the same checks the upload pipeline runs (size, imports, sandbox-blocked modules, decide() timeout sniff). The supported go/no-go before docker packaging. |

Run `chipzen-sdk <command> --help` for the full option list per command.

## What the SDK is for (and what it isn't)

The SDK does three things and nothing else:

1. **Protocol adapter** — your bot doesn't hand-roll WebSockets.
2. **`chipzen-sdk validate`** — pre-upload conformance check.
3. **(Forthcoming) IP-protected Dockerfile recipe** — Cython
   multi-stage build that produces an image containing only compiled
   `.so` files, not your `.py` source.

It does **not** include a local match simulator, hand evaluator, or
opponent pool. Bot strength testing happens after upload; the platform
runs comprehensive bot-vs-bot evaluation as part of the submission
pipeline. If you want fast local iteration, write your own profiling
harness that calls your `Bot.decide()` directly with recorded
`GameState` objects.

## Reference

Full developer documentation lives in the [chipzen-sdk
repo](https://github.com/chipzen-ai/chipzen-sdk):

- [DEV-MANUAL](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md)
  — SDK reference, lifecycle hooks, performance budgets, container
  contract, troubleshooting.
- [QUICKSTART](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/QUICKSTART.md)
  — write a bot, validate it, package it (~10 minutes).
- [Protocol spec](https://github.com/chipzen-ai/chipzen-sdk/tree/main/docs/protocol)
  — Layer 1 (Transport) + Layer 2 (Poker game state). Authoritative.
- [Bot runtime security model](https://github.com/chipzen-ai/chipzen-sdk/blob/main/SECURITY.md)
  — what the platform enforces on uploaded bots (sandbox, network
  egress, resource limits).

Per-package quickstart: [QUICKSTART.md](QUICKSTART.md).

## License

Apache-2.0. See the [LICENSE](https://github.com/chipzen-ai/chipzen-sdk/blob/main/LICENSE)
file in the repo.
