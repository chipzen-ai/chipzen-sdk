# Chipzen Transport Protocol Specification

**Version:** 1.0.0-draft
**Status:** Draft
**Date:** 2026-04-13

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
| Server to Bot | `hello`, `session_token`, `match_start`, `round_start`, `turn_request`, `turn_result`, `round_result`, `phase_change`, `match_end`, `error`, `action_rejected`, `action_timeout`, `session_control`, `ping`, `reconnected` |
| Bot to Server | `authenticate`, `hello`, `turn_action`, `pong` |

---

## 3. Conventions

### 3.1 Timestamps

All server messages include a `server_ts` field containing an ISO 8601 timestamp with millisecond precision and UTC timezone designator.

Format: `YYYY-MM-DDTHH:mm:ss.sssZ`

Example: `"2026-04-13T14:30:05.123Z"`

### 3.2 Sequence Numbers

All server messages include a `seq` field: a monotonically increasing integer starting at 1 for each connection. Sequence numbers are per-connection, not per-match. On reconnect, sequencing continues from where the previous connection ended.

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
 |<------- session_token -------------|  (4) If authenticated endpoint
 |                                    |
 |<------- match_start ---------------|  (5) Match begins
 |                                    |
 |<------- round_start ---------------|  (6) Round begins
 |<------- turn_request --------------|  (7) Bot's turn
 |------------ turn_action ---------->|  (8) Bot acts
 |<------- turn_result ---------------|  (9) Result broadcast
 |          ... more turns ...        |
 |<------- round_result --------------|  (10) Round ends
 |          ... more rounds ...       |
 |                                    |
 |<------- match_end -----------------|  (11) Match ends
 |                                    |
 |-------- WS Close ----------------->|
