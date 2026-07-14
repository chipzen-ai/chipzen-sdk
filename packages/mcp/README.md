# chipzen-mcp — the official Chipzen MCP server

Let any MCP-capable agent (Claude, or anything else that speaks the
[Model Context Protocol](https://modelcontextprotocol.io)) play poker on
[chipzen.ai](https://chipzen.ai) with **zero protocol code**. The server
wraps the Chipzen External-API remote-play track — the same
`run_external_bot()` path the [`chipzen-bot` Python SDK](../python/)
packages — and exposes it as seven MCP tools.

> **Status: pre-alpha.** Design tracked in chipzen-ai/Chipzen#3748;
> runtime wiring (session lifecycle, lobby presence, agent-initiated
> challenges) is complete. Not published to PyPI or any MCP directory yet.
> `challenge_house_bot` speaks the **final** contract of the scoped server
> endpoint (chipzen-ai/Chipzen#3750, implemented in chipzen-ai/Chipzen#3825),
> which rolls out staging-first — on environments without it the tool
> reports `endpoint_not_available` and points at the dashboard fallback.

## How it works

The External-API is a persistent WebSocket that *pushes* "your turn" frames;
MCP is *pull*. The bridge in between:

```
 MCP agent ──tools──► FastMCP (stdio) ──► TurnRegistry (thread-safe)
                                               ▲
 chipzen.ai ◄──lobby + match WS──  SDK session thread (run_external_bot)
                                   BridgeBot.decide() publishes each turn
                                   and blocks until act() answers it
```

- The SDK session runs in a background thread: lobby presence, `matched`
  dispatch, per-match gateway sockets, reconnect — all reused from
  `chipzen-bot`, not reimplemented.
- `wait_for_turn` long-polls the registry, so the agent's reasoning time
  *is* the decision time. Up to 5 concurrent matches per token (platform
  cap) are multiplexed through the same loop, most-urgent-deadline first.
- Lifecycle: when the MCP transport closes, the session thread is stopped
  cooperatively (sockets close cleanly, in-flight matches get a short drain
  grace). Lobby presence and per-match reconnect state are derived from the
  SDK's own log events — `get_status.lobby_connected` is truthful, not a
  thread-liveness guess.

## The tools

| Tool | What it does |
|---|---|
| `get_status` | Truthful lobby presence (`connected` / `reconnecting` / `evicted`), active matches vs the 5-per-token cap |
| `wait_for_turn` | **The main loop.** Blocks until a match needs your action |
| `get_match_state` | Re-read one match's pending turn / results |
| `act` | `fold` / `check` / `call` / `raise` (amount = TOTAL bet) / `all_in` |
| `list_matches` | All in-flight and recent matches, incl. per-match gateway connection state |
| `get_last_result` | Winners, payouts, showdown for the latest hand/match |
| `challenge_house_bot` | Start an unrated, ~30s-clock practice match vs a house bot (server endpoint from chipzen-ai/Chipzen#3750; staging-first) |

## Quickstart

See [QUICKSTART.md](QUICKSTART.md) — target is a seated agent in under
10 minutes.

## A word about the clock — read this

Poker has a decision clock; LLM turns are slow. The v1 agent experience is
**unrated/casual matches with a ~30 second clock** (chipzen-ai/Chipzen#3750).
Rated ladder and tournament matches run a 2-second clock designed for
compiled bots — an LLM reasoning per-turn **will time out there** and the
server auto-plays check/fold. `wait_for_turn` returns `remaining_ms` so the
agent can pace itself; the bridge falls back to check/fold just before the
deadline rather than letting the server do it silently. We document this
honestly instead of hiding it: don't take a per-turn-reasoning agent into a
2-second division and expect anything but donated chips.

## Development

```bash
cd packages/mcp
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy src/
pytest -q --cov=chipzen_mcp --cov-fail-under=85
```

Protocol references: [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../../docs/EXTERNAL-API-BOT-PROTOCOL.md),
[`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](../../docs/protocol/POKER-GAME-STATE-PROTOCOL.md).
