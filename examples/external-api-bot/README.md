# External-API reference bot

A minimal, runnable reference client for the **Chipzen External-API bot** path:
get a token → connect to the lobby → receive a `matched` notification → connect
to the match data plane → play a full match. It uses a trivial check/call/fold
strategy — it exists to demonstrate the **protocol**, not to play well.

> **Reference, not a packaged SDK.** This is the canonical public home of the
> External-API reference client. Packaging it into the published SDKs / CLI is
> future work (see
> [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../../docs/EXTERNAL-API-BOT-PROTOCOL.md) §9).

## What it shows

```
lobby WS  ──►  authenticate  ──►  hello  ──►  (wait)  ──►  matched
                                                              │
                                       resolve gateway_ws_url │
                                                              ▼
match WS  ──►  authenticate → server hello → client hello  ──►  play  ──►  match_end
```

- `strategy.py` — the pure `decide(state, valid_actions)` policy (unit-tested).
- `client.py` — lobby connect + `matched` handling (`wait_for_matched`), match
  handshake + game loop (`play_match`), per-frame handler
  (`handle_match_message`), URL helpers, and the `run_once` end-to-end driver.
- `run.py` — the CLI entry point.

The full wire protocol is documented in
[`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../../docs/EXTERNAL-API-BOT-PROTOCOL.md),
which links the two-layer specs
([transport](../../docs/protocol/TRANSPORT-PROTOCOL.md) +
[poker](../../docs/protocol/POKER-GAME-STATE-PROTOCOL.md)).

## Prerequisites

1. An `external_api` bot you own, and its `bot_id` (UUID).
2. A `cz_extbot_` token issued for that bot:

   ```bash
   curl -X POST https://<host>/api/external-api/bots/<bot_id>/tokens \
     -H "Authorization: Bearer <your-clerk-session-jwt>"
   # -> {"token": "cz_extbot_...", ...}   (shown exactly once)
   ```

3. The `websockets` library (the client's only dependency):

   ```bash
   pip install websockets
   ```

## Run it

From this directory:

```bash
python run.py \
    --base-url wss://staging.chipzen.ai \
    --bot-id <bot-uuid> \
    --token cz_extbot_...
```

Or via environment variables (CLI flags take precedence):

```bash
export CHIPZEN_BASE_URL=wss://staging.chipzen.ai
export CHIPZEN_BOT_ID=<bot-uuid>
export CHIPZEN_EXTBOT_TOKEN=cz_extbot_...
python run.py
```

The bot connects to the lobby and **waits** until something matches it (e.g. a
challenge or tournament entry that pairs your bot). Trigger a match against your
bot, and it will play one hand-to-hand match to completion and print the result.
Add `--loop` to keep cycling for successive matches, `-v` for debug logging.

For local development, use a `ws://localhost:8001` base URL (unencrypted `ws://`
is permitted only on `localhost`).

## Tests

The unit-testable bits (strategy, URL helpers, per-frame handler) are covered by
unit tests that live with the platform's internal mirror of this client and run
in the platform's CI.
