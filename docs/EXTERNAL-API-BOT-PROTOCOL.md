# Chipzen External-API Bot Protocol

**Audience:** developers running a bot **outside** the Chipzen container pipeline —
a long-lived process anywhere on the internet that authenticates with a long-lived
API token (`cz_extbot_…`) and plays matches over WebSocket.

**Status:** this is the **canonical public home** of the External-API protocol doc
and its [reference client](#9-sdk-home--productionizing), moved here from the private
platform repo so external developers can actually reach them. The platform repo keeps
an internal mirror; content edits happen here first.

> **Already know the container pipeline?** The External-API path reuses the **same
> two-layer game protocol** a containerized (sandboxed) bot speaks
> ([`docs/protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) +
> [`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)). The
> **only** differences are: (1) you authenticate with a long-lived `cz_extbot_` token
> instead of a per-match ticket, and (2) you hold a standing **lobby** connection so the
> platform can tell you when you've been matched. Everything inside a match is identical.

---

## Table of Contents

1. [Overview — the two connections](#1-overview--the-two-connections)
2. [Step 1 — get a bot token](#2-step-1--get-a-bot-token)
3. [Step 2 — connect to the lobby](#3-step-2--connect-to-the-lobby)
4. [Step 3 — receive the `matched` notification](#4-step-3--receive-the-matched-notification)
5. [Step 4 — connect to the match data plane](#5-step-4--connect-to-the-match-data-plane)
6. [The two-layer game protocol](#6-the-two-layer-game-protocol)
7. [Match flows: ext-vs-container, ext-vs-ext, ext-vs-house-bot (unrated)](#7-match-flows)
8. [Error codes, close codes, rate limits, reconnect](#8-error-codes-close-codes-rate-limits-reconnect)
9. [SDK home / productionizing](#9-sdk-home--productionizing)

---

## 1. Overview — the two connections

An External-API bot maintains **two** WebSocket connections, with different lifetimes:

```
                          ┌──────────────────────── api (public ingress) ─────────────────────────┐
                          │                                                                        │
 your bot process         │   lobby WS  (standing)            per-match gateway relay (per match)  │
 ───────────────────────► │   /ws/external/bot/{bot_id}       /ws/external/match/{mid}/{pid}       │
                          │        │  presence + "matched"            │  verbatim byte-pipe        │
                          │        ▼                                  ▼                            │
                          │   Redis presence/notify            inner WS → executor bot route       │
                          └──────────────────────────────────────────────│─────────────────────────┘
                                                                          ▼
                                                              MatchRunner runs 100% on the executor
```

| Connection | URL | Lifetime | Purpose |
|---|---|---|---|
| **Lobby** | `wss://<host>/ws/external/bot/{bot_id}` | Standing (between + across matches) | Register "I'm online"; receive `matched` notifications; heartbeat |
| **Match data plane** | `wss://<host>/ws/external/match/{match_id}/{participant_id}` (token in `Sec-WebSocket-Protocol`) | One per match | Play a single match end-to-end |

**Why two connections?** The lobby is a pure presence/matchmaking channel. Match frames flow
on a **separate, fresh** WS per match — a bot-token-authed relay that pumps your frames
verbatim to a dedicated match **executor** task (`MatchRunner` runs there, never on the api).
This is the same relay shape human players use for `/play`, and it means **any** api task can
serve **any** participant: your lobby socket can live on one api task while your match is
relayed by another. You never reach the executor directly — its IPs are private; the api is
your only ingress for both connections.

`<host>` is the same origin for both connections — e.g. `staging.chipzen.ai` /
`chipzen.ai`. Production is `wss://` only; `ws://` is permitted **only** on `localhost`
for local development.

---

## 2. Step 1 — get a bot token

External-API tokens are minted over the authenticated HTTP API for a bot whose
`bot_kind == external_api`, owned by your account. (Sandboxed / container-pipeline bots
authenticate with per-match tickets and cannot have `cz_extbot_` tokens.)

The HTTP surface is mounted under `/api/external-api`:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/external-api/bots/{bot_id}/tokens` | Issue a new token |
| `GET`  | `/api/external-api/bots/{bot_id}/tokens` | List non-revoked tokens (metadata only) |
| `POST` | `/api/external-api/tokens/{token_id}/rotate` | Atomically revoke + re-issue |
| `DELETE` | `/api/external-api/tokens/{token_id}` | Revoke a token |
| `GET`  | `/api/external-api/bots/{bot_id}/latency-stats` | p50/p95/p99 decision-latency percentiles |

### Issue a token

```bash
curl -X POST https://<host>/api/external-api/bots/<bot_id>/tokens \
  -H "Authorization: Bearer <your-clerk-session-jwt>"
```

Response (the plaintext is returned **exactly once** — only a bcrypt hash is persisted):

```json
{
  "token": "cz_extbot_AbC123…",
  "token_prefix": "cz_extbot",
  "created_at": "2026-05-28T12:00:00Z",
  "warning": "Save this token now. We do not store the plaintext and cannot show it again."
}
```

Store `token` somewhere safe immediately. If you lose it, rotate (which revokes the old
one and issues a new one) — you cannot retrieve a plaintext after issuance.

> **Token shape.** A token is `cz_extbot_` followed by 32 base62 chars. The server checks
> the structural prefix before doing any bcrypt work, so a malformed token is rejected fast.

The same token is used for **both** the lobby connection and every per-match gateway
connection. You may have multiple tokens per bot (e.g. one per deployment); each one is
subject to its own per-token concurrent-match cap (see [§8](#8-error-codes-close-codes-rate-limits-reconnect)).

---

## 3. Step 2 — connect to the lobby

Open the lobby WS and authenticate. The lobby is where the platform tracks "is this bot
online?" (cross-task, Redis-backed) and where it delivers `matched` notifications.

```
wss://<host>/ws/external/bot/{bot_id}
```

`{bot_id}` is your bot's UUID and **must** match the bot the token resolves to — a token
for bot A used at the URL for bot B is rejected with close code **4001**.

### Lobby handshake

1. Open the WS.
2. Send the authenticate frame as your **first** message:

   ```json
   {"type": "authenticate", "token": "cz_extbot_AbC123…"}
   ```

3. The server validates the token and replies with a `hello` envelope confirming you're
   connected. The lobby `hello` is **not** a game handshake — there is no match yet — so it
   carries `endpoint: "lobby"` and advertises the supported protocol versions for
   forward-compat. Its `match_id` field carries your `bot_id` (the envelope shape requires a
   string; treat it as an opaque correlation id on the lobby socket, not a match):

   ```json
   {
     "type": "hello",
     "match_id": "<your-bot-id>",
     "seq": 1,
     "server_ts": "2026-05-28T12:00:00.000Z",
     "supported_versions": ["1.0"],
     "selected_version": "1.0",
     "server_name": "chipzen",
     "endpoint": "lobby"
   }
   ```

   You do **not** send a client `hello` back on the lobby socket. Once you've received the
   server `hello`, you're registered as online.

### Lobby heartbeat

The server pings every **15s** and expects a `pong` within **5s**:

```
S→B  {"type": "ping"}
B→S  {"type": "pong"}
```

Miss a pong and the server closes with **4000** (heartbeat failed). Each heartbeat cycle the
server also re-checks your token's revocation state; if you revoke the token mid-session the
lobby closes with **4002** (token revoked).

> **Single connection per bot.** Opening a second lobby connection for the same bot evicts
> the first — the older socket is closed (cross-task) with code **4000** ("Replaced by new
> connection"). Run exactly one lobby connection per bot.

> **Lobby frames are presence-only.** The lobby socket carries `hello`, `ping`/`pong`, and
> `matched`/`evict` notifications. Do **not** send game frames on the lobby socket — they are
> silently dropped. Match play happens on the per-match gateway WS only.

---

## 4. Step 3 — receive the `matched` notification

When the platform pairs your bot into a match, it publishes a `matched` notification that the
lobby forwards to you verbatim on the lobby WS:

```json
{
  "type": "matched",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "participant_id": "p_your_seat_uuid",
  "gateway_ws_url": "/ws/external/match/a1b2c3d4-…/p_your_seat_uuid",
  "rated": true
}
```

| Field | Description |
|---|---|
| `match_id` | The match UUID you've been assigned to |
| `participant_id` | **Your** seat's participant id for this match (use it in the gateway URL) |
| `gateway_ws_url` | The **path** to dial for the match data plane (resolve against the same origin as the lobby) |
| `rated` | `true` for a ranked match; `false` for an unrated sandbox match (e.g. vs a CZ house bot — see [§7](#7-match-flows)) |

On receiving `matched`, open a **fresh** WS to `gateway_ws_url` (next step). Keep the lobby
socket open — it stays up across matches, and you may receive further `matched` notifications
for additional concurrent matches (subject to your concurrent-match cap).

---

## 5. Step 4 — connect to the match data plane

Dial the match gateway, passing your `cz_extbot_` token in the
**`Sec-WebSocket-Protocol`** request header (NOT the query string — see the note below):

```
URL:   wss://<host>/ws/external/match/{match_id}/{participant_id}
Header: Sec-WebSocket-Protocol: chipzen-bot-token, cz_extbot_AbC123…
```

- `{match_id}` and `{participant_id}` come from the `matched` notification.
- Offer **two** subprotocols: the sentinel `chipzen-bot-token` followed by your token value.
  The gateway reads the token from this header and echoes the sentinel back on accept (per
  RFC 6455 the server must select one of your offered subprotocols). In the `websockets`
  Python lib: `websockets.connect(url, subprotocols=["chipzen-bot-token", token])`.
- The token authenticates **you to the api gateway**. The gateway validates it (same bcrypt
  validator the lobby uses) and enforces a **seat-ownership** check: the
  `(match_id, participant_id)` seat in the DB must belong to the bot the token resolves to —
  you cannot relay into a seat you don't own.
- The token **never reaches the executor.** The gateway mints a fresh, short-lived internal
  executor JWT for the inner leg; the executor only ever sees that internal credential.

> **Why a header and not the query string?** A long-lived `cz_extbot_` credential on the URL
> query string lands in plaintext in every access log along the path (CDN, load balancer,
> any reverse proxy), in browser history, and in `Referer` headers (CZ issue 2932). The
> `Sec-WebSocket-Protocol` header carries it at connect time without any of that exposure; the
> gateway then pumps every subsequent frame verbatim.
>
> **Deprecated fallback:** a `?token=cz_extbot_…` query string is still accepted during the
> beta cutover so bots written against the old protocol keep working, but it is deprecated and
> will be removed — migrate to the header.

Once the gateway accepts your socket, you are speaking **directly** to the match executor's
bot route through a verbatim relay. Run the standard two-layer handshake next.

---

## 6. The two-layer game protocol

Inside a match, an External-API bot speaks **exactly** the protocol a containerized bot
speaks. Full specs:

- **Layer 1 (transport):** [`docs/protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md)
- **Layer 2 (poker / NLHE payloads):** [`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)

### 6.1 Match handshake (Layer 1)

After the gateway accepts the match WS, run the bot handshake:

```
B→S  {"type": "authenticate", "match_id": "<match_id>", "token": "cz_extbot_AbC123…"}
S→B  {"type": "hello", "match_id": "<match_id>", "seq": 1, …,
      "supported_versions": ["1.0"], "selected_version": "1.0",
      "game_type": "poker", "capabilities": ["reconnect"]}
B→S  {"type": "hello", "match_id": "<match_id>", "supported_versions": ["1.0"],
      "client_name": "my-bot", "client_version": "1.0.0"}
```

> **The `authenticate` frame is drained for protocol compliance but its credential is
> ignored** by the executor — the internal JWT minted by the gateway off the URL is
> authoritative. You **must** still send `authenticate` as your first match frame (the
> executor parks waiting for it; skipping it stalls the handshake). Sending your `cz_extbot_`
> token as the frame's `token` is fine and recommended for symmetry with the container path.

`capabilities` advertises `reconnect` when the executor honors it (see
[§8](#8-error-codes-close-codes-rate-limits-reconnect)).

### 6.2 Game loop (Layers 1 + 2)

After `hello`, the server drives the match. Handle each message type:

| Server → bot | What to do |
|---|---|
| `match_start` | Read `seats` (find your `is_self` seat), `game_config` (blinds, stacks, `num_players`), `turn_timeout_ms` |
| `round_start` | New hand; `state.your_hole_cards` are your cards |
| `turn_request` | **Your turn.** Pick an action from `valid_actions`, reply with `turn_action` echoing `request_id` |
| `turn_result` | An action (yours or opponent's) was applied |
| `phase_change` | Board advanced (flop/turn/river) |
| `round_result` | Hand over (`winner_seats`, `payouts`) |
| `action_rejected` | Your action was illegal; retry with the **same** `request_id` within `remaining_ms` |
| `action_timeout` | You ran out of time; server auto-applied `check` if legal else `fold` |
| `ping` | Reply `{"type": "pong", "match_id": "<match_id>"}` |
| `match_end` | Match over (`reason`, `results`); close the match WS |
| `error` | Diagnostic; `code` + `message` |
| anything else | **Silently ignore** (forward-compat — new types may appear) |

The `turn_action` you send:

```json
{"type": "turn_action", "match_id": "<match_id>", "request_id": "<echoed>",
 "action": "raise", "params": {"amount": 60}}
```

Key Layer-2 rules (full detail in the poker protocol doc):

- `valid_actions` lists the legal action strings (`fold`, `check`, `call`, `raise`, `all_in`).
- For `raise`, `params.amount` is the **total bet size**, bounded by `state.min_raise` ≤
  amount ≤ `state.max_raise` (both `0` when raising isn't legal).
- `state.to_call` is the amount to call; `0` means checking is free.
- `game_config.num_players` is the seat count. The protocol supports 2–6 players, but **remote play via the external-API SDK is currently heads-up only** (`num_players == 2`); multi-way remote support is tracked in chipzen-ai/Chipzen#3742.
- Bot→server messages must be ≤ **4096 bytes** (close **4008** otherwise).

### 6.2.1 Recovering from `action_rejected`

The server is authoritative: it validates every `turn_action` against the canonical state. If
your action is illegal (an action type not in `valid_actions`, a wrong-turn submission, etc.)
the server sends `action_rejected` (carrying `reason`, `valid_actions`, and `remaining_ms`)
**instead of** applying it, and waits for a corrected action. Reply with a **legal**
`turn_action` echoing the **same** `request_id` within `remaining_ms` — the retry is accepted
and play continues; a rejected attempt does **not** consume your turn. Only repeated failures
(no legal action before time runs out, across enough turns) auto-substitute and ultimately end
the match. An out-of-range `raise` amount is **clamped** to the legal range and accepted (not
rejected). This reject→retry loop is identical whether your bot is gateway-relayed (external
API) or a container — the relay forwards `action_rejected` to you verbatim.

### 6.3 Match end

On `match_end`, the gateway also sends a clean terminal `match_end` frame if the executor
closes first, then closes the match WS with **1000**. Treat `match_end` as the signal to tear
down the match WS — but **keep the lobby open** for the next match.

---

## 7. Match flows

### 7.1 ext-vs-container (uploaded / other-image bot)

Your seat is gateway-served; the opponent is launched as a container on the executor exactly
like a bot-vs-bot match. You see no difference — both seats just connect and play. Rated by
default.

### 7.2 ext-vs-ext (two External-API bots)

Both seats are gateway-served — **zero containers** are launched. Each bot receives its **own**
`matched` notification carrying its own `participant_id` + `gateway_ws_url`, and each connects
its own match WS. The executor treats a seat identically whether a container or a remote
gateway fills it. Rated when both are non-system bots.

### 7.3 ext-vs-house-bot sandbox (unrated)

You may be matched against a **CZ house bot** (the platform sentinel) for practice. These are
**unrated** sandbox matches: `matched.rated == false`. The match completes and persists
normally (results, latency rows) but does **not** move your Glicko-2 rating. This is the same
"no ranked play against system bots" rule the challenge path enforces — there's no separate
flag and no hardcoded house-bot id; it's derived from the opponent's owner being a system user.

In all three flows the **game protocol on your match WS is identical** — the differences are
purely server-side (whether the opponent is a container or another gateway seat, and whether
the match is rated).

---

## 8. Error codes, close codes, rate limits, reconnect

> Beyond the ExtAPI-specific codes below, the platform-wide catalogues live at
> [`docs/ERROR-CODES.md`](ERROR-CODES.md) (every close code / protocol error / HTTP error
> with remediation) and [`docs/COMMON-PITFALLS.md`](COMMON-PITFALLS.md) (real bot failure
> modes: symptom / why / detect / fix).

### 8.1 Lobby close codes

| Code | Meaning | Cause |
|---|---|---|
| `1008` | policy violation | Too many connection attempts from your IP (connect rate-limit) |
| `4001` | auth failed | Missing/invalid/malformed token, or token's bot ≠ URL `bot_id` |
| `4000` | heartbeat failed / replaced | Missed a pong **or** a newer connection for the same bot evicted this one |
| `4002` | token revoked | The token was revoked while the lobby connection was open |

### 8.2 Match gateway close codes

| Code | Meaning | Cause |
|---|---|---|
| `1008` | policy violation | Connect rate-limit (per IP) |
| `4001` | auth failed | Invalid/revoked `cz_extbot_` token, or the seat is not owned by your bot |
| `4002` | forbidden / capacity | Malformed `match_id`/`participant_id`, or **no executor registered** for this match yet |
| `1011` | executor unavailable | The gateway could not establish the inner WS to the executor |
| `1000` | normal | Match completed / executor closed cleanly |

> **Note on 4002 "no executor registered."** Don't dial the gateway before you've received the
> `matched` notification — the executor is allocated as part of dispatch, and the registry
> entry the gateway resolves against appears around then. If you dial too early (or after the
> match has been torn down) you'll get 4002. The correct trigger to dial is the `matched`
> frame.

### 8.3 In-match (Layer 1) close codes

The match WS shares the executor's transport close codes
([`TRANSPORT-PROTOCOL.md` §15](protocol/TRANSPORT-PROTOCOL.md#15-websocket-close-codes)). The ones
you're most likely to see:

| Code | Name | Meaning |
|---|---|---|
| `4007` | `handshake_timeout` | You didn't send `hello` within 5000ms of the server `hello` |
| `4008` | `message_too_large` | A bot→server frame exceeded 4096 bytes |
| `4009` | `rate_limit_exceeded` | 5th rate-limit violation (see below) |
| `4011` | `server_error` / resource | Unrecoverable server error, or duplicate connection for an already-attached seat |
| `4013` | `protocol_mismatch` | No mutually supported protocol version |

### 8.4 Rate limits

- **Connection rate-limit (per IP):** both WS endpoints reject excess connection attempts with
  `1008`. Hold your lobby connection open rather than reconnecting in a tight loop.
- **In-match (per `participant_id`):** 10 messages/second (rolling 1s) and 5 invalid actions
  per round. Violations 1–4 send an `error` frame with code `rate_limited`; the 5th closes the
  match WS with `4009`. Counters survive reconnection (tracked per participant, not per
  connection).
- **Per-token concurrent-match cap:** each `cz_extbot_` token can be in only so many
  simultaneous matches. Exceeding it causes the **dispatch** to be rejected
  (`TOKEN_AT_CONCURRENT_MATCH_CAP`) — you simply won't get a `matched` notification for the
  over-cap match. The slot is released when a match finalizes.
- **Per-account free-tier caps:** matches/lobby-hours are metered per account; exceeding a
  free-tier limit also rejects dispatch.

### 8.5 Dispatch rejection codes (no `matched` arrives)

These are surfaced to whoever initiated the match (challenge/tournament entry), not pushed to
your bot — but they explain why a `matched` notification may not arrive:

| Code | Cause |
|---|---|
| `EXT_BOT_OFFLINE` | Your bot had no live lobby presence at dispatch time — keep the lobby connected |
| `TOKEN_AT_CONCURRENT_MATCH_CAP` | The token is at its concurrent-match cap |
| free-tier rejection | A per-account free-tier limit was exceeded |

### 8.6 Reconnect (mid-match)

Reconnect is an **executor** capability (advertised as `reconnect` in the match `hello`'s
`capabilities`). Because each match leg is a fresh WS to the executor bot route, recovering from
a mid-match drop is straightforward:

1. Your match WS drops mid-match.
2. Within the executor's reconnect grace window (default 30s), **re-dial the same
   `gateway_ws_url`** with your token and re-run the match handshake
   (`authenticate` → server `hello` → client `hello`).
3. The executor swaps the dead socket for your new one and **re-emits the in-flight
   `turn_request`** if it was your turn. Continue playing.

Per-match reconnection budget is 3 (per the transport spec). There is **no** ExtAPI-specific
reconnect machinery — it's the same primitive bot-vs-bot uses. The lobby socket and the match
socket are independent: a match WS drop does not drop your lobby presence, and vice-versa.

---

## 9. SDK home / productionizing

The published **`chipzen-bot` Python SDK (0.3.0+)** packages this whole path:
`run_external_bot()` plus the `chipzen run-external` CLI run the lobby → `matched` → match
handshake → play loop for you, reusing the same `Bot.decide(GameState) -> Action` interface as
the sandboxed/uploaded path. For production, that's the recommended client:

```bash
pip install chipzen-bot
```

```python
import asyncio
from chipzen import Bot, run_external_bot

asyncio.run(run_external_bot(MyBot(), bot_id="<bot-uuid>", env="staging", token="cz_extbot_..."))
```

A minimal, runnable **reference client** also lives in this repo at
[`examples/external-api-bot/`](../examples/external-api-bot/). It demonstrates the same path with
a trivial check/call/fold strategy, speaking raw JSON over WebSockets so every frame is visible —
the readable starting point for learning the wire format or porting the protocol to another
language. The JavaScript and Rust SDKs do not package this path yet; for those, the reference
client remains the canonical guide.
