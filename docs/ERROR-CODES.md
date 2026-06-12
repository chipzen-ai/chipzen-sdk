<!-- Canonical public copy. Mirrored from the Chipzen platform repo (docs/ERROR-CODES.md); platform-internal issue references appear as CZ#NNNN. -->

# Chipzen Error Code Catalogue

Every error your bot can encounter while talking to Chipzen, with a plain-English meaning, the typical cause, what to do about it, and a pointer back to the spec for the deep dive. Use this as a crib sheet while developing — keep the [TRANSPORT-PROTOCOL spec](protocol/TRANSPORT-PROTOCOL.md) open for the field-level detail.

There are three layers of errors a Chipzen bot can see:

1. **WebSocket close codes** (4001–4013). The server closed the connection — your bot is no longer in the match.
2. **In-band protocol errors** (`error`, `action_rejected`, `action_timeout`, `bot_error`). The match is still running. Your bot may need to retry, change behaviour, or simply observe.
3. **HTTP API errors** (`AUTH_*`, `USER_*`, `GAME_*`, `BOT_*`, `RATE_*`, `INTERNAL_*`). Returned by the REST API when you upload, list, or manage bots. Not seen during live play.

Source-of-truth specs:
- [docs/protocol/TRANSPORT-PROTOCOL.md](protocol/TRANSPORT-PROTOCOL.md) — Layer 1 transport (close codes, envelope, error / action_rejected / action_timeout messages)
- [docs/protocol/POKER-GAME-STATE-PROTOCOL.md](protocol/POKER-GAME-STATE-PROTOCOL.md) — Layer 2 poker payload semantics
- [docs/DEV-MANUAL.md](DEV-MANUAL.md) — developer manual including debugging surfaces

---

## Table of contents

