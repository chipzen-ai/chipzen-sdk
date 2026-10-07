# Chipzen Transport Protocol Specification

**Version:** 1.0.0-draft
**Status:** Draft
**Date:** 2026-04-13
**Frames last checked against the executor:** 2026-10-07 (chipzen-ai/Chipzen#5573)

> **The frame examples and schemas in this document are checked against the server code.** A parity test in the platform repository compares the keys of every server frame example and Appendix A schema with the frames the match server builds, so an example that does not match what the server sends fails CI. Where this document and the server disagree, the server is right.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Conventions](#3-conventions)
4. [Connection Endpoints](#4-connection-endpoints)
5. [Connection Lifecycle](#5-connection-lifecycle)
6. [Server State Machine](#6-server-state-machine)
7. [Message Envelope](#7-message-envelope)
8. [Server-to-Bot Messages](#8-server-to-bot-messages)
9. [Bot-to-Server Messages](#9-bot-to-server-messages)
10. [Timing and Timeouts](#10-timing-and-timeouts)
11. [Reconnection](#11-reconnection)
12. [Error Handling](#12-error-handling)
13. [Rate Limiting](#13-rate-limiting)
14. [Security](#14-security)
15. [WebSocket Close Codes](#15-websocket-close-codes)
16. [Extensibility and Forward Compatibility](#16-extensibility-and-forward-compatibility)
17. [Quick-Start Example](#17-quick-start-example)

---

## 1. Overview

The Chipzen Transport Protocol is the game-agnostic communication layer between the Chipzen match server and participant clients (bots or human-facing frontends). It handles connection establishment, authentication, turn-taking, timing, error recovery, session management, and audit requirements.

This document specifies **Layer 1** of a two-layer protocol:

- **Layer 1 (this document):** Transport Protocol -- game-agnostic connection lifecycle, authentication, turn sequencing, timing, and session management.
- **Layer 2 (separate document):** Game State Protocol -- per-game-type definitions of what appears inside `state`, `game_config`, `result`, `details`, and `params` payloads.

Layer 1 treats all game state objects as opaque. A bot framework implements Layer 1 once and it works for any game type Chipzen supports.

---

## 2. Architecture

Communication uses WebSocket (RFC 6455). All messages are UTF-8 encoded JSON objects. Binary frames are not used and must be rejected.

The protocol follows a strict request-response pattern for actions: the server sends a `turn_request`, the bot responds with a `turn_action`. The server never accepts actions outside this cycle.

### 2.1 Roles

- **Server:** The Chipzen match server. Authoritative for all game state, timing, and validation.
- **Bot:** A participant client. Receives game state, submits actions when prompted.

### 2.2 Message Direction

| Direction | Messages |
|-----------|----------|
| Server to Bot | `hello`, `match_start`, `round_start`, `turn_request`, `turn_result`, `phase_change`, `round_result`, `match_end`, `action_rejected`, `error` |
| Bot to Server | `authenticate`, `hello`, `turn_action` |

The match executor sends no other frame type to a bot. `session_token`, `action_timeout`, `session_control`, `ping` and `reconnected` were in earlier drafts of this document but are not sent; sections 8.2 and 8.12-8.15 say what happens instead. Bots must still ignore frame types they do not recognise (section 16.1).

---

## 3. Conventions

### 3.1 Timestamps

All server messages include a `server_ts` field containing an ISO 8601 timestamp with millisecond precision and UTC timezone designator.

Format: `YYYY-MM-DDTHH:mm:ss.sssZ`

Example: `"2026-04-13T14:30:05.123Z"`

### 3.2 Sequence Numbers

All server messages include a `seq` field, a monotonically increasing integer. The executor keeps two counters per seat:

- The server `hello` has its own handshake counter, so it always carries `seq: 1`.
- Game frames have a per-seat counter that starts at 1 with `match_start` and goes up by one on every frame that seat receives. A reconnecting seat keeps its counter, so numbering continues where the dropped connection left off.

An `error` frame sent while rejecting a connection (section 8.10) is built on a fresh counter and carries `seq: 1`.

### 3.3 Match Identifiers

All messages (both directions) include a `match_id` field. This enables future multi-table multiplexing over a single WebSocket connection.

### 3.4 Field Naming

All field names use `snake_case`. All string enumerations use `snake_case`.

### 3.5 Maximum Message Size

Server messages have no fixed size limit. Bot-to-server messages must not exceed **4096 bytes**. The server will close the connection with code 4008 if a bot message exceeds this limit.

---

## 4. Connection Endpoints

### 4.1 Production (wss:// only)

| Endpoint | Purpose |
|----------|---------|
| `wss://<host>/ws/match/{match_id}/{participant_id}` | Competitive match entry |
| `wss://<host>/ws/match/{match_id}/bot` | Internal bot entry (authenticated) |
| `wss://<host>/ws/reconnect/{match_id}/{participant_id}` | Reconnection to an active match |

Authentication credentials are **not** sent as URL query parameters. Instead, the bot must send an `authenticate` message as its first message after WebSocket upgrade (see section 9.4).

### 4.2 Development Only (ws://)

| Endpoint | Purpose |
|----------|---------|
| `ws://localhost:<port>/ws/match/{match_id}/{participant_id}` | Local development |
| `ws://localhost:<port>/ws/match/{match_id}/bot` | Local bot testing |

Unencrypted `ws://` connections are permitted only on `localhost` in development environments. Production deployments must reject `ws://` connections.

### 4.3 Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `match_id` | string | UUID v4 identifying the match |
| `participant_id` | string | Stable, opaque identifier for the participant |

### 4.4 Authentication Flow

Authentication credentials are sent as the **first message** after WebSocket upgrade, not in the URL. The bot sends an `authenticate` message (see section 9.4) containing either a `ticket` (competitive endpoint) or `token` (bot endpoint). The server validates the credential before sending `hello`. If validation fails, the server closes the connection with code 4001 (`auth_failed`).

### 4.5 Bot Endpoint Authentication

The `/bot` endpoint requires authentication via either:
- An `authenticate` message containing a valid bot API `token`, or
- Network-level isolation (e.g., VPC-internal only) when running in a sidecar configuration.

Unauthenticated access to the `/bot` endpoint is not permitted in any environment.

---

## 5. Connection Lifecycle

```
Bot                                Server
 |                                    |
 |-------- WS Connect --------------->|
 |                                    |
 |------------ authenticate --------->|  (1) Bot sends ticket or token
 |                                    |      Server validates credential
 |<----------- hello -----------------|  (2) Server hello
 |------------ hello ---------------->|  (3) Bot hello
 |                                    |
 |<------- match_start ---------------|  (4) Match begins
 |                                    |
 |<------- round_start ---------------|  (5) Round begins
 |<------- turn_request --------------|  (6) Bot's turn
 |------------ turn_action ---------->|  (7) Bot acts
 |<------- turn_result ---------------|  (8) Result broadcast
 |<------- phase_change --------------|      (when the phase advances)
 |          ... more turns ...        |
 |<------- round_result --------------|  (9) Round ends
 |          ... more rounds ...       |
 |                                    |
 |<------- match_end -----------------|  (10) Match ends
 |                                    |
 |-------- WS Close ----------------->|
```

### 5.1 Handshake Sequence

1. Bot opens a WebSocket connection to the appropriate endpoint.
2. Bot sends an `authenticate` message containing a `ticket` or `token` within 5000ms of connection.
3. Server validates the credential. If invalid, the server closes the connection with code 4001 (`auth_failed`).
4. Server sends a `hello` message containing its `supported_versions` and `selected_version` (see section 16.3).
5. Bot must respond with a `hello` message within 5000ms.
6. If no mutually supported protocol version exists, server closes with code 4013.
7. No game messages are sent until the handshake is complete. The next frame is `match_start`; no session token is issued.

### 5.2 Connection Wait

After the handshake, the server waits up to **30000ms** (configurable) for all participants to connect before starting the match. If a participant fails to connect within this window, the match is cancelled and all connected participants are notified via `error`.

---

## 6. Server State Machine

The server tracks each participant's state independently:

```
[disconnected] --connect--> [handshaking] --authenticate--> [authenticating]
[authenticating] --valid--> [handshaking] --hello--> [connected]
[authenticating] --invalid--> [closed]
[connected] --match_start--> [in_match]
[in_match] --turn_request--> [awaiting_action]
[awaiting_action] --turn_action--> [in_match]
[awaiting_action] --timeout--> [in_match]
[in_match] --match_end--> [complete]
[in_match] --session_control:pause--> [paused]
[awaiting_action] --session_control:pause--> [paused]
[paused] --session_control:resume--> [previous state]
[any] --disconnect--> [disconnected]
[disconnected] --reconnect--> [reconnecting]
[reconnecting] --hello--> [in_match] or [awaiting_action]
```

**The server only accepts `turn_action` messages when the participant is in the `awaiting_action` state.** Any `turn_action` received in another state is silently dropped.

**While in the `paused` state**, turn timeouts are suspended and no `turn_request` messages are sent. When the server sends `session_control` with action `resume`, the participant returns to their previous state.

> **The `paused` state is not reached today.** The match server has a pause/resume primitive that would send `session_control`, but nothing calls it (section 8.13).

---

## 7. Message Envelope

Every message conforms to a common envelope structure.

### 7.1 Server Message Envelope

```json
{
  "type": "<message_type>",
  "match_id": "<uuid>",
  "seq": 1,
  "server_ts": "2026-04-13T14:30:05.123Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | Message type identifier |
| `match_id` | string | yes | UUID v4 of the match |
| `seq` | integer | yes | Monotonically increasing sequence number (starts at 1 per connection) |
| `server_ts` | string | yes | ISO 8601 timestamp with millisecond precision (UTC) |

All server message schemas use `additionalProperties: true` to allow non-breaking additions.

### 7.2 Bot Message Envelope

```json
{
  "type": "<message_type>",
  "match_id": "<uuid>"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | Message type identifier |
| `match_id` | string | yes | UUID v4 of the match |

All bot message schemas use `additionalProperties: false`. Unknown fields in bot messages are rejected.

---

## 8. Server-to-Bot Messages

### 8.1 `hello`

Sent after the server has received the bot's `authenticate` message. It is the first server message on the connection.

```json
{
  "type": "hello",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 1,
  "server_ts": "2026-04-13T14:30:05.123Z",
  "supported_versions": ["1.0"],
  "selected_version": "1.0",
  "server_name": "chipzen",
  "game_type": "poker",
  "capabilities": ["reconnect"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `supported_versions` | string[] | yes | Protocol versions supported by the server (major.minor format). Currently `["1.0"]`. |
| `selected_version` | string | yes | The server's preferred version (the first entry of `supported_versions`). It is sent before the bot's `hello`, so it is not negotiated; the server checks for an overlap when the bot's `hello` arrives and closes with code 4013 if there is none (section 16.3). |
| `server_name` | string | yes | Always `"chipzen"`. |
| `game_type` | string | yes | The game this seat is playing. `"poker"` for NLHE. |
| `capabilities` | string[] | yes | Server capabilities. The server sends `["reconnect"]` while reconnection is enabled, and `[]` when it has been switched off. |
| `game` | object | no | Only at a non-poker table: the game's action vocabulary, phase sequence and state-shape marker (chipzen-ai/Chipzen#4245). Absent for poker, so an NLHE `hello` never carries it. |

There is no `server_id` field.

### 8.2 `session_token`

**Not sent.** The match executor never sends a `session_token` frame, and no session token is issued at the handshake or on reconnect. Nothing in the handshake depends on one: after the bot's `hello` the next frame is `match_start`.

### 8.3 `match_start`

Announces the beginning of a match. Sent to all participants after all have connected and completed handshake.

```json
{
  "type": "match_start",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 1,
  "server_ts": "2026-04-13T14:30:06.000Z",
  "seats": [
    {
      "seat": 0,
      "display_name": "AlphaBot",
      "participant_id": "p_abc123",
      "is_self": false
    },
    {
      "seat": 1,
      "display_name": "MyBot",
      "participant_id": "p_def456",
      "is_self": true
    }
  ],
  "game_config": {
    "_comment": "Game-specific configuration -- see Layer 2 spec"
  },
  "turn_timeout_ms": 5000,
  "your_seat": 1
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `seats` | object[] | yes | Array of seat assignments |
| `seats[].seat` | integer | yes | Zero-indexed seat number |
| `seats[].display_name` | string | yes | The participant's platform name (for a bot, the bot's name) |
| `seats[].participant_id` | string | yes | Stable, opaque participant identifier |
| `seats[].is_self` | boolean | yes | `true` only on the receiving client's own seat |
| `game_config` | object | yes | Game-specific configuration (opaque to Layer 1) |
| `turn_timeout_ms` | integer | yes | Default action timeout in milliseconds |
| `your_seat` | integer | yes | The receiving client's seat. Kept for older clients; it always equals the `seat` of the `is_self` entry. |

> **Note:** Layer 1 does not impose a round count — how a match ends is game-specific. For poker it is elimination: the match runs until a seat busts, and the per-match hand cap is deliberately **not** exposed in `game_config` so bots cannot condition strategy on a remaining-hands count (chipzen-ai/Chipzen#1588).

### 8.4 `round_start`

Signals the beginning of a new round.

```json
{
  "type": "round_start",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 2,
  "server_ts": "2026-04-13T14:30:07.000Z",
  "round_id": "r_f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "round_number": 1,
  "state": {
    "_comment": "Game-specific state -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `round_id` | string | yes | Globally unique identifier for this round/hand, for cross-system audit reference: `r_` followed by a UUID |
| `round_number` | integer | yes | One-indexed round number within the match |
| `state` | object | yes | Game-specific round state (opaque to Layer 1) |

> **Note:** Game-specific Layer 2 protocols MAY include cryptographic verification fields (e.g., deck hash commitment in `round_start.state`) for RNG verifiability.

### 8.5 `turn_request`

Requests an action from the bot. The bot must respond with a `turn_action` before the timeout expires.

```json
{
  "type": "turn_request",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 3,
  "server_ts": "2026-04-13T14:30:07.500Z",
  "seat": 1,
  "request_id": "req_4621ff557b22",
  "timeout_ms": 5000,
  "turn_duration_ms": 5000,
  "deadline_ts": 1776090612500,
  "valid_actions": ["fold", "call", "raise"],
  "state": {
    "_comment": "Game-specific state -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `seat` | integer | yes | The seat index of the participant whose turn it is (always the receiving client's seat) |
| `request_id` | string | yes | Unique identifier for this turn request (`req_` plus 12 hex characters). Must be echoed in the response. Used for correlation, idempotency, and deduplication. |
| `timeout_ms` | integer | yes | Time allowed to submit an action, in milliseconds |
| `turn_duration_ms` | integer | yes | The same window as `timeout_ms`, for a client to start a countdown of that length when the frame arrives (chipzen-ai/Chipzen#2581) |
| `deadline_ts` | integer | yes | The server's enforcement deadline as Unix epoch milliseconds (chipzen-ai/Chipzen#2562). For a bot it is send time plus `timeout_ms`. |
| `valid_actions` | string[] | yes | List of valid action type strings for this turn |
| `state` | object | yes | Current game-specific state (opaque to Layer 1) |

### 8.6 `turn_result`

Sent to every participant after an action is applied, whoever acted. The acting seat receives it at once; the other seats receive it after a random 100-500ms jitter, so they cannot infer the actor's computation time (section 10.3).

```json
{
  "type": "turn_result",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 5,
  "server_ts": "2026-04-13T14:30:08.350Z",
  "seat": 1,
  "details": {
    "action": "raise",
    "_comment": "Game-specific action details -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `seat` | integer | yes | Seat number of the participant who acted |
| `details` | object | yes | Game-specific action details (opaque to Layer 1). The action string (e.g., `"raise"`, `"fold"`) is carried inside this object as defined by the Layer 2 protocol. |

There is no top-level `is_timeout`. Whether the server substituted the action (timeout, disconnect or unparseable reply) is `details.is_timeout`, which is always present; see the Layer 2 spec.

### 8.7 `phase_change`

Indicates the game phase advanced within a round (e.g., dealing community cards in poker).

```json
{
  "type": "phase_change",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 10,
  "server_ts": "2026-04-13T14:30:09.100Z",
  "state": {
    "phase": "turn",
    "_comment": "Game-specific state for the new phase -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `state` | object | yes | Updated game-specific state (opaque to Layer 1). The phase identifier (e.g., `"preflop"`, `"flop"`, `"turn"`, `"river"`) is carried inside this object as defined by the Layer 2 protocol. |

### 8.8 `round_result`

Sent at the conclusion of a round. Contains the outcome for the round.

```json
{
  "type": "round_result",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 15,
  "server_ts": "2026-04-13T14:30:12.000Z",
  "round_id": "r_f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "round_number": 1,
  "result": {
    "_comment": "Game-specific round result -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `round_id` | string | yes | The same `r_`-prefixed identifier as the round's `round_start` |
| `round_number` | integer | yes | One-indexed round number |
| `result` | object | yes | Game-specific result (opaque to Layer 1) |

> **Note:** The complete action history for the round is contained in the `result` payload, defined by the game-specific Layer 2 protocol.

> **Note:** Game-specific Layer 2 protocols MAY include cryptographic verification fields (e.g., deck seed reveal in `round_result.result`) for RNG verifiability. Poker withholds the reveal from participants (chipzen-ai/Chipzen#3575); see the Layer 2 spec.

The server retains all dealt information (e.g., all player cards) even if not exposed to all clients during play. This data is available through administrative and audit APIs.

### 8.9 `match_end`

Signals the end of a match. When the match is played out, every seat receives the frame below.

```json
{
  "type": "match_end",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 50,
  "server_ts": "2026-04-13T14:35:00.000Z",
  "reason": "complete",
  "results": [
    {
      "seat": 0,
      "name": "AlphaBot",
      "net_chips": -10000,
      "hands_won": 21,
      "final_stack": 0,
      "bot_errors": [],
      "decision_latency_samples_ms": [12.4, 9.8, 15.1]
    },
    {
      "seat": 1,
      "name": "MyBot",
      "net_chips": 10000,
      "hands_won": 27,
      "final_stack": 20000,
      "bot_errors": [],
      "decision_latency_samples_ms": [3.2, 4.0, 2.9]
    }
  ],
  "total_hands_played": 48,
  "mode": "elimination",
  "finishing_order": [
    {"place": 1, "seat": 1, "name": "MyBot"},
    {"place": 2, "seat": 0, "name": "AlphaBot"}
  ],
  "terminal_status": "completed"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | string | yes | `"complete"` on this path, however the match finished. Read `terminal_status` and the conditional keys below for how it finished. |
| `results` | object[] | yes | One entry per seat, in seat order |
| `results[].seat` | integer | yes | Seat number |
| `results[].name` | string | yes | The participant's display name |
| `results[].net_chips` | integer | yes | Chips won (positive) or lost (negative) over the match |
| `results[].hands_won` | integer | yes | Hands this seat won |
| `results[].final_stack` | integer | yes | Chips at the end of the match |
| `results[].bot_errors` | object[] | yes | Errors recorded against this seat (chipzen-ai/Chipzen#1082). Empty when the seat played cleanly. |
| `results[].decision_latency_samples_ms` | number[] | yes | The seat's decision round-trip times in milliseconds (chipzen-ai/Chipzen#2218). Empty if it made no decision. |
| `total_hands_played` | integer | yes | Hands played in the match |
| `mode` | string | yes | Always `"elimination"` (chipzen-ai/Chipzen#1588) |
| `finishing_order` | object[] | yes | `{"place", "seat", "name"}` per seat; place 1 is the last seat standing |
| `terminal_status` | string | yes | `"completed"`, `"error"` or `"abandoned"` |

These keys appear only when they apply:

| Field | When |
|-------|------|
| `forfeit_seat`, `forfeit_winner_seat`, `forfeit_reason` | One seat stopped responding and the other was awarded the match (chipzen-ai/Chipzen#3341). `terminal_status` stays `"completed"`. |
| `error_code`, `auto_substitute_limit_seat`, `error_message` | A seat stopped responding and there was no responsive opponent to award the match to (chipzen-ai/Chipzen#1682). |
| `all_disconnected`, `error_message` (and `simultaneous_disconnect` when both seats dropped together) | Every seat was disconnected for too long. |
| `abandoned`, `abandoned_seat` | A human seat walked away (chipzen-ai/Chipzen#2561). |
| `safety_limit_reached` | The match hit the internal hand-count safety cap instead of ending by elimination. |

**Matches that never finish.** If the match cannot be played out, the executor sends every connected seat a shorter `match_end` with no `results`, built on one counter for the whole match rather than per seat:

| `reason` | Extra fields | When |
|----------|--------------|------|
| `cancelled` | `error_code: "connection_timeout"` | Not every participant connected in time (section 5.2) |
| `error` | `error_code: "server_error"`, `error_message` | The match crashed on the server |

An External-API bot connected through the gateway may also receive `{"reason": <close reason>, "source": "gateway"}` as its final `match_end` when the executor connection closes first (see `docs/EXTERNAL-API-BOT-PROTOCOL.md` §6.3).

### 8.10 `error`

General error notification.

```json
{
  "type": "error",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 12,
  "server_ts": "2026-04-13T14:30:10.000Z",
  "code": "match_cancelled",
  "message": "Opponent failed to connect within the allowed time."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | yes | Machine-readable error code |
| `message` | string | yes | Human-readable description |
| `game_type` | string | no | Only with `code` `EXTAPI_CLIENT_GAME_UNSUPPORTED`: the game the seat belongs to (chipzen-ai/Chipzen#4245) |

The match executor sends `error` just before it closes a connection: a second connection to a live seat or a refused reconnect (`code` such as `duplicate_participant`, `grace_expired` or `budget_exhausted`, then close 4011), a client that did not declare the seat's game (`code` `EXTAPI_CLIENT_GAME_UNSUPPORTED`, then close 4002), and an oversize inbound frame (`code` `message_too_large`, then close 4008). These frames are built on a fresh counter and carry `seq: 1`.

### 8.11 `action_rejected`

The submitted action failed validation. The bot receives another chance to submit a valid action within the remaining timeout.

```json
{
  "type": "action_rejected",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 8,
  "server_ts": "2026-04-13T14:30:08.100Z",
  "request_id": "req_4621ff557b22",
  "reason": "Action 'bet' is not valid. Valid actions: ['fold', 'call', 'raise']",
  "message": "Action 'bet' is not valid. Valid actions: ['fold', 'call', 'raise']",
  "details": {
    "valid_actions": ["fold", "call", "raise"]
  },
  "submitted_action": "bet",
  "remaining_ms": 3200,
  "valid_actions": ["fold", "call", "raise"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | The `request_id` from the original `turn_request` |
| `reason` | string | yes | Why the action was rejected. Currently the same human-readable text as `message`, not a fixed code, so do not match on its exact wording. |
| `message` | string | yes | Human-readable explanation |
| `details` | object | yes | Structured context from the validator, e.g. `valid_actions` for an action that is not legal. Its keys depend on the rejection. |
| `submitted_action` | string | yes | The `action` string from the rejected `turn_action` (`""` if it had none). Not an object. |
| `remaining_ms` | integer | yes | Milliseconds remaining before timeout auto-action |
| `valid_actions` | array of string | yes | The seat's currently legal action types (same set sent in the originating `turn_request`). Use it to pick a legal retry. Servers before v0.3.53 did not send it; a client that must support them MAY fall back to `["check", "fold"]`, one of which the auto-action policy guarantees is legal. |

The participant remains in the `awaiting_action` state. The original `request_id` is still valid. The bot should submit a corrected `turn_action` with the same `request_id`.

### 8.12 `action_timeout`

**Not sent.** When a turn's time runs out the server applies the auto-action (`check` if it is legal, otherwise `fold`) and reports it in the ordinary `turn_result`, with `details.is_timeout: true`. The same happens when a bot disconnects mid-turn or sends a reply that is not valid JSON. No separate timeout frame is sent.

### 8.13 `session_control`

**Not sent by the match executor.** The match server has a pause/resume primitive that would send `session_control` (`action`, `reason`, `message`) to every seat, but nothing calls it, so a bot never receives this frame today. A bot should still ignore it if one ever arrives (section 16.1).

### 8.14 `ping`

**Not sent to bots.** The application-level participant heartbeat was removed in chipzen-ai/Chipzen#1641 because its `recv` raced the action loop. Connection liveness is the WebSocket protocol's own ping/pong, which the executor runs at the transport level (a ping every 30 seconds, dropped after 10 seconds without a pong); WebSocket libraries answer those control frames automatically. The match executor sends a `ping` text frame only to spectator connections.

### 8.15 `reconnected`

**Not sent by the match executor.** A seat that may reconnect (the server `hello` lists `reconnect` in `capabilities`) reconnects on the same match endpoint and runs the full handshake again (`authenticate`, server `hello`, bot `hello`). The executor then attaches the new connection to the seat, and the seat simply receives the next game frames, with `seq` continuing from the dropped connection. If the seat dropped while one of its turns was pending and the server held that turn open, the same `turn_request` (same `request_id`) is sent again; otherwise the turn has already been auto-acted (section 8.12). A refused reconnect gets an `error` frame and close 4011 (section 8.10).

---

## 9. Bot-to-Server Messages

### 9.1 `hello`

Bot's response to the server `hello`. Must be sent within 5000ms of receiving the server `hello`.

```json
{
  "type": "hello",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "supported_versions": ["1.0"],
  "client_name": "MyPokerBot",
  "client_version": "2.1.0"
}
```

**JSON Schema:**

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "hello" },
    "match_id": { "type": "string", "format": "uuid" },
    "supported_versions": {
      "type": "array",
      "items": { "type": "string", "pattern": "^\\d+\\.\\d+$" },
      "minItems": 1
    },
    "client_name": { "type": "string", "maxLength": 64 },
    "client_version": { "type": "string", "maxLength": 32 }
  },
  "required": ["type", "match_id", "supported_versions"],
  "additionalProperties": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `supported_versions` | string[] | yes | Protocol versions the client supports (major.minor format). The server selects the highest mutually supported version. |
| `client_name` | string | no | Client software name |
| `client_version` | string | no | Client software version |

### 9.2 `turn_action`

Bot's response to a `turn_request`.

```json
{
  "type": "turn_action",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "request_id": "req_x7y8z9",
  "action": "raise",
  "params": {
    "_comment": "Game-specific action parameters -- see Layer 2 spec"
  }
}
```

**JSON Schema:**

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "turn_action" },
    "match_id": { "type": "string", "format": "uuid" },
    "request_id": { "type": "string" },
    "action": { "type": "string" },
    "params": { "type": "object" }
  },
  "required": ["type", "match_id", "request_id", "action"],
  "additionalProperties": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | Must echo the `request_id` from the corresponding `turn_request` |
| `action` | string | yes | One of the strings from `valid_actions` in the `turn_request` |
| `params` | object | no | Game-specific action parameters (opaque to Layer 1) |

### 9.3 `pong`

Response to a server `ping`. The match executor does not send `ping` to bots (section 8.14), so a bot never needs to send `pong`. Do not send one unprompted: while a turn is pending, the next frame a bot sends is read as its `turn_action`.

```json
{
  "type": "pong",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

**JSON Schema:**

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "pong" },
    "match_id": { "type": "string", "format": "uuid" }
  },
  "required": ["type", "match_id"],
  "additionalProperties": false
}
```

### 9.4 `authenticate`

First message sent by the bot after WebSocket upgrade. Must be sent within 5000ms of connection. The server validates the credential before proceeding with the handshake.

```json
{
  "type": "authenticate",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ticket": "tk_one_time_use_ticket_value"
}
```

Or for bot endpoints:

```json
{
  "type": "authenticate",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "token": "bot_api_token_value"
}
```

**JSON Schema:**

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "authenticate" },
    "match_id": { "type": "string", "format": "uuid" },
    "ticket": { "type": "string" },
    "token": { "type": "string" }
  },
  "required": ["type", "match_id"],
  "additionalProperties": false,
  "oneOf": [
    { "required": ["ticket"] },
    { "required": ["token"] }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ticket` | string | conditional | One-time-use connection ticket, obtained from the matchmaking API. Required for competitive endpoints. |
| `token` | string | conditional | Long-lived authentication token for internal bot endpoints. Required for bot endpoints. |

Exactly one of `ticket` or `token` must be provided. If neither or both are present, the server closes with code 4001.

---

## 10. Timing and Timeouts

### 10.1 Default Values

| Parameter | Default | Configurable | Description |
|-----------|---------|--------------|-------------|
| Turn timeout | 5000ms | Per competition | Time allowed for a `turn_action` response |
| Connection wait | 30000ms | Per competition | Time to wait for all participants to connect |
| Handshake timeout | 5000ms | No | Time for bot to send `hello` after receiving server `hello` |
| WebSocket ping interval | 30000ms | No | Transport-level WebSocket ping. There is no application-level `ping` frame (section 8.14). |
| WebSocket ping timeout | 10000ms | No | Time for the client's WebSocket library to answer that ping before the connection is dropped |
| Reconnection grace | 30000ms | Per competition | Time allowed for a disconnected client to reconnect |

### 10.2 Turn Timeout Behavior

1. Server sends `turn_request` with `timeout_ms`.
2. Timer starts on the server when the message is sent (not when the bot receives it).
3. If the bot sends an invalid action, the server sends `action_rejected` with `remaining_ms`. The bot may retry within the remaining time.
4. If the timer expires before a valid action is received, the server applies the auto-action (check if legal, otherwise fold) and reports it in `turn_result` with `details.is_timeout: true`. No `action_timeout` frame is sent (section 8.12).

### 10.3 Action Delivery Jitter

After a valid action is processed, the server sends `turn_result` to the acting seat at once, then waits a random duration of 100-500ms (uniformly distributed) before sending it to the other participants. This prevents opponents from inferring computation time from network timing.

### 10.4 Server-Side Receive Timestamp

When the server receives a `turn_action`, it records a `server_received_ts` timestamp in its audit log. This timestamp is **not** sent to the client but is available for dispute investigation to distinguish network latency from late sends.

---

## 11. Reconnection

### 11.1 Reconnection Flow

Reconnecting is only possible when the server `hello` listed `reconnect` in `capabilities`.

1. Client detects disconnection.
2. Client connects again to the same match endpoint it first connected to.
3. Bot sends `authenticate`; server sends `hello`; bot sends `hello` (the normal handshake, section 5.1).
4. Server attaches the new connection to the seat. No `reconnected` frame is sent (section 8.15).
5. If the seat's turn was still being held open, the same `turn_request` (same `request_id`) is sent again.
6. Play resumes with the next game frame. A refused reconnect gets an `error` frame and close 4011.

### 11.2 Reconnection Rules

- Rate limiting counters are tracked per `participant_id`, not per connection. Counters survive reconnection.
- The reconnection grace period starts when the server detects the disconnect. If the grace period expires, the participant forfeits.
- Sequence numbers continue from the last value on the previous connection.
- During the grace period, if it is the disconnected participant's turn, the turn timeout is paused only on the **first** reconnection per match. Subsequent reconnections do not pause the timer.

### 11.3 Reconnection Budget

Each participant has a per-match reconnection budget of **3 reconnections**. After the budget is exhausted, further disconnections result in immediate forfeit.

---

## 12. Error Handling

### 12.1 Invalid Action

When a bot submits an action that fails validation:

1. Server sends `action_rejected` with the reason and remaining time.
2. The participant remains in `awaiting_action` state.
3. The bot should submit a corrected action using the same `request_id`.
4. If time expires, the auto-action policy applies.

### 12.2 Unsolicited Action

A `turn_action` received when the participant is not in `awaiting_action` state is silently dropped. No error is sent.

### 12.3 Malformed Messages

If a bot message fails JSON parsing or schema validation, the server sends an `error` message with code `malformed_message`. Repeated malformed messages may trigger rate limiting.

> **Match executor behaviour:** a reply to a `turn_request` that is not valid JSON is not answered with an `error` frame. The server applies the auto-action and reports it in `turn_result` with `details.is_timeout: true`.

### 12.4 Disconnection

1. Server detects WebSocket close or transport failure.
2. Reconnection grace period begins.
3. If the disconnected participant's turn is active, the turn timeout is paused.
4. If the grace period expires without reconnection, the participant forfeits all remaining rounds.
5. Other participants are notified of the forfeit and the match continues or ends accordingly.

---

## 13. Rate Limiting

Rate limiting is tracked per `participant_id` (not per connection) and survives reconnection.

### 13.1 Limits

| Metric | Limit | Window |
|--------|-------|--------|
| Messages per second | 10 | Rolling 1s |
| Invalid actions per round | 5 | Per round |

### 13.2 Enforcement

1. **Violations 1-4:** Server sends an `error` message with code `rate_limited` and a human-readable warning.
2. **Violation 5:** Server closes the connection with close code 4009 (`rate_limit_exceeded`).

> **Match executor behaviour:** during a match the executor sends no `rate_limited` error. A turn reply that arrives over the per-second limit is dropped and the auto-action is applied instead.

---

## 14. Security

### 14.1 Transport Security

All production connections must use `wss://` (WebSocket over TLS). The server must reject unencrypted `ws://` connections in production environments. Unencrypted connections are permitted only on `localhost` for development.

### 14.2 Authentication

- **Competitive endpoint:** Authenticated via a one-time-use `ticket` sent in the `authenticate` message (first message after WebSocket upgrade). Tickets expire after a single use or after a short TTL (configurable, default 60s).
- **Bot endpoint:** Authenticated via a `token` sent in the `authenticate` message, or network-level isolation. Tokens must have minimum 32 bytes of cryptographically random entropy.
- Credentials are never sent as URL query parameters to avoid logging in server access logs, proxy logs, and browser history.

### 14.3 Session Tokens

The match executor issues no session tokens; there is no `session_token` frame (section 8.2).

### 14.4 Timing Side-Channel Mitigation

The server adds uniformly random jitter of 100-500ms before sending `turn_result` to every seat except the one that acted. This prevents participants from inferring an opponent's computation time from message delivery timing.

### 14.5 Information Isolation

Each participant receives only the game state they are entitled to see. The server retains complete game state (including hidden information such as opponent cards) for audit and regulatory purposes, but does not expose it through the transport protocol during play.

---

## 15. WebSocket Close Codes

| Code | Name | Description |
|------|------|-------------|
| 1000 | Normal Closure | Clean shutdown (match complete or client departing) |
| 1001 | Going Away | Server shutting down |
| 4001 | `auth_failed` | Authentication failed (invalid ticket or token) |
| 4002 | `match_not_found` | The specified match does not exist |
| 4003 | `match_full` | All seats in the match are occupied |
| 4004 | `match_started` | Match already in progress, reconnect required |
| 4005 | `match_ended` | Match has already concluded |
| 4006 | `participant_not_found` | Participant ID not recognized for this match |
| 4007 | `handshake_timeout` | Bot did not send `hello` within the required time |
| 4008 | `message_too_large` | Bot message exceeded the 4096-byte limit |
| 4009 | `rate_limit_exceeded` | Rate limit violated (5th violation) |
| 4010 | `reconnect_expired` | Reconnection grace period has elapsed |
| 4011 | `server_error` | Unrecoverable server-side error |
| 4012 | `responsible_gaming` | Connection closed due to a responsible gaming intervention |
| 4013 | `protocol_mismatch` | Incompatible protocol versions |

---

## 16. Extensibility and Forward Compatibility

### 16.1 Unknown Message Types

Bots **MUST** silently ignore any server message with an unrecognized `type`. The server may introduce new message types in minor protocol versions. Bots that reject unknown types will break on upgrades.

### 16.2 Unknown Fields

Bots **MUST** silently ignore any unrecognized fields in server messages. All server message schemas specify `additionalProperties: true`. This allows the server to add fields without breaking existing clients.

Bot messages use `additionalProperties: false`. The server rejects bot messages containing unknown fields, returning an `error` with code `unknown_field`.

### 16.3 Version Negotiation

The server and bot exchange `supported_versions` arrays in their `hello` messages. Each version string follows `major.minor` format. The server speaks first, so its `selected_version` is its own preferred version, not a negotiated one. When the bot's `hello` arrives the server checks for a mutually supported version and, if there is none, closes the connection with code 4013 (`protocol_mismatch`). The server currently supports only `1.0`.

- **Major version change:** Breaking changes. Server and bot must agree on the major version.
- **Minor version change:** Backward-compatible additions (new message types, new fields). A bot supporting version 1.0 can connect to a server running 1.3 without issues, as the server will select 1.0.

### 16.4 Reserved Namespaces

The `spectator_*` message type namespace is reserved for future spectator functionality. Implementations must not use message types beginning with `spectator_` for other purposes.

---

## 17. Quick-Start Example

A minimal bot session through one hand, with the frames the match executor sends. The bot is seat 0.

```
# Direction markers:  S→B = server to bot,  B→S = bot to server

# --- Authentication ---

B→S  {"type":"authenticate","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "token":"bot_api_token_value"}

# --- Handshake ---

S→B  {"type":"hello","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":1,
      "server_ts":"2026-04-13T14:30:05.000Z",
      "supported_versions":["1.0"],"selected_version":"1.0",
      "server_name":"chipzen","game_type":"poker","capabilities":["reconnect"]}

B→S  {"type":"hello","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "supported_versions":["1.0"],"client_name":"ExampleBot","client_version":"0.1.0"}

# --- Match starts (game frames count seq from 1 again) ---

S→B  {"type":"match_start","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":1,
      "server_ts":"2026-04-13T14:30:06.000Z",
      "seats":[
        {"seat":0,"display_name":"ExampleBot","participant_id":"p_def456","is_self":true},
        {"seat":1,"display_name":"OpponentBot","participant_id":"p_abc123","is_self":false}
      ],
      "game_config":{"variant":"nlhe","starting_stack":10000,"small_blind":50,
                     "big_blind":100,"ante":0,"num_players":2},
      "turn_timeout_ms":5000,"your_seat":0}

S→B  {"type":"round_start","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":2,
      "server_ts":"2026-04-13T14:30:07.000Z",
      "round_id":"r_f47ac10b-58cc-4372-a567-0e02b2c3d479","round_number":1,
      "state":{"hand_number":1,"dealer_seat":0,"your_hole_cards":["Ah","Kd"],"pot":150,
               "post_blind_stacks":[9950,9900],"stacks":[10000,10000],
               "deck_commitment":"94d9f436703c1dda135c2ba119bbe886c0998246232d7fb95c1aaf9146f95a30"}}

S→B  {"type":"turn_request","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":3,
      "server_ts":"2026-04-13T14:30:07.500Z","seat":0,
      "request_id":"req_4621ff557b22","timeout_ms":5000,"turn_duration_ms":5000,
      "deadline_ts":1776090612500,
      "valid_actions":["fold","call","raise"],
      "state":{"hand_number":1,"phase":"preflop","board":[],"your_hole_cards":["Ah","Kd"],
               "pot":150,"your_stack":9950,"opponent_stacks":[9900],"to_call":50,
               "min_raise":200,"max_raise":10000,
               "action_history":[
                 {"seat":0,"action":"post_small_blind","amount":50,"phase":"preflop","is_timeout":false},
                 {"seat":1,"action":"post_big_blind","amount":100,"phase":"preflop","is_timeout":false}
               ]}}

B→S  {"type":"turn_action","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "request_id":"req_4621ff557b22","action":"call"}

S→B  {"type":"turn_result","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":4,
      "server_ts":"2026-04-13T14:30:08.100Z","seat":0,
      "details":{"action":"call","amount":50,"pot":200,"stacks":[9900,9900],"is_timeout":false}}

S→B  {"type":"turn_result","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":5,
      "server_ts":"2026-04-13T14:30:08.900Z","seat":1,
      "details":{"action":"check","amount":0,"pot":200,"stacks":[9900,9900],"is_timeout":false}}

S→B  {"type":"phase_change","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":6,
      "server_ts":"2026-04-13T14:30:09.000Z",
      "state":{"phase":"flop","board":["Qs","7h","3d"]}}

# ... flop, turn and river are checked down ...

S→B  {"type":"round_result","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":19,
      "server_ts":"2026-04-13T14:30:20.000Z",
      "round_id":"r_f47ac10b-58cc-4372-a567-0e02b2c3d479","round_number":1,
      "result":{"hand_number":1,"winner_seats":[0],"pot":200,
                "payouts":[{"seat":0,"amount":200}],
                "showdown":[
                  {"seat":0,"hole_cards":["Ah","Kd"],"hand_rank":"High Card",
                   "best_hand":["Ah","Kd","Qs","9s","7h"]},
                  {"seat":1,"hole_cards":["Jd","Tc"],"hand_rank":"High Card",
                   "best_hand":["Qs","Jd","Tc","9s","7h"]}
                ],
                "action_history":[
                  {"seat":0,"action":"post_small_blind","amount":50,"phase":"preflop","is_timeout":false},
                  {"seat":1,"action":"post_big_blind","amount":100,"phase":"preflop","is_timeout":false},
                  {"seat":0,"action":"call","amount":50,"phase":"preflop","is_timeout":false},
                  {"seat":1,"action":"check","amount":0,"phase":"preflop","is_timeout":false}
                ],
                "stacks":[10100,9900],
                "deck_commitment":"94d9f436703c1dda135c2ba119bbe886c0998246232d7fb95c1aaf9146f95a30",
                "deck_reveal":null}}

# --- Match concludes when one seat has no chips left ---

S→B  {"type":"match_end","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":180,
      "server_ts":"2026-04-13T14:35:00.000Z","reason":"complete",
      "results":[
        {"seat":0,"name":"ExampleBot","net_chips":10000,"hands_won":14,"final_stack":20000,
         "bot_errors":[],"decision_latency_samples_ms":[3.2,4.0,2.9]},
        {"seat":1,"name":"OpponentBot","net_chips":-10000,"hands_won":11,"final_stack":0,
         "bot_errors":[],"decision_latency_samples_ms":[12.4,9.8,15.1]}
      ],
      "total_hands_played":25,"mode":"elimination",
      "finishing_order":[
        {"place":1,"seat":0,"name":"ExampleBot"},
        {"place":2,"seat":1,"name":"OpponentBot"}
      ],
      "terminal_status":"completed"}
```

The `action_history` in the `round_result` above is shortened to the preflop actions; the real one lists every action of the hand.

### 17.1 Handling Unknown Messages

Bots will encounter message types not listed in this specification as the protocol evolves. A conforming bot must ignore them:

```python
# Python pseudocode

# Step 1: Authenticate immediately after WebSocket upgrade
ws.send(json.dumps({
    "type": "authenticate",
    "match_id": MATCH_ID,
    "token": MY_TOKEN
}))

# Step 2: Process messages
message = json.loads(ws.recv())

if message["type"] == "turn_request":
    action = decide_action(message)
    ws.send(json.dumps({
        "type": "turn_action",
        "match_id": message["match_id"],
        "request_id": message["request_id"],
        "action": action
    }))
elif message["type"] in ("hello", "match_start", "round_start",
                          "turn_result", "phase_change", "round_result",
                          "match_end", "action_rejected", "error"):
    handle_known_message(message)
else:
    pass  # MUST ignore unknown message types
```

---

## Appendix A: Full JSON Schemas

### A.1 Server Message Schemas

All server schemas include the common envelope and specify `"additionalProperties": true`.

#### hello (server)

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "hello" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "supported_versions": {
      "type": "array",
      "items": { "type": "string", "pattern": "^\\d+\\.\\d+$" },
      "minItems": 1
    },
    "selected_version": { "type": "string", "pattern": "^\\d+\\.\\d+$" },
    "game_type": { "type": "string" },
    "server_name": { "type": "string", "const": "chipzen" },
    "capabilities": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["type", "match_id", "seq", "server_ts", "supported_versions", "selected_version", "server_name", "game_type", "capabilities"],
  "additionalProperties": true
}
```

#### match_start

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "match_start" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "seats": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "seat": { "type": "integer", "minimum": 0 },
          "display_name": { "type": "string" },
          "participant_id": { "type": "string" },
          "is_self": { "type": "boolean" }
        },
        "required": ["seat", "participant_id", "display_name", "is_self"],
        "additionalProperties": true
      }
    },
    "game_config": { "type": "object" },
    "turn_timeout_ms": { "type": "integer", "minimum": 1000 },
    "your_seat": { "type": "integer", "minimum": 0 }
  },
  "required": ["type", "match_id", "seq", "server_ts", "seats", "game_config", "turn_timeout_ms", "your_seat"],
  "additionalProperties": true
}
```

#### round_start

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "round_start" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "round_id": { "type": "string", "pattern": "^r_" },
    "round_number": { "type": "integer", "minimum": 1 },
    "state": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "round_id", "round_number", "state"],
  "additionalProperties": true
}
```

#### turn_request

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "turn_request" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "seat": { "type": "integer", "minimum": 0 },
    "request_id": { "type": "string" },
    "timeout_ms": { "type": "integer", "minimum": 1 },
    "turn_duration_ms": { "type": "integer", "minimum": 1 },
    "deadline_ts": { "type": "integer" },
    "valid_actions": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "state": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "seat", "request_id", "timeout_ms", "turn_duration_ms", "deadline_ts", "valid_actions", "state"],
  "additionalProperties": true
}
```

#### turn_result

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "turn_result" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "seat": { "type": "integer", "minimum": 0 },
    "details": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "seat", "details"],
  "additionalProperties": true
}
```

#### phase_change

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "phase_change" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "state": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "state"],
  "additionalProperties": true
}
```

#### round_result

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "round_result" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "round_id": { "type": "string", "pattern": "^r_" },
    "round_number": { "type": "integer", "minimum": 1 },
    "result": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "round_id", "round_number", "result"],
  "additionalProperties": true
}
```

#### match_end

The schema of the `match_end` a seat receives when the match is played out. See section 8.9 for the conditional keys and for the shorter frame sent when a match cannot be played out.

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "match_end" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "reason": { "type": "string", "const": "complete" },
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "seat": { "type": "integer", "minimum": 0 },
          "name": { "type": "string" },
          "net_chips": { "type": "integer" },
          "hands_won": { "type": "integer", "minimum": 0 },
          "final_stack": { "type": "integer", "minimum": 0 },
          "bot_errors": { "type": "array", "items": { "type": "object" } },
          "decision_latency_samples_ms": { "type": "array", "items": { "type": "number" } }
        },
        "required": ["seat", "name", "net_chips", "hands_won", "final_stack", "bot_errors", "decision_latency_samples_ms"],
        "additionalProperties": true
      }
    },
    "total_hands_played": { "type": "integer", "minimum": 0 },
    "mode": { "type": "string", "const": "elimination" },
    "finishing_order": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "place": { "type": "integer", "minimum": 1 },
          "seat": { "type": "integer", "minimum": 0 },
          "name": { "type": "string" }
        },
        "required": ["place", "seat", "name"],
        "additionalProperties": true
      }
    },
    "terminal_status": { "type": "string", "enum": ["completed", "error", "abandoned"] }
  },
  "required": ["type", "match_id", "seq", "server_ts", "reason", "results", "total_hands_played", "mode", "finishing_order", "terminal_status"],
  "additionalProperties": true
}
```

#### error

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "error" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "code": { "type": "string" },
    "message": { "type": "string" },
    "game_type": { "type": "string" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "code", "message"],
  "additionalProperties": true
}
```

#### action_rejected

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "action_rejected" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "request_id": { "type": "string" },
    "reason": { "type": "string" },
    "message": { "type": "string" },
    "details": { "type": "object" },
    "submitted_action": { "type": "string" },
    "remaining_ms": { "type": "integer", "minimum": 0 },
    "valid_actions": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["type", "match_id", "seq", "server_ts", "request_id", "reason", "message", "details", "submitted_action", "remaining_ms", "valid_actions"],
  "additionalProperties": true
}
```

The match executor sends no `session_token`, `action_timeout`, `session_control`, `ping` or `reconnected` frame to a bot (sections 8.2 and 8.12-8.15), so they have no schema here.

### A.2 Bot Message Schemas

All bot schemas specify `"additionalProperties": false`.

See sections 9.1 (`hello`), 9.2 (`turn_action`), 9.3 (`pong`), and 9.4 (`authenticate`) for the complete schemas.

---

*End of specification.*