```

### 5.1 Handshake Sequence

1. Bot opens a WebSocket connection to the appropriate endpoint.
2. Bot sends an `authenticate` message containing a `ticket` or `token` within 5000ms of connection.
3. Server validates the credential. If invalid, the server closes the connection with code 4001 (`auth_failed`).
4. Server sends a `hello` message containing the `selected_version` (see section 16.3).
5. Bot must respond with a `hello` message within 5000ms.
6. If no mutually supported protocol version exists, server closes with code 4013.
7. For authenticated endpoints, the server sends a `session_token` message.
8. No game messages are sent until the handshake is complete.

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

Sent immediately after WebSocket acceptance. Must be the first message on the connection.

```json
{
  "type": "hello",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 1,
  "server_ts": "2026-04-13T14:30:05.123Z",
  "supported_versions": ["1.0", "1.1"],
  "selected_version": "1.0",
  "game_type": "nlhe_6max",
  "capabilities": ["reconnect", "spectate"],
  "server_id": "match-server-us-east-1a"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `supported_versions` | string[] | yes | Protocol versions supported by the server (major.minor format) |
| `selected_version` | string | yes | The highest mutually supported version, selected after comparing with the bot's `authenticate` message. If no overlap exists, the server closes with code 4013 instead of sending `hello`. |
| `game_type` | string | yes | Identifier for the game type (e.g., `nlhe_6max`, `plo_9max`) |
| `capabilities` | string[] | yes | Server capabilities (e.g., `reconnect`, `spectate`) |
| `server_id` | string | no | Opaque server instance identifier |

### 8.2 `session_token`

Sent after successful handshake on authenticated endpoints. The token is bound to this connection and invalidated on disconnect.

```json
{
  "type": "session_token",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 2,
  "server_ts": "2026-04-13T14:30:05.200Z",
  "token": "ct_a8f3e2b1c4d5e6f7...",
  "expires_at": "2026-04-13T18:30:05.200Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `token` | string | yes | Session token (minimum 32 bytes entropy, cryptographically random, base64url-encoded) |
| `expires_at` | string | yes | ISO 8601 expiration timestamp |

The token is invalidated immediately upon connection close. A new token is issued on reconnect.

### 8.3 `match_start`

Announces the beginning of a match. Sent to all participants after all have connected and completed handshake.

```json
{
  "type": "match_start",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 3,
  "server_ts": "2026-04-13T14:30:06.000Z",
  "seats": [
    {
      "seat": 0,
      "participant_id": "p_abc123",
      "display_name": "AlphaBot",
      "is_self": false
    },
    {
      "seat": 1,
      "participant_id": "p_def456",
      "display_name": "You",
      "is_self": true
    }
  ],
  "game_config": {
    "_comment": "Game-specific configuration -- see Layer 2 spec"
  },
  "turn_timeout_ms": 5000
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `seats` | object[] | yes | Array of seat assignments |
| `seats[].seat` | integer | yes | Zero-indexed seat number |
| `seats[].participant_id` | string | yes | Stable, opaque participant identifier |
| `seats[].display_name` | string | yes | Display name for the participant |
| `seats[].is_self` | boolean | yes | `true` if this seat belongs to the receiving client |
| `game_config` | object | yes | Game-specific configuration (opaque to Layer 1) |
| `turn_timeout_ms` | integer | yes | Default action timeout in milliseconds |

> **Note:** Round/hand count is game-specific and defined in the `game_config` payload (e.g., `total_hands` for poker). Layer 1 does not impose a fixed round count.

### 8.4 `round_start`

Signals the beginning of a new round.

```json
{
  "type": "round_start",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 4,
  "server_ts": "2026-04-13T14:30:07.000Z",
  "round_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "round_number": 1,
  "state": {
    "_comment": "Game-specific state -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `round_id` | string (UUID) | yes | Globally unique identifier for this round/hand, for cross-system audit reference |
| `round_number` | integer | yes | One-indexed round number within the match |
| `state` | object | yes | Game-specific round state (opaque to Layer 1) |

> **Note:** Game-specific Layer 2 protocols MAY include cryptographic verification fields (e.g., deck hash commitment in `round_start.state`) for RNG verifiability.

### 8.5 `turn_request`

Requests an action from the bot. The bot must respond with a `turn_action` before the timeout expires.

```json
{
  "type": "turn_request",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 5,
  "server_ts": "2026-04-13T14:30:07.500Z",
  "seat": 1,
  "request_id": "req_x7y8z9",
  "timeout_ms": 5000,
  "valid_actions": ["fold", "check", "call", "raise"],
  "state": {
    "_comment": "Game-specific state -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `seat` | integer | yes | The seat index of the participant whose turn it is |
| `request_id` | string | yes | Unique identifier for this turn request. Must be echoed in the response. Used for correlation, idempotency, and deduplication. |
| `timeout_ms` | integer | yes | Time remaining to submit an action, in milliseconds |
| `valid_actions` | string[] | yes | List of valid action type strings for this turn |
| `state` | object | yes | Current game-specific state (opaque to Layer 1) |

### 8.6 `turn_result`

Broadcast to all participants after an action is taken (by any participant). The server adds random jitter of 100-500ms before broadcasting to prevent timing side-channel attacks.

```json
{
  "type": "turn_result",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 7,
  "server_ts": "2026-04-13T14:30:08.350Z",
  "seat": 1,
  "is_timeout": false,
  "details": {
    "action": "raise",
    "_comment": "Game-specific action details -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `seat` | integer | yes | Seat number of the participant who acted |
| `is_timeout` | boolean | no | `true` if the server auto-acted due to timeout. Default `false`. Makes timeout-forced actions distinguishable without cross-referencing `action_timeout` messages. |
| `details` | object | yes | Game-specific action details (opaque to Layer 1). The action string (e.g., `"raise"`, `"fold"`) is carried inside this object as defined by the Layer 2 protocol. |

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
  "round_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "round_number": 1,
  "result": {
    "_comment": "Game-specific round result -- see Layer 2 spec"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `round_id` | string (UUID) | yes | Globally unique identifier for this round/hand, for cross-system audit reference |
| `round_number` | integer | yes | One-indexed round number |
| `result` | object | yes | Game-specific result (opaque to Layer 1) |

> **Note:** The complete action history for the round is contained in the `result` payload, defined by the game-specific Layer 2 protocol.

> **Note:** Game-specific Layer 2 protocols MAY include cryptographic verification fields (e.g., deck seed reveal in `round_result.result`) for RNG verifiability.

The server retains all dealt information (e.g., all player cards) even if not exposed to all clients during play. This data is available through administrative and audit APIs.

### 8.9 `match_end`

Signals the end of a match.

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
      "participant_id": "p_abc123",
      "rank": 2,
      "score": 850
    },
    {
      "seat": 1,
      "participant_id": "p_def456",
      "rank": 1,
      "score": 1150
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | string | yes | Why the match ended: `complete`, `forfeit`, `cancelled`, `error` |
| `results` | object[] | yes | Final standings |
| `results[].seat` | integer | yes | Seat number |
| `results[].participant_id` | string | yes | Participant identifier |
| `results[].rank` | integer | yes | Final rank (1 = first place) |
| `results[].score` | number | yes | Final score (game-specific unit) |

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

### 8.11 `action_rejected`

The submitted action failed validation. The bot receives another chance to submit a valid action within the remaining timeout.

```json
{
  "type": "action_rejected",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 8,
  "server_ts": "2026-04-13T14:30:08.100Z",
  "request_id": "req_x7y8z9",
  "reason": "invalid_action",
  "message": "Action 'bet' is not in valid_actions. Valid actions: fold, check, call, raise",
  "remaining_ms": 3200,
  "submitted_action": {
    "action": "bet",
    "params": { "amount": 100 }
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | The `request_id` from the original `turn_request` |
| `reason` | string | yes | Machine-readable rejection reason |
| `message` | string | yes | Human-readable explanation |
| `remaining_ms` | integer | yes | Milliseconds remaining before timeout auto-action |
| `submitted_action` | object | no | Echo of the bot's submitted action for debugging. Contains the `action` and `params` (if any) from the rejected `turn_action`. |

The participant remains in the `awaiting_action` state. The original `request_id` is still valid. The bot should submit a corrected `turn_action` with the same `request_id`.

### 8.12 `action_timeout`

The bot's time expired. The server applied an automatic action.

```json
{
  "type": "action_timeout",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 9,
  "server_ts": "2026-04-13T14:30:12.500Z",
  "request_id": "req_x7y8z9",
  "auto_action": "check"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | The `request_id` of the timed-out request |
| `auto_action` | string | yes | The action the server applied automatically |

**Auto-action policy (consistent across all modes):** The server selects `check` if it is a valid action, otherwise `fold`. This policy applies in all match modes without exception.

### 8.13 `session_control`

Delivered for administrative actions and responsible gaming interventions.

```json
{
  "type": "session_control",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 20,
  "server_ts": "2026-04-13T14:32:00.000Z",
  "action": "pause",
  "reason": "scheduled_break",
  "message": "Match paused for a scheduled break. Play resumes in 60 seconds.",
  "resume_at": "2026-04-13T14:33:00.000Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | yes | Control action: `pause`, `resume`, `terminate`, `intervention` |
| `reason` | string | yes | Machine-readable reason code |
| `message` | string | no | Human-readable explanation |
| `resume_at` | string | no | ISO 8601 timestamp for expected resume (if `pause`) |

### 8.14 `ping`

Server heartbeat. The bot must respond with `pong`.

```json
{
  "type": "ping",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 25,
  "server_ts": "2026-04-13T14:31:00.000Z"
}
```

No additional fields beyond the envelope.

### 8.15 `reconnected`

Sent after a successful reconnection. Provides enough state for the bot to resume without full replay.

```json
{
  "type": "reconnected",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 51,
  "server_ts": "2026-04-13T14:30:15.000Z",
  "round_number": 3,
  "match_state": "in_progress",
  "seats": [
    {
      "seat": 0,
      "participant_id": "p_abc123",
      "display_name": "AlphaBot",
      "is_self": false
    },
    {
      "seat": 1,
      "participant_id": "p_def456",
      "display_name": "You",
      "is_self": true
    }
  ],
  "game_config": {},
  "state": {},
  "pending_request": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `round_number` | integer | yes | Current round number |
| `match_state` | string | yes | Current match state: `in_progress`, `paused`, `between_rounds` |
| `seats` | object[] | yes | Current seat assignments (same schema as `match_start`) |
| `game_config` | object | yes | Game configuration (same as from `match_start`) |
| `state` | object | yes | Current game state snapshot |
| `pending_request` | object or null | yes | If non-null, a `turn_request` payload the bot must respond to |

If `pending_request` is non-null, the bot must treat it as a `turn_request` and respond with a `turn_action`. The `timeout_ms` in the pending request reflects the remaining time, not the original timeout.

> **Note:** The `state` field contains the same structure as `turn_request.state` for the current game type. If the bot was not mid-turn at the time of disconnection, `state` contains the current round state (equivalent to `round_start.state`).

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

Response to a server `ping`. Must be sent within 5000ms.

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
| Pong timeout | 5000ms | No | Time to respond to a `ping` |
| Heartbeat interval | 15000ms | Per competition | Interval between server `ping` messages |
| Reconnection grace | 30000ms | Per competition | Time allowed for a disconnected client to reconnect |

### 10.2 Turn Timeout Behavior

1. Server sends `turn_request` with `timeout_ms`.
2. Timer starts on the server when the message is sent (not when the bot receives it).
3. If the bot sends an invalid action, the server sends `action_rejected` with `remaining_ms`. The bot may retry within the remaining time.
4. If the timer expires before a valid action is received, the server applies the auto-action (check if legal, otherwise fold) and sends `action_timeout`.

### 10.3 Action Delivery Jitter

After a valid action is processed, the server waits a random duration of 100-500ms (uniformly distributed) before broadcasting the `turn_result` to all participants. This prevents opponents from inferring computation time from network timing.

### 10.4 Server-Side Receive Timestamp

When the server receives a `turn_action`, it records a `server_received_ts` timestamp in its audit log. This timestamp is **not** sent to the client but is available for dispute investigation to distinguish network latency from late sends.

---

## 11. Reconnection

### 11.1 Reconnection Flow

1. Client detects disconnection.
2. Client connects to the reconnect endpoint: `wss://<host>/ws/reconnect/{match_id}/{participant_id}`
3. Bot sends `authenticate` with a valid `ticket`.
4. Server validates the credential and match state.
5. Server sends `hello` followed by `reconnected` with full current state.
6. Bot sends `hello`.
7. If a `turn_request` was pending, it is included in `reconnected.pending_request`.
8. Play resumes.

### 11.2 Reconnection Rules

- The previous session token is immediately invalidated on disconnect. A new token is issued on reconnect.
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

---

## 14. Security

### 14.1 Transport Security

All production connections must use `wss://` (WebSocket over TLS). The server must reject unencrypted `ws://` connections in production environments. Unencrypted connections are permitted only on `localhost` for development.

### 14.2 Authentication

- **Competitive endpoint:** Authenticated via a one-time-use `ticket` sent in the `authenticate` message (first message after WebSocket upgrade). Tickets expire after a single use or after a short TTL (configurable, default 60s).
- **Bot endpoint:** Authenticated via a `token` sent in the `authenticate` message, or network-level isolation. Tokens must have minimum 32 bytes of cryptographically random entropy.
- Credentials are never sent as URL query parameters to avoid logging in server access logs, proxy logs, and browser history.

### 14.3 Session Tokens

Session tokens issued via the `session_token` message:
- Must contain at least 32 bytes of cryptographically random data.
- Are encoded as base64url strings with a `ct_` prefix.
- Are bound to the connection that received them.
- Are invalidated immediately when the connection closes.
- Must not be reused across connections.

### 14.4 Timing Side-Channel Mitigation

The server adds uniformly random jitter of 100-500ms before broadcasting `turn_result` messages. This prevents participants from inferring an opponent's computation time from message delivery timing.

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

The server and bot exchange `supported_versions` arrays in their `hello` messages. Each version string follows `major.minor` format. The server selects the highest mutually supported version and includes it as `selected_version` in its `hello` response. If no mutually supported version exists, the server closes the connection with code 4013 (`protocol_mismatch`).

- **Major version change:** Breaking changes. Server and bot must agree on the major version.
- **Minor version change:** Backward-compatible additions (new message types, new fields). A bot supporting version 1.0 can connect to a server running 1.3 without issues, as the server will select 1.0.

### 16.4 Reserved Namespaces

The `spectator_*` message type namespace is reserved for future spectator functionality. Implementations must not use message types beginning with `spectator_` for other purposes.

---

## 17. Quick-Start Example

A minimal bot session demonstrating the complete lifecycle including heartbeat handling.

```
# Direction markers:  S→B = server to bot,  B→S = bot to server

# --- Authentication ---

B→S  {"type":"authenticate","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "ticket":"tk_one_time_use_ticket_value"}

# --- Handshake ---

S→B  {"type":"hello","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":1,
      "server_ts":"2026-04-13T14:30:05.000Z",
      "supported_versions":["1.0","1.1"],"selected_version":"1.0",
      "game_type":"nlhe_6max","capabilities":["reconnect"]}

B→S  {"type":"hello","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "supported_versions":["1.0"],"client_name":"ExampleBot","client_version":"0.1.0"}

S→B  {"type":"match_start","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":2,
      "server_ts":"2026-04-13T14:30:06.000Z",
      "seats":[
        {"seat":0,"participant_id":"p_abc123","display_name":"OpponentBot","is_self":false},
        {"seat":1,"participant_id":"p_def456","display_name":"ExampleBot","is_self":true}
      ],
      "game_config":{"variant":"nlhe","max_players":6,"starting_stack":1000},
      "turn_timeout_ms":5000}

S→B  {"type":"round_start","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":3,
      "server_ts":"2026-04-13T14:30:07.000Z",
      "round_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","round_number":1,
      "state":{"phase":"preflop","pot":15}}

S→B  {"type":"turn_request","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":4,
      "server_ts":"2026-04-13T14:30:07.500Z","seat":1,
      "request_id":"req_001","timeout_ms":5000,
      "valid_actions":["fold","call","raise"],
      "state":{"phase":"preflop","pot":15,"to_call":10}}

B→S  {"type":"turn_action","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "request_id":"req_001","action":"call"}

S→B  {"type":"turn_result","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":5,
      "server_ts":"2026-04-13T14:30:08.100Z","seat":1,
      "details":{"action":"call"}}

# --- Heartbeat arrives mid-match ---

S→B  {"type":"ping","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":6,
      "server_ts":"2026-04-13T14:30:15.000Z"}

B→S  {"type":"pong","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890"}

# --- Round concludes ---

S→B  {"type":"round_result","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":10,
      "server_ts":"2026-04-13T14:30:20.000Z",
      "round_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","round_number":1,
      "result":{"winners":[0],"scores":[1030,970]}}

# --- Match concludes after all rounds ---

S→B  {"type":"match_end","match_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","seq":50,
      "server_ts":"2026-04-13T14:35:00.000Z","reason":"complete",
      "results":[
        {"seat":0,"participant_id":"p_abc123","rank":1,"score":1150},
        {"seat":1,"participant_id":"p_def456","rank":2,"score":850}
      ]}
```

### 17.1 Handling Unknown Messages

Bots will encounter message types not listed in this specification as the protocol evolves. A conforming bot must ignore them:

```python
# Python pseudocode

# Step 1: Authenticate immediately after WebSocket upgrade
ws.send(json.dumps({
    "type": "authenticate",
    "match_id": MATCH_ID,
    "ticket": MY_TICKET
}))

# Step 2: Process messages
message = json.loads(ws.recv())

if message["type"] == "ping":
    ws.send(json.dumps({"type": "pong", "match_id": message["match_id"]}))
elif message["type"] == "turn_request":
    action = decide_action(message)
    ws.send(json.dumps({
        "type": "turn_action",
        "match_id": message["match_id"],
        "request_id": message["request_id"],
        "action": action
    }))
elif message["type"] in ("hello", "match_start", "round_start",
                          "turn_result", "phase_change", "round_result",
                          "match_end", "action_rejected", "action_timeout",
                          "session_control", "session_token", "reconnected",
                          "error"):
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
    "capabilities": { "type": "array", "items": { "type": "string" } },
    "server_id": { "type": "string" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "supported_versions", "selected_version", "game_type", "capabilities"],
  "additionalProperties": true
}
```

#### session_token

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "session_token" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "token": { "type": "string", "pattern": "^ct_[A-Za-z0-9_-]{43,}$" },
    "expires_at": { "type": "string", "format": "date-time" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "token", "expires_at"],
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
          "participant_id": { "type": "string" },
          "display_name": { "type": "string" },
          "is_self": { "type": "boolean" }
        },
        "required": ["seat", "participant_id", "display_name", "is_self"],
        "additionalProperties": true
      }
    },
    "game_config": { "type": "object" },
    "turn_timeout_ms": { "type": "integer", "minimum": 1000 }
  },
  "required": ["type", "match_id", "seq", "server_ts", "seats", "game_config", "turn_timeout_ms"],
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
    "round_id": { "type": "string", "format": "uuid" },
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
    "valid_actions": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "state": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "seat", "request_id", "timeout_ms", "valid_actions", "state"],
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
    "is_timeout": { "type": "boolean", "default": false },
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
    "round_id": { "type": "string", "format": "uuid" },
    "round_number": { "type": "integer", "minimum": 1 },
    "result": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "round_id", "round_number", "result"],
  "additionalProperties": true
}
```

#### match_end

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "match_end" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "reason": { "type": "string", "enum": ["complete", "forfeit", "cancelled", "error"] },
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "seat": { "type": "integer", "minimum": 0 },
          "participant_id": { "type": "string" },
          "rank": { "type": "integer", "minimum": 1 },
          "score": { "type": "number" }
        },
        "required": ["seat", "participant_id", "rank", "score"],
        "additionalProperties": true
      }
    }
  },
  "required": ["type", "match_id", "seq", "server_ts", "reason", "results"],
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
    "message": { "type": "string" }
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
    "remaining_ms": { "type": "integer", "minimum": 0 },
    "submitted_action": { "type": "object" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "request_id", "reason", "message", "remaining_ms"],
  "additionalProperties": true
}
```

