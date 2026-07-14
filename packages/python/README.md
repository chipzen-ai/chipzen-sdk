# chipzen-bot

> [!WARNING]
> **Beta software (pre-1.0).** This SDK is in active development; the
> public API may change between minor versions before 1.0. Pin to a
> specific version in production. Report issues at
> [chipzen-ai/chipzen-sdk/issues](https://github.com/chipzen-ai/chipzen-sdk/issues).

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
import asyncio
import os
from chipzen import Bot, Action, GameState, run_bot

class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()

if __name__ == "__main__":
    # An uploaded bot gets its match URL from the platform via $CHIPZEN_WS_URL.
    asyncio.run(run_bot(os.environ["CHIPZEN_WS_URL"], MyBot(),
                        token=os.environ.get("CHIPZEN_TOKEN")))
```

> To run a bot **remotely from your own machine** instead of uploading it, use
> `run_external_bot(...)` / `chipzen run-external` — see [Two ways to run a bot](#two-ways-to-run-a-bot).

The SDK handles the Layer-1 transport handshake, Layer-2 game-state
parsing, ping/pong, request-id echoing, `action_rejected` retries,
and reconnect. Subclass `Bot`, override `decide()`, return an
`Action`. That's the entire surface for a working bot.

Lifecycle hooks (`on_match_start`, `on_round_start`, `on_phase_change`,
`on_turn_result`, `on_round_result`, `on_match_end`,
`on_decision_latency`) are optional — override them if you need to
maintain per-match or per-hand state between turns or log your decision
timings.

## Two ways to run a bot

The same `Bot` class works on both paths:

- **Upload (containerized).** Package your bot as an image and submit it; the
  platform's executor runs it. This is the `run_bot(...)` / `chipzen-sdk
  validate` + Docker path above — best for ranked competition and tournaments.
- **External-API (remote play).** Run your bot on your own machine and let the
  platform match and dispatch it over the public token-authed API — no upload,
  fast iteration:

  ```python
  import asyncio
  from chipzen import Bot, run_external_bot

  asyncio.run(run_external_bot(MyBot(), bot_id="<bot-uuid>", env="staging",
                               token="cz_extbot_..."))
  ```

  It holds one lobby connection and plays every match dispatched to your bot — a
  single challenge, or each round of a tournament. Put the token in a
  `chipzen.toml` (`[external_api] token = "cz_extbot_..."`, optional `bot_id` /
  `url`) and the CLI is a one-liner:

  ```bash
  chipzen run-external my_bot.py --env staging
  ```

  Tunables: `connect_to_chipzen()` (env→URL), `RetryPolicy` (reconnect/backoff),
  `safe_mode=False` (crash on a `decide()` bug instead of folding — for
  dev/eval). See [`docs/external-api/FIRST-30-MINUTES.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/external-api/FIRST-30-MINUTES.md).

## CLI

The `chipzen-sdk` CLI (aliased as `chipzen`) is installed alongside the package:

| Command | Purpose |
|---|---|
| `chipzen-sdk init <name>` | Scaffold a new bot project from a starter template. |
| `chipzen-sdk validate <path>` | Run the same checks the upload pipeline runs (size, imports, sandbox-blocked modules, decide() timeout sniff). The supported go/no-go before docker packaging. |
| `chipzen run-external <bot.py>` | Run a bot on the external-API remote-play path (lobby → matched → play). |

Run `chipzen-sdk <command> --help` for the full option list per command.

## What the SDK is for (and what it isn't)

The SDK does three things and nothing else:

1. **Protocol adapter** — your bot doesn't hand-roll WebSockets.
2. **`chipzen-sdk validate`** — pre-upload conformance check.
3. **IP-protected Dockerfile recipe** — the Cython multi-stage build
   that ships in
   [`starters/python/Dockerfile`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/packages/python/starters/python/Dockerfile)
   (`cythonize -i bot.py && rm bot.py`) produces an image containing only
   compiled `.so` files, not your `.py` source.

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

Per-package quickstart: [QUICKSTART.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/packages/python/QUICKSTART.md).

## License

Apache-2.0. See the [LICENSE](https://github.com/chipzen-ai/chipzen-sdk/blob/main/LICENSE)
file in the repo.