- [1. WebSocket close codes (4001–4013)](#1-websocket-close-codes-40014013)
  - [`4001` auth_failed](#4001-auth_failed)
  - [`4002` match_not_found](#4002-match_not_found)
  - [`4003` match_full](#4003-match_full)
  - [`4004` match_started](#4004-match_started)
  - [`4005` match_ended](#4005-match_ended)
  - [`4006` participant_not_found](#4006-participant_not_found)
  - [`4007` handshake_timeout](#4007-handshake_timeout)
  - [`4008` message_too_large](#4008-message_too_large)
  - [`4009` rate_limit_exceeded](#4009-rate_limit_exceeded)
  - [`4010` reconnect_expired](#4010-reconnect_expired)
  - [`4011` server_error](#4011-server_error)
  - [`4012` responsible_gaming](#4012-responsible_gaming)
  - [`4013` protocol_mismatch](#4013-protocol_mismatch)
- [2. `error` message codes](#2-error-message-codes)
  - [`malformed_message`](#malformed_message)
  - [`unknown_field`](#unknown_field)
  - [`rate_limited`](#rate_limited)
  - [`match_cancelled`](#match_cancelled)
  - [`invalid_action` (human-play frontend)](#invalid_action-human-play-frontend)
- [3. `action_rejected` reasons](#3-action_rejected-reasons)
  - [Wrong turn](#wrong-turn)
  - [Duplicate / replayed action](#duplicate--replayed-action)
  - [Action not in `valid_actions`](#action-not-in-valid_actions)
  - [Raise amount out of range](#raise-amount-out-of-range)
  - [No valid actions available](#no-valid-actions-available)
- [4. `action_timeout`](#4-action_timeout)
- [5. `bot_error` (human-vs-bot only)](#5-bot_error-human-vs-bot-only)
  - [`bot_container_failed_to_attach`](#bot_container_failed_to_attach)
  - [`bot_connector_disconnected_midmatch`](#bot_connector_disconnected_midmatch)
  - [`bot_decision_timeout`](#bot_decision_timeout)
  - [`bot_invalid_action`](#bot_invalid_action)
  - [`bot_exception`](#bot_exception)
- [6. HTTP API errors](#6-http-api-errors)
- [7. A "good" error response from your bot](#7-a-good-error-response-from-your-bot)
- [8. Reporting a bug](#8-reporting-a-bug)

---

## 1. WebSocket close codes (4001–4013)

These are RFC 6455 close codes in the application-defined range. The server attaches the symbolic name in the close reason and tears down the connection. **Your bot will not receive a final `error` message — read the close code from the socket close event.**

See [TRANSPORT-PROTOCOL §15](protocol/TRANSPORT-PROTOCOL.md#15-websocket-close-codes) for the canonical table.

### `4001` auth_failed

**What it means.** The `authenticate` message you sent as your first frame after WebSocket upgrade was rejected.

**Typical cause.** Expired or already-used ticket; missing token on the `/bot` endpoint; both `ticket` and `token` supplied (the schema requires exactly one); credential sent in the URL query string instead of the `authenticate` message.

**Remediation.** Re-acquire a fresh `ticket` from the matchmaking API and send `authenticate` as the **first** bot frame (not after `hello`). Tickets are single-use with a short TTL (default 60s) — generate one immediately before connecting. For sidecar / internal bots, set `CHIPZEN_TOKEN` and send `{"type":"authenticate","match_id":"…","token":"…"}`. Never put the credential in the URL.

**See also.** [TRANSPORT-PROTOCOL §4.4 Authentication Flow](protocol/TRANSPORT-PROTOCOL.md#44-authentication-flow), [§9.4 `authenticate`](protocol/TRANSPORT-PROTOCOL.md#94-authenticate), [§14.2 Authentication](protocol/TRANSPORT-PROTOCOL.md#142-authentication).

### `4002` match_not_found

**What it means.** The `match_id` in the URL path does not correspond to any known match.

**Typical cause.** Typo or stale UUID; reusing a `match_id` from a previous session; race where the bot connects before the matchmaker has persisted the match record.

**Remediation.** Re-read the `match_id` from the matchmaking API response that gave you the ticket. Don't hard-code UUIDs across runs.

**See also.** [TRANSPORT-PROTOCOL §4 Connection Endpoints](protocol/TRANSPORT-PROTOCOL.md#4-connection-endpoints).

### `4003` match_full

**What it means.** All seats in the match are already occupied.

**Typical cause.** Two clients race to claim the last seat with the same ticket; a stale reconnection attempt arrives after the seat has been re-filled.

**Remediation.** Don't retry the same match. Ask the matchmaker for a new assignment.

### `4004` match_started

**What it means.** The match is already in progress, and the normal join endpoint is no longer accepted for your participant.

**Typical cause.** Bot crashed and tried to reconnect on `/ws/match/...` instead of `/ws/reconnect/...`.

**Remediation.** Switch to the reconnection endpoint: `wss://<host>/ws/reconnect/{match_id}/{participant_id}` and re-authenticate. The server will send a `reconnected` message with the current state and any `pending_request`.

**See also.** [TRANSPORT-PROTOCOL §11 Reconnection](protocol/TRANSPORT-PROTOCOL.md#11-reconnection).

### `4005` match_ended

**What it means.** The match has already concluded. No further messages will be exchanged.

**Typical cause.** Late reconnection attempt after `match_end` was broadcast; opponent forfeited and the match closed while your bot was disconnected.

**Remediation.** Treat this as terminal. Read the final standings from the REST API (`GET /matches/{match_id}`) if you need the result.

### `4006` participant_not_found

**What it means.** The `participant_id` in the URL path is not recognised for this match.

**Typical cause.** Wrong `participant_id` — most commonly mixing up the human player's ID with the bot's seat ID, or copying the ID from a previous match. On the human-play WS endpoint (`/ws/play/{match_id}`), this code is also used when the browser fails to send the `client_ready` sentinel within the handshake window (5s).

**Remediation.** Use the `participant_id` returned alongside the ticket. For bot endpoints, the seat assignment is in the `match_start.seats[]` payload — the seat with `is_self: true` is yours.

**See also.** [TRANSPORT-PROTOCOL §4.3 Path Parameters](protocol/TRANSPORT-PROTOCOL.md#43-path-parameters).

### `4007` handshake_timeout

**What it means.** The bot did not send its `hello` message within **5000ms** of receiving the server's `hello`.

**Typical cause.** Bot is doing heavy startup work (loading a model, opening DB connections) before reading the socket; sync code blocks the event loop; the bot tried to authenticate after `hello` rather than before.

**Remediation.** Open the WebSocket → immediately send `authenticate` → as soon as you read server `hello`, send your `hello`. Defer model loading and other expensive setup until after the handshake completes (or do it before connecting). Do not do any network I/O during the handshake.

**See also.** [TRANSPORT-PROTOCOL §5.1 Handshake Sequence](protocol/TRANSPORT-PROTOCOL.md#51-handshake-sequence), [§10.1 Default Values](protocol/TRANSPORT-PROTOCOL.md#101-default-values).

### `4008` message_too_large

**What it means.** A bot-to-server frame exceeded the **4096-byte** ceiling.

**Typical cause.** Stuffing extra fields into `turn_action.params`; sending logs or large debug payloads in the action; including the full `state` echo in your response.

**Remediation.** Only send the fields the schema requires (`type`, `match_id`, `request_id`, `action`, optional `params`). Bot schemas use `additionalProperties: false` — unknown fields are rejected anyway. Move any extra logging to your container's stdout/stderr.

**See also.** [TRANSPORT-PROTOCOL §3.5 Maximum Message Size](protocol/TRANSPORT-PROTOCOL.md#35-maximum-message-size).

### `4009` rate_limit_exceeded

**What it means.** Fifth rate-limit violation in the rolling window — the connection is force-closed.

**Typical cause.** Sending more than **10 messages/second**; submitting more than 5 invalid actions in a single round; tight reconnection loop without backoff.

**Remediation.** Throttle your sends. You should normally produce **one** `turn_action` per `turn_request` plus a `pong` per `ping` — there is no reason to exceed 10 msg/s in steady state. Use exponential backoff between reconnections (start at ~1s, cap at 60s). Validate locally before sending to cut invalid-action storms.

**See also.** [TRANSPORT-PROTOCOL §13 Rate Limiting](protocol/TRANSPORT-PROTOCOL.md#13-rate-limiting).

### `4010` reconnect_expired

**What it means.** The 30-second reconnection grace period elapsed without you reconnecting. You forfeit any remaining rounds.

**Typical cause.** Long GC pause; container restart; network partition longer than 30s; exhausted your per-match reconnection budget of **3 reconnects**.

**Remediation.** Detect disconnects fast (use the WebSocket close callback, not just timeouts) and reconnect within ~5s. If you anticipate operations longer than 5s (model swap, etc.), do them before connecting.

**See also.** [TRANSPORT-PROTOCOL §11.2 Reconnection Rules](protocol/TRANSPORT-PROTOCOL.md#112-reconnection-rules), [§11.3 Reconnection Budget](protocol/TRANSPORT-PROTOCOL.md#113-reconnection-budget).

### `4011` server_error

**What it means.** Unrecoverable server-side error. Not your fault.

**Typical cause.** Infrastructure incident, bug in the match runner, database connectivity.

**Remediation.** Report it with the `match_id` and approximate timestamp. The matchmaking API will let you join a new match. Do not retry the same match — it has been torn down.

### `4012` responsible_gaming

**What it means.** The connection was closed by a responsible-gaming intervention. Specific to human accounts; bot-only matches don't see this.

**Typical cause.** Self-exclusion, deposit/time limits exceeded, admin intervention.

**Remediation.** No bot action required — humans manage these flags via their account settings.

### `4013` protocol_mismatch

**What it means.** Server and bot have no overlap in `supported_versions`. Sent in place of the server `hello`.

**Typical cause.** Bot pins an outdated `supported_versions: ["0.9"]`; server has been upgraded to a new major version with no backwards compatibility.

**Remediation.** Bump your `supported_versions` array in the client `hello` to include the current major version (currently `"1.0"`). The server selects the highest mutually supported version, so listing multiple versions is safe: `"supported_versions": ["1.0", "1.1"]`.

**See also.** [TRANSPORT-PROTOCOL §16.3 Version Negotiation](protocol/TRANSPORT-PROTOCOL.md#163-version-negotiation).

---

## 2. `error` message codes

Sent as a payload (not a close code) when the match should keep running. Envelope:

```json
{
  "type": "error",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 12,
  "server_ts": "2026-04-13T14:30:10.000Z",
  "code": "<error_code>",
  "message": "<human-readable description>"
}
```

See [TRANSPORT-PROTOCOL §8.10](protocol/TRANSPORT-PROTOCOL.md#810-error) for the envelope schema.

### `malformed_message`

**What it means.** A bot frame failed JSON parsing or did not match the schema for its declared `type`.

**Typical cause.** Sending a non-UTF-8 frame; sending a binary WebSocket frame (only text is accepted); missing `match_id` or `request_id`; passing `params` as a string instead of an object.

**Remediation.** Always emit JSON via your language's standard encoder (`json.dumps`, `JSON.stringify`, `serde_json::to_string`). Never hand-build JSON strings. Validate against the schemas in [TRANSPORT-PROTOCOL §A.2](protocol/TRANSPORT-PROTOCOL.md#a2-bot-message-schemas) before sending if your client doesn't already.

**See also.** [TRANSPORT-PROTOCOL §12.3 Malformed Messages](protocol/TRANSPORT-PROTOCOL.md#123-malformed-messages).

### `unknown_field`

**What it means.** Your message contained a field not defined in the schema. Bot schemas use `additionalProperties: false`.

**Typical cause.** Echoing the full `state` back in your `turn_action`; adding a debug field like `bot_version` to every message; misspelling a field name (`params_` instead of `params`).

**Remediation.** Send only the documented fields. For debugging, log locally — don't pass through the wire. Read the schema in [TRANSPORT-PROTOCOL §9](protocol/TRANSPORT-PROTOCOL.md#9-bot-to-server-messages) for the allowed set.

**See also.** [TRANSPORT-PROTOCOL §16.2 Unknown Fields](protocol/TRANSPORT-PROTOCOL.md#162-unknown-fields).

### `rate_limited`

**What it means.** First through fourth rate-limit violation (10 msg/s or 5 invalid actions/round). The server warns you but keeps the connection open. The fifth violation upgrades to close code 4009.

**Typical cause.** Bursts of retries after `action_rejected`; reconnection loops without backoff; spamming `pong` ahead of `ping`.

**Remediation.** Back off. One `turn_action` per `turn_request`. Don't pre-emptively send messages — the protocol is strictly request-response except for `pong` (which only fires in response to `ping`).

**See also.** [TRANSPORT-PROTOCOL §13.2 Enforcement](protocol/TRANSPORT-PROTOCOL.md#132-enforcement).

### `match_cancelled`

**What it means.** The match was cancelled before completion — typically because an opponent failed to connect within the 30-second connection-wait window.

**Typical cause.** Opponent crashed on startup; matchmaking paired you with a dead bot.

**Remediation.** No bot action required. The match is torn down; ask the matchmaker for a new assignment.

**See also.** [TRANSPORT-PROTOCOL §5.2 Connection Wait](protocol/TRANSPORT-PROTOCOL.md#52-connection-wait).

### `invalid_action` (human-play frontend)

**What it means.** This code is emitted on the **human-play** WS endpoint (`/ws/play/...`) when a browser submits an action that doesn't validate. Bots see [`action_rejected`](#3-action_rejected-reasons) instead.

**Typical cause.** A human clicked "Raise 1000" but their stack only allowed 800. Not applicable to bots.

**Remediation.** N/A for bot authors. Listed here for completeness when inspecting frames from a human-vs-bot match.

---

## 3. `action_rejected` reasons

Your action submission was understood but failed game-state validation. Unlike `error`, this is **recoverable**: your turn is still open and you can submit a corrected action with the **same `request_id`** as long as `remaining_ms > 0`.

Envelope (see [TRANSPORT-PROTOCOL §8.11](protocol/TRANSPORT-PROTOCOL.md#811-action_rejected)):

```json
{
  "type": "action_rejected",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 8,
  "server_ts": "2026-04-13T14:30:08.100Z",
  "request_id": "req_x7y8z9",
  "reason": "Action 'bet' is not valid. Valid actions: ['fold', 'check', 'call', 'raise']",
  "message": "Action 'bet' is not valid. Valid actions: ['fold', 'check', 'call', 'raise']",
  "remaining_ms": 3200,
  "submitted_action": "bet",
  "valid_actions": ["fold", "check", "call", "raise"]
}
```

The `reason` field is currently a sentence rather than a stable enum string — match on substrings or use `valid_actions` (added in v0.3.53) to pick a legal retry. Older servers may omit `valid_actions`; in that case, fall back to `["check", "fold"]` — the server's auto-action policy guarantees one of those is always legal. The five distinguishable rejection categories the validator emits today are documented below.

### Wrong turn

**Reason text.** `Seat N submitted action but it is seat M's turn`.

**What it means.** You sent a `turn_action` when it was not your seat's turn. The server silently drops actions in this state under most code paths, but submission via the human-play endpoint surfaces it as a rejection.

**Typical cause.** Echoing a stale `turn_request` after a `turn_result` arrived for someone else; submitting an action without correlating to the most recent `request_id`.

**Remediation.** Only respond to `turn_request` with a matching `request_id`. Treat `turn_result` as informational — do not act on it.

### Duplicate / replayed action

**Reason text.** `Duplicate action for decision point <hash>`.

**What it means.** You submitted a second action for a decision point the server has already accepted an action for.

**Typical cause.** Retry-on-timeout logic firing after the original action landed; reconnection echoing a buffered action.

**Remediation.** Treat `turn_request` as authoritative. Don't re-send the same `request_id` after you saw a `turn_result` for it. On reconnect, read `pending_request` from the `reconnected` message — if it's `null`, your previous action already landed.

**See also.** [TRANSPORT-PROTOCOL §8.15 `reconnected`](protocol/TRANSPORT-PROTOCOL.md#815-reconnected).

### Action not in `valid_actions`

**Reason text.** `Action '<action>' is not valid. Valid actions: [...]`.

**What it means.** The action string is not legal for this decision point.

**Typical cause.** Choosing `check` when `to_call > 0`; choosing `raise` when your stack is below `min_raise` (`all_in` is the right action there); using a non-canonical action string (`bet`, `BET`, `raise-to`, etc. — the only legal strings are `fold`, `check`, `call`, `raise`, `all_in`).

**Remediation.** Always filter your candidate action through the `valid_actions` array from the `turn_request`. Use the literal strings from [POKER-GAME-STATE-PROTOCOL §2](protocol/POKER-GAME-STATE-PROTOCOL.md#2-action-vocabulary) — they're case-sensitive.

### Raise amount out of range

**What it means.** A `raise` action's `params.amount` is below `min_raise` or above `max_raise`.

**Note.** Under the current validator, raises with an out-of-range `amount` are **clamped** to `[min_raise, max_raise]` and applied — you'll see a `turn_result` rather than `action_rejected`. The server logs the clamp at INFO. Treat this as advisory: don't depend on clamping, send a legal amount.

**Typical cause.** Forgetting that `min_raise` / `max_raise` are **total bet sizes**, not increments above the current bet. Forgetting that `min_raise` can be `0` (raise unavailable) and you must use `all_in` instead.

**Remediation.** Read [POKER-GAME-STATE-PROTOCOL §5.4 Raise Sizing](protocol/POKER-GAME-STATE-PROTOCOL.md#54-raise-sizing). Compute `amount = clamp(your_target, min_raise, max_raise)` yourself before sending.

### No valid actions available

**Reason text.** `No valid actions available (player cannot act)`.

**What it means.** The current game state offers no legal player actions — typically because you're already all-in or have folded.

**Typical cause.** Stale `turn_request` that you tried to respond to after a `phase_change`; bug in your local state machine that thinks it's your turn when it isn't.

**Remediation.** This is exceptional — the server should not normally send a `turn_request` here. If you see it, log the full message and report it. Your bot should respond with `fold` to keep the loop moving; the server will drop the message.

---

## 4. `action_timeout`

Not strictly an error — this is the server telling you it auto-acted because your `turn_action` didn't arrive in time. Your bot keeps playing.

Envelope (see [TRANSPORT-PROTOCOL §8.12](protocol/TRANSPORT-PROTOCOL.md#812-action_timeout)):

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

**Auto-action policy.** The server picks `check` if it is legal, otherwise `fold`. This is consistent across all match modes.

**Typical cause.** `decide()` exceeded the **5000ms** turn budget (default; per-competition configurable); synchronous network call blocking the event loop; GC pause on a long-running bot.

**Remediation.** Use the SDK's async runner, set explicit timeouts on outbound HTTP (3s max), profile your `decide()`. The bot dashboard at `/bots/<bot_id>/dashboard` shows decision latency p99 / max so you can spot the bot drifting toward the 5000ms wall before it starts losing turns. The corresponding `is_timeout: true` flag in `action_history` makes timeout-forced actions distinguishable in replays — see [POKER-GAME-STATE-PROTOCOL §5.8](protocol/POKER-GAME-STATE-PROTOCOL.md#58-timeout-behavior).

**See also.** [TRANSPORT-PROTOCOL §10.2 Turn Timeout Behavior](protocol/TRANSPORT-PROTOCOL.md#102-turn-timeout-behavior), [DEV-MANUAL §6 Performance](DEV-MANUAL.md#6-performance), [DEV-MANUAL §9.3 "My `decide()` is timing out"](DEV-MANUAL.md#93-my-decide-is-timing-out).

---

## 5. `bot_error` (human-vs-bot only)

A Chipzen-specific in-band notification sent on the **human-play** WebSocket (`/ws/play/...`) when the bot side of a human-vs-bot match misbehaves. Your bot will never receive this — it's surfaced to the human player so the UI can show "Bot disconnected mid-match" instead of silently substituting check/fold. Documented here so bot authors understand what their failures look like from the human's seat.

Envelope:

```json
{
  "type": "bot_error",
  "match_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "seq": 14,
  "server_ts": "2026-04-13T14:30:11.000Z",
  "reason": "bot_decision_timeout",
  "message": "MyBot did not respond within 5000ms.",
  "hand_number": 3,
  "phase": "flop",
  "match_continues": true
}
```

After **3 consecutive** `bot_error` events without an intervening successful action, the match aborts with `match_end.reason = "bot_failed"` and `match_continues: false` on the final event.

### `bot_container_failed_to_attach`

**What the human sees.** "MyBot did not connect in time (bot container failed to start)."

**Typical cause.** The bot container crashed on startup, has the wrong WS URL baked in, or exceeded the 15-second cold-start window. Common subcauses: wrong API bind address (`127.0.0.1` instead of `host.docker.internal` on Docker Desktop), missing dependency, seccomp-blocked syscall.

**Remediation.** Test locally with `docker run my-bot:v1` and verify the container reaches the API on `host.docker.internal` (Docker Desktop) or `0.0.0.0:8001` (native Linux with `--network=host`). Trim startup work — load models lazily, not at import time.

**See also.** [DEV-MANUAL §9.4 "Bot can't reach the server / WS upgrade fails"](DEV-MANUAL.md#94-bot-cant-reach-the-server--ws-upgrade-fails), [DEV-MANUAL §9.5 "Container dies immediately with no logs"](DEV-MANUAL.md#95-container-dies-immediately-with-no-logs).

### `bot_connector_disconnected_midmatch`

**What the human sees.** "MyBot disconnected mid-match."

**Typical cause.** Container OOM kill, panic, or clean exit before `match_end`; lost WS connection without reconnecting via `/ws/reconnect/...`.

**Remediation.** Run a stress test locally for the full match length. Watch container memory with `docker stats`. Implement reconnection per [TRANSPORT-PROTOCOL §11](protocol/TRANSPORT-PROTOCOL.md#11-reconnection).

### `bot_decision_timeout`

**What the human sees.** "MyBot did not respond within 5000ms."

**Typical cause.** Same as `action_timeout` (Section 4) — `decide()` blew the turn budget.

**Remediation.** See Section 4 above.

### `bot_invalid_action`

**What the human sees.** "MyBot sent an invalid action (<detail>)."

**Typical cause.** Sent an action string not in `valid_actions`; raise amount out of range and refused to retry; unparseable JSON in the `turn_action` payload; mismatched `request_id`.

**Remediation.** Validate against `valid_actions` before sending. Always echo the exact `request_id` from the `turn_request`. Handle `action_rejected` by retrying with a safe fallback (see Section 7).

### `bot_exception`

**What the human sees.** "Internal error while waiting for MyBot: <detail>."

**Typical cause.** Server-side exception in the bot connector path. Should be rare — indicates a bug in the bridge between the match server and the bot container.

**Remediation.** Report it with the `match_id`, your bot name, and the timestamp. Not normally caused by anything you can fix in your bot code.

---

## 6. HTTP API errors

Returned by the REST API (`api.chipzen.ai/v1/*`) when uploading, listing, or managing bots and matches — **not** seen during live play. Format:

```json
{
  "error_code": "GAME_001",
  "message": "The house bot is not available. Please try again later.",
  "request_id": "abc123"
}
```

The error code is the stable identifier; the message text may evolve.

### Authentication (AUTH)

| Code     | Message                            | HTTP |
|----------|------------------------------------|------|
| AUTH_001 | Invalid or expired access token.   | 401  |
| AUTH_002 | Access revoked.                    | 403  |
| AUTH_003 | Insufficient permissions.          | 403  |

### User account (USER)

| Code     | Message                                                                                  | HTTP |
|----------|------------------------------------------------------------------------------------------|------|
| USER_001 | Your account is pending approval. Please contact the Chipzen team or use an invite link. | 403  |
| USER_002 | User not found.                                                                          | 404  |
| USER_003 | Guest users cannot access this feature. Please upgrade your account.                     | 403  |

### Game session (GAME)

| Code     | Message                                            | HTTP |
|----------|----------------------------------------------------|------|
| GAME_001 | The house bot is not available. Please try again later. | 503  |
| GAME_002 | Session not found.                                 | 404  |
| GAME_003 | Invalid difficulty level.                          | 422  |
| GAME_004 | Unknown game type.                                 | 422  |
| GAME_005 | Match not found.                                   | 404  |
| GAME_006 | You did not participate in this match.             | 403  |
| GAME_007 | Feedback already submitted for this match.         | 409  |

### Bot (BOT)

| Code    | Message                                                          | HTTP |
|---------|------------------------------------------------------------------|------|
| BOT_001 | Bot not found or not active.                                     | 404  |
| BOT_002 | Bot build failed.                                                | 500  |
| BOT_003 | Bot execution timed out.                                         | 504  |
| BOT_004 | Failed to start bot container. Please try again.                 | 503  |

The bot-slot limit (3 bots per user) returns **HTTP 409** with a contextual message from the upload route; there is no dedicated code for it yet.

### Geo policy (GEO)

| Code    | Message                                    | HTTP |
|---------|--------------------------------------------|------|
| GEO_001 | Sign-ups are not available in your region. | 403  |
| GEO_002 | Sign-ups are not available in your region. | 403  |

GEO_001 = the resolved region is blocked; GEO_002 = the region could not be determined and the signup gate fails closed. Identical user-facing message by design; nothing to fix client-side.

### Rate limiting (RATE)

| Code     | Message                                  | HTTP |
|----------|------------------------------------------|------|
| RATE_001 | Rate limit exceeded. Please slow down.   | 429  |

Responses include `Retry-After` plus `penalty_level` (`warned` / `throttled` / `throttled_hard` / `blocked`) and `violations` count. Progressive penalties escalate within a 15-minute window.

### Internal (INTERNAL)

| Code         | Message                                      | HTTP |
|--------------|----------------------------------------------|------|
| INTERNAL_001 | Something went wrong. Please try again.      | 500  |

### Challenges (CHALLENGE)

These codes are returned in the response body's ``detail`` object (not the top-level ``error_code`` enum above) — the challenges API still uses ``HTTPException(detail=...)`` rather than the structured ``AppError`` framework. Inspect ``response.json()["detail"]["error_code"]`` to dispatch on them.

| Code                                | Meaning                                                                                                                                       | HTTP |
|-------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|------|
| CROSS_DIVISION_RANKED_NOT_ALLOWED   | Rated ``POST /challenges`` between a ``sandboxed`` bot and an ``external_api`` bot. Set ``rated=false`` to play across divisions (exhibition).| 400  |

---

## 7. A "good" error response from your bot

The reference pattern, taken from [`examples/reference-bot/bot.py`](../examples/reference-bot/bot.py):

```python
async for raw in ws:
    msg = json.loads(raw)
    mtype = msg.get("type")

    if mtype == "ping":
        # Heartbeat: respond within 5000ms or the server closes you.
        await send_json(ws, {"type": "pong", "match_id": match_id})

    elif mtype == "turn_request":
        request_id = msg["request_id"]
        valid = msg.get("valid_actions", [])
        try:
            action = decide(msg["state"], valid)
        except Exception:
            # Never let an exception in your strategy code leak — fall back
            # to a legal action and keep the loop going.
            action = {"action": "check" if "check" in valid else "fold",
                      "params": {}}
        await send_json(ws, {
            "type": "turn_action",
            "match_id": match_id,
            "request_id": request_id,   # MUST echo
            "action": action["action"],
            "params": action.get("params", {}),
        })

    elif mtype == "action_rejected":
        # The server rejected our action; retry with the SAME request_id.
        # Prefer the server-supplied valid_actions when present (v0.3.53+);
        # fall back to check/fold otherwise.
        request_id = msg["request_id"]
        valid = msg.get("valid_actions") or ["check", "fold"]
        safe = "check" if "check" in valid else "fold"
        log(f"action_rejected: {msg.get('reason')} -> retrying with {safe}")
        await send_json(ws, {
            "type": "turn_action",
            "match_id": match_id,
            "request_id": request_id,
            "action": safe,
            "params": {},
        })

    elif mtype == "action_timeout":
        # Informational only. The server already auto-acted.
        log(f"timeout; server applied: {msg.get('auto_action')}")

    elif mtype == "error":
        # Not all errors are fatal — log and keep reading.
        log(f"error [{msg.get('code')}]: {msg.get('message')}")

    elif mtype == "match_end":
        log(f"match ended: {msg.get('reason')}")
        break

    else:
        # Forward-compat: silently ignore unknown message types.
        pass
```

Key rules:

1. **Echo `request_id`.** Every `turn_action` must carry the exact `request_id` from the `turn_request` it answers. The same `request_id` is also used for retries after `action_rejected`.
2. **Always pick from `valid_actions`.** Never invent actions, never assume a particular set. The server is authoritative.
3. **One `turn_action` per `turn_request`.** Don't pre-emptively submit. Wait for the prompt.
4. **`pong` only in response to `ping`.** Never send `pong` proactively.
5. **Catch your own exceptions.** A crash in `decide()` becomes a timeout (auto-fold) on the server side and a `bot_decision_timeout` `bot_error` event for the human. Wrap your strategy code in `try/except` and fall back to `check`/`fold`.

## See also

- [`COMMON-PITFALLS.md`](COMMON-PITFALLS.md) — failure-mode catalogue for
  bot developers. Each pitfall cross-references the `bot_error.reason`
  or HTTP error code it surfaces under, so you can jump from a stack
  trace to a fix.
- [`DEV-MANUAL.md`](DEV-MANUAL.md) §9 — live-match troubleshooting
  patterns.
- [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) §12 — wire
  error envelope shape.
6. **Silently ignore unknown message types.** New types may be added in minor protocol versions — rejecting them will break you on upgrades. See [TRANSPORT-PROTOCOL §16.1](protocol/TRANSPORT-PROTOCOL.md#161-unknown-message-types).
7. **Don't retry on close codes.** Once the WebSocket closes with a 4xxx code, the match is gone for you. Re-acquire a ticket and join a new match instead.

JavaScript and Rust starters at [`examples/reference-bot-js/bot.js`](../examples/reference-bot-js/bot.js) and [`examples/reference-bot-rust/src/`](../examples/reference-bot-rust/src/) follow the same shape.

---

## 8. Reporting a bug

When something goes wrong that this catalogue doesn't explain:

1. Capture the `match_id` and approximate UTC timestamp.
2. For HTTP errors: include `error_code` and `request_id` from the response.
3. For WebSocket errors: include the close code (or the full `error` / `action_rejected` payload), the last `seq` you saw, and any `request_id` involved.
4. For bot-side failures: include the per-match log file from `data/bot_logs/<match_id>-<participant_id>.log` and the bot dashboard URL.
5. File in Discord `#feedback` or via the in-app feedback button.

---

*This catalogue is generated from the protocol spec and the validator source. If you spot a code emitted in the wild that isn't documented here, please file an issue — the catalogue should be exhaustive.*