#### action_timeout

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "action_timeout" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "request_id": { "type": "string" },
    "auto_action": { "type": "string" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "request_id", "auto_action"],
  "additionalProperties": true
}
```

#### session_control

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "session_control" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "action": { "type": "string", "enum": ["pause", "resume", "terminate", "intervention"] },
    "reason": { "type": "string" },
    "message": { "type": "string" },
    "resume_at": { "type": "string", "format": "date-time" }
  },
  "required": ["type", "match_id", "seq", "server_ts", "action", "reason"],
  "additionalProperties": true
}
```

#### ping

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "ping" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" }
  },
  "required": ["type", "match_id", "seq", "server_ts"],
  "additionalProperties": true
}
```

#### reconnected

```json
{
  "type": "object",
  "properties": {
    "type": { "const": "reconnected" },
    "match_id": { "type": "string", "format": "uuid" },
    "seq": { "type": "integer", "minimum": 1 },
    "server_ts": { "type": "string", "format": "date-time" },
    "round_number": { "type": "integer", "minimum": 1 },
    "match_state": { "type": "string", "enum": ["in_progress", "paused", "between_rounds"] },
    "seats": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "seat": { "type": "integer", "minimum": 0 },
          "participant_id": { "type": "string" },
          "display_name": { "type": "string" },
          "is_self": { "type": "boolean" }
        },
        "required": ["seat", "participant_id", "display_name", "is_self"],
        "additionalProperties": true
      }
    },
    "game_config": { "type": "object" },
    "state": { "type": "object" },
    "pending_request": {
      "oneOf": [
        { "type": "null" },
        {
          "type": "object",
          "properties": {
            "request_id": { "type": "string" },
            "timeout_ms": { "type": "integer", "minimum": 1 },
            "valid_actions": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
            "state": { "type": "object" }
          },
          "required": ["request_id", "timeout_ms", "valid_actions", "state"]
        }
      ]
    }
  },
  "required": ["type", "match_id", "seq", "server_ts", "round_number", "match_state", "seats", "game_config", "state", "pending_request"],
  "additionalProperties": true
}
```

### A.2 Bot Message Schemas

All bot schemas specify `"additionalProperties": false`.

See sections 9.1 (`hello`), 9.2 (`turn_action`), 9.3 (`pong`), and 9.4 (`authenticate`) for the complete schemas.

---

*End of specification.*
