# Chipzen Starter Bots

Ready-to-run bot templates for the Chipzen poker platform. Each implements the
same tight-aggressive strategy (raise strong hands preflop, check-call postflop)
against the Chipzen **two-layer protocol**:

- **Layer 1 (Transport):** connection, authentication, turn sequencing, timing.
  See [`../docs/protocol/TRANSPORT-PROTOCOL.md`](../docs/protocol/TRANSPORT-PROTOCOL.md).
- **Layer 2 (Poker):** NLHE-specific game state, action vocabulary, payloads.
  See [`../docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](../docs/protocol/POKER-GAME-STATE-PROTOCOL.md).

Pick your language and start hacking.

## Quick Start

### Python

```bash
cd python
pip install -r requirements.txt
python bot.py ws://localhost:8001/ws/match/{match_id}/bot
```

### JavaScript

The JavaScript starter has moved to
[`../packages/javascript/starters/javascript/`](../packages/javascript/starters/javascript/)
now that the JS SDK ships on npm. Use it via:

```bash
npm install @chipzen-ai/bot
chipzen-sdk init my-bot   # scaffold a project that uses the SDK
```

### Rust

```bash
cd rust
cargo run -- ws://localhost:8001/ws/match/{match_id}/bot
```

## Environment Variables

All three starters accept the same configuration via env vars (useful for
Docker / CI):

| Variable            | Description                                                 |
|---------------------|-------------------------------------------------------------|
| `CHIPZEN_WS_URL`    | WebSocket URL (used when no CLI arg is provided).           |
| `CHIPZEN_TOKEN`     | Bot API token for `/ws/match/{match_id}/bot` endpoints.     |
| `CHIPZEN_TICKET`    | One-time ticket for competitive `/ws/match/{...}` endpoints.|
| `CHIPZEN_MATCH_ID`  | Match UUID (auto-extracted from the URL if not provided).   |

Exactly one of `CHIPZEN_TOKEN` / `CHIPZEN_TICKET` should be set for production
endpoints. Local sidecar / dev endpoints may accept an empty token.

## Docker

Each starter includes a Dockerfile:

```bash
# Python example
cd python
docker build -t my-bot .
docker run --rm -e CHIPZEN_TOKEN=... my-bot ws://host.docker.internal:8001/ws/match/{match_id}/bot

# JavaScript example
cd ../packages/javascript/starters/javascript
docker build -t my-bot .
docker run --rm -e CHIPZEN_TOKEN=... my-bot ws://host.docker.internal:8001/ws/match/{match_id}/bot

# Rust example
cd rust
docker build -t my-bot .
docker run --rm -e CHIPZEN_TOKEN=... my-bot ws://host.docker.internal:8001/ws/match/{match_id}/bot
```

## Protocol at a Glance

1. Bot connects via WebSocket.
2. Bot sends `authenticate` (with `token` or `ticket`).
3. Server replies with `hello` (includes `supported_versions`, `game_type`).
4. Bot replies with `hello` (includes `supported_versions`, `client_name`,
   `client_version`).
5. Server sends `match_start` with the `game_config` (blinds, stacks, hand count).
6. Per hand: `round_start` -> (`turn_request` -> `turn_action` -> `turn_result`)*
   -> `phase_change`\* -> `round_result`.
7. `match_end` closes the match.

### Bot responsibilities

- Echo the `request_id` from `turn_request` in the `turn_action` reply.
- Use `action` strings from `valid_actions` (`fold` / `check` / `call` / `raise`
  / `all_in`) and pass raise amounts inside `params`, e.g.
  `{"action": "raise", "params": {"amount": 60}}`.
- Respond to every server `ping` with a `pong` within 5000ms.
- Retry after `action_rejected` using the **same** `request_id` (the turn is
  still open until `remaining_ms` hits zero).
- Ignore unknown message types (forward-compat).

See [`../docs/protocol/TRANSPORT-PROTOCOL.md`](../docs/protocol/TRANSPORT-PROTOCOL.md)
and [`../docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](../docs/protocol/POKER-GAME-STATE-PROTOCOL.md)
for the authoritative specification.

## Customizing

These starters are intentionally simple. Ideas to improve:

- Track community cards and evaluate hand strength postflop.
- Adjust bet sizing based on pot odds and stack depth (`your_stack`,
  `opponent_stacks`).
- Use `action_history` in `turn_request.state` for opponent modeling.
- Implement position-aware play (track `dealer_seat` from `round_start`).
- Use Monte Carlo simulation for equity estimation before raising.
- Handle `reconnected` gracefully (see
  [`../docs/protocol/TRANSPORT-PROTOCOL.md`](../docs/protocol/TRANSPORT-PROTOCOL.md)
  section 11) for long-running competitive matches.
