# Chipzen Bot Developer Manual

Comprehensive reference for building, testing, debugging, and deploying bots
on the Chipzen platform. This manual is the canonical entry point for
everything beyond the one-page quickstart.

> **Looking for the 10-minute walk-through?** Start at
> [`QUICKSTART.md`](QUICKSTART.md). Come back here when you need
> real reference material: full SDK surface, Layer 1/Layer 2 protocol
> details, debugging tools, performance budgets, containerization, and
> troubleshooting.

## Table of Contents

1. [Quickstart pointer](#1-quickstart-pointer)
2. [SDK reference](#2-sdk-reference)
3. [Protocol](#3-protocol)
4. [Testing your bot](#4-testing-your-bot)
5. [Debugging a live match](#5-debugging-a-live-match)
6. [Performance](#6-performance)
7. [Containerization](#7-containerization)
8. [Platform rules](#8-platform-rules)
9. [Troubleshooting](#9-troubleshooting)
10. [Getting help](#10-getting-help)

---

## 1. Quickstart pointer

If you have not yet built your first bot, stop here and do the quickstart
first. It takes roughly 10 minutes and produces a working upload on a local
Chipzen stack. This manual assumes you have already worked through it at
least once.

- **[`QUICKSTART.md`](QUICKSTART.md)** — build the reference
  check-fold bot, tweak one line, upload, play.

Once you understand the upload → play loop, the rest of this manual tells
you how to make the bot *good*.

---

## 2. SDK reference

The Python SDK is published as `chipzen-bot` on PyPI. The public surface is
intentionally small: one abstract base class, one `GameState` dataclass, one
`Action` dataclass, and a runner.

Source of truth for the signatures in this section is the published
`chipzen-bot` package on PyPI.

JavaScript and Rust starters under [`starters/`](../starters/) implement the
same protocol over raw WebSockets. This manual focuses on the Python SDK
because it is the reference implementation; the wire format and lifecycle
are identical across languages.

### 2.1 The three-layer conceptual model

When you build a Chipzen bot there are three distinct pieces:

```
  your underlying AI  ->  your Bot subclass  ->  the SDK runtime (WebSocket)
```

1. **The SDK runtime** (this package). Game-agnostic. Handles WebSocket
   connect, the `authenticate` + `hello` handshake, envelope parsing,
   `request_id` echoing, `ping`/`pong`, `action_rejected` retries, and
   reconnect.
2. **Your `Bot` subclass** (the "adapter"). You override `decide()` and,
   optionally, lifecycle hooks. This is normally <200 lines.
3. **Your underlying AI.** Whatever you want: hand-tuned rules, a neural
   net, CFR, LLM + search. The only contract is that `decide()` returns
   within the timeout.

### 2.2 `Bot` lifecycle hooks

Subclass `chipzen.Bot` (alias for `chipzen.bot.ChipzenBot`). Every method
below except `decide()` is optional and defaults to a no-op. Signatures are
quoted verbatim from `chipzen.bot.ChipzenBot` in the published SDK.

#### `decide(state: GameState) -> Action` — required

```python
@abstractmethod
def decide(self, state: GameState) -> Action:
    """Return your action given the current game state."""
```

Called every time the server sends a `turn_request` for your seat.

- **Return contract:** one of `Action.fold()`, `Action.check()`,
  `Action.call()`, `Action.raise_to(amount)`, `Action.all_in()`. The
  `action.action` string must be present in `state.valid_actions`; if
  raising, `amount` must satisfy `state.min_raise <= amount <=
  state.max_raise`. Both are **total bet sizes**, not increments above the
  current bet.
- **Timeout:** you must return within `turn_timeout_ms` (announced in
  `match_start`, default 5000 ms — see §6). Exceptions are caught by the
  SDK, which sends `fold` on your behalf and logs the traceback at
  `ERROR`.
- **Re-entrancy:** `decide` is never called concurrently with itself for a
  given `Bot` instance. All SDK hooks run on the same asyncio task.

#### `on_match_start(match_info: dict) -> None`

Called once when the `match_start` message arrives.

- `match_info` is the full Layer 1 message. The most useful fields are
  `match_info["seats"]` (array of seat assignments, each with an `is_self`
  flag), `match_info["game_config"]` (blinds, starting stack, total hand
  count), and `match_info["total_hands"]` if present.
- Typical use: initialize per-match state (opponent model, stack
  trackers, per-match Monte Carlo caches).

#### `on_round_start(message: dict) -> None`

Called at the start of every hand with the raw `round_start` message.

The default implementation parses the hole cards and delegates to
`on_hand_start`. Override `on_round_start` directly if you need the Layer 1
envelope (`round_id`, `round_number`) or the full Layer 2 `state` payload
(`deck_commitment`, per-seat `stacks`, `dealer_seat`).

```python
def on_round_start(self, message: dict) -> None:
    state = message.get("state", {}) or {}
    hand_number = int(state.get("hand_number", 0))
    hole_strs = state.get("your_hole_cards", [])
    hole_cards = [Card.from_str(c) for c in hole_strs]
    self.on_hand_start(hand_number, hole_cards)
```

#### `on_hand_start(hand_number: int, hole_cards: list[Card]) -> None`

Convenience wrapper around `on_round_start`. Override this if you only care
about the hand number and the two hole cards. Use for resetting per-hand
trackers.

#### `on_phase_change(message: dict) -> None`

Called when the flop, turn, or river is dealt (`phase_change` message). The
Layer 2 payload is `message["state"]` with `"phase"` and the current
`"board"`. Useful for triggering expensive postflop planning work
*between* your turns, not inside `decide`.

Default: no-op.

#### `on_turn_result(message: dict) -> None`

Called after every participant's action is broadcast — yours and every
opponent's. The Layer 2 payload is `message["details"]` with `seat`,
`action`, `amount`, plus `is_timeout: bool` at the top level. Use for
opponent modeling, timing analysis, or stack tracking.

Default: no-op. Note: this hook runs *before* the next `turn_request` is
dispatched, and it runs serially — see §6 for why slow `on_turn_result`
work eats into your decide budget.

#### `on_round_result(message: dict) -> None`

Called when a hand ends (`round_result` message). Default parses
`message["result"]` and delegates to `on_hand_result`. Override if you
want the full envelope or the `deck_reveal` payload for RNG verification.

#### `on_hand_result(result: dict) -> None`

Convenience wrapper. `result` is the Layer 2 `result` object: `winner_seats`
(array of ints — may be multiple on split pots), `pot`, `payouts`,
`showdown` (array of `{seat, hole_cards, hand_rank}` — present only on
showdown hands), `action_history`, `stacks` (post-payout), `deck_commitment`.

#### `on_match_end(results: dict) -> None`

Called once when the match ends. `results` is the full `match_end` message
with final standings. Good place to flush opponent-model caches or write a
summary to your bot's log.

### 2.3 `GameState`

Built from the `turn_request` message. The full definition is in
`chipzen.models` in the installed SDK:

| Field | Type | Description |
|---|---|---|
| `hand_number` | `int` | 1-indexed within the match. |
| `phase` | `str` | `"preflop"` / `"flop"` / `"turn"` / `"river"`. |
| `hole_cards` | `list[Card]` | Your two hole cards. |
| `board` | `list[Card]` | 0, 3, 4, or 5 community cards. |
| `pot` | `int` | Total chips in the pot (includes all prior bets this hand). |
| `your_stack` | `int` | Your remaining stack. |
| `opponent_stacks` | `list[int]` | Opponents' remaining stacks, ordered by seat, excluding yours. |
| `your_seat` | `int` | 0-based. |
| `dealer_seat` | `int` | Button seat. |
| `to_call` | `int` | Chips to call. `0` means you can check. |
| `min_raise` | `int` | Minimum legal raise-to (total bet). `0` if raising is illegal. |
| `max_raise` | `int` | Maximum legal raise-to (your effective all-in). `0` if raising is illegal. |
| `valid_actions` | `list[str]` | Strings from `{"fold","check","call","raise","all_in"}`. |
| `action_history` | `list[dict]` | All actions this hand, chronological, including synthetic blind/ante entries. |
| `round_id` | `str` | Layer 1 round UUID. Empty in the local test harness. |
| `request_id` | `str` | Layer 1 turn request ID. Empty in the local test harness. |

`Card` is a `(rank, suit)` frozen dataclass; `Card.from_str("Ah")` parses
the wire format; `str(card)` renders it back.

### 2.4 `Action`

```python
Action.fold()
Action.check()
Action.call()
Action.raise_to(amount)   # amount is total bet, not increment
Action.all_in()
```

The SDK serializes these into the two-layer wire format. Raise amounts are
total bets; the server rejects `min_raise <= amount <= max_raise`
violations with `action_rejected` (see §3.3).

### 2.5 Runner — connecting to a live server

For any language, the runner drives the whole lifecycle:

```python
import asyncio
from chipzen import Bot, GameState, Action
from chipzen.client import run_bot

class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()

asyncio.run(run_bot(
    url="ws://localhost:8001/ws/match/<match_id>/<participant_id>",
    bot=MyBot(),
    token="...",          # or ticket=...
    client_name="my-bot",
    client_version="0.1.0",
))
```

Signature (quoted from `chipzen.client.run_bot`):

```python
async def run_bot(
    url: str,
    bot: ChipzenBot,
    *,
    max_retries: int = 3,
    token: str | None = None,
    ticket: str | None = None,
    match_id: str | None = None,
    client_name: str = "chipzen-sdk",
    client_version: str = "0.2.0",
) -> None:
```

The runner handles `authenticate`, version negotiation, the envelope
sequence check, `ping`/`pong`, `action_rejected` fallback, `reconnected`
with `pending_request`, and clean exit on `match_end`.

### 2.6 CLI (`chipzen-sdk`)

The CLI surface is intentionally small — two commands:

| Command | Purpose |
|---|---|
| `chipzen-sdk init <name>` | Scaffold a new bot project from a starter template. |
| `chipzen-sdk validate <path>` | Run the same checks the upload pipeline runs: size, entry point, imports, decide() timeout sniff. The supported go/no-go before docker packaging. |

Both commands are documented in `chipzen-sdk <command> --help`.

---

## 3. Protocol

You normally should not need this section — the SDK handles the protocol.
Read it when writing a non-Python client, debugging wire-level issues, or
reviewing a bot rejection reason.

**Authoritative specs** (binding; this manual is a summary):

- Layer 1 (Transport) — [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md)
- Layer 2 (Poker) — [`protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)

If this manual contradicts those docs, those docs win.

### 3.1 Two layers

- **Layer 1 — Transport.** Game-agnostic. Covers connection, authentication,
  message envelope (`seq`, `match_id`, `server_ts`, `request_id`), timing
  (`turn_timeout_ms`, 15 000 ms heartbeat interval), `action_rejected`,
  `action_timeout`, `session_control`, reconnect.
- **Layer 2 — Poker.** NLHE-specific payloads nested inside Layer 1
  messages: `match_start.game_config`, `round_start.state`,
  `turn_request.state`, `turn_result.details`, `phase_change.state`,
  `round_result.result`.

### 3.2 Envelope fields (Layer 1)

Every server-to-bot message carries:

| Field | Type | Description |
|---|---|---|
| `type` | `string` | Message type. |
| `match_id` | `string` (UUID v4) | The match. |
| `seq` | `integer` | Monotonic per connection, starts at 1. |
| `server_ts` | `string` | ISO 8601 UTC, ms precision. |

Turn messages additionally carry `request_id` (string). **You must echo
`request_id` verbatim in the matching `turn_action`.** The server uses it
for correlation, idempotency, and `action_rejected` retries.

Bot-to-server messages carry `type` and `match_id` (plus `request_id` on
`turn_action`).

### 3.3 Error handling

Two wire errors you need to handle (the SDK handles both; this is for
your information):

- **`action_rejected`.** Server rejected your action (invalid amount, wrong
  action string, stale `request_id`, etc.). Payload includes `reason`,
  human-readable `message`, `remaining_ms`, and the echoed `request_id`.
  Retry with the **same** `request_id` while `remaining_ms > 0`. The SDK
  falls back to `check`-or-`fold`, which is always legal when the server
  offered a turn.
- **`error`.** Server-initiated error (not tied to an action). Payload
  includes `code`, `message`. The SDK logs at `ERROR`. Not all errors are
  fatal — keep reading messages.

The full error-code taxonomy is sourced from the TRANSPORT spec at
[`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md).

### 3.4 Action vocabulary (Layer 2)

`turn_action.action` must be one of:

| String | `params` |
|---|---|
| `fold` | `{}` |
| `check` | `{}` |
| `call` | `{}` |
| `raise` | `{"amount": <int>}` where `min_raise <= amount <= max_raise` |
| `all_in` | `{}` |

**Raise amounts are total bet sizes, not increments.** `raise` with
`amount: 60` means "make the total bet 60", regardless of what you had
already posted.

Synthetic actions (`post_small_blind`, `post_big_blind`, `post_ante`)
appear in `action_history` only — the server generates them. Do not submit
them from a bot.

---

## 4. Testing your bot

The SDK's job here is narrow and explicit: give you a fast **go/no-go
conformance check** so you can be confident your bot will be accepted by
the upload pipeline and survive its first hand without an obvious wire
error. Bot strength testing — playing your bot against opponents to
measure win rate — is **not** an SDK concern. The platform runs
comprehensive bot-vs-bot evaluation after upload.

### 4.1 What `validate` checks

```bash
chipzen-sdk validate ./my_bot/
```

`validate` runs the same pre-upload checks the platform performs and
exits non-zero on any failure. Categories:

- **Image / size budget** — bot directory size, presence of an entry
  point, no source files larger than the per-tier upload cap.
- **Imports** — your bot module imports cleanly under the same Python
  version the platform uses, with only the SDK + your stated deps on the
  path.
- **Sandbox-blocked modules** — your code doesn't import modules the
  runtime sandbox forbids (disk-mutating utilities, network stacks
  outside `websockets`, subprocess spawners, etc.). Failing here means
  the bot would `bot_container_failed_to_attach` in production.
- **`decide()` timeout sniff** — invokes `decide()` once with a canned
  state and warns if the call took longer than 100 ms (warn) or 500 ms
  (fail) on the test machine. Real production budget is per-tier (§6.2).

### 4.2 Connectivity smoke test

```bash
chipzen-sdk validate ./my_bot/ --check-connectivity
```

Layered on top of 4.1: spins up an in-process mock WebSocket server and
drives your bot through:

- The Layer 1 handshake (`authenticate` / server `hello` / client
  `hello` / `supported_versions`).
- A canned Layer 2 hand: `hand_start` → `turn_request` → `turn_action`
  echo-back → `phase_change` → `turn_result` → `round_result` → `game_end`.
- Adversarial inputs: a malformed envelope, an `action_rejected` to
  exercise your retry path, a deliberate timeout overrun, an unknown
  message type.

The mock judges only protocol conformance — never strategy. A pass means
"the upload pipeline will not reject this on protocol grounds"; it does
not mean "your bot is good".

This is the supported test surface. Run it as the final step before
`docker build`.

---

## 5. Debugging a live match

When your bot is running on the platform (local, staging, or prod), you
have five observability surfaces.

### 5.1 Per-match container log

Stdout + stderr of your bot's container is captured to:

```
data/bot_logs/<match_id>-<participant_id>.log
```

(Configurable via `settings.bot_log_dir`; capped at `bot_log_max_bytes`
= 2 MiB by default, further output is dropped with a `[truncated]`
marker.)

Raw access: `tail -f data/bot_logs/<match_id>-<pid>.log`.

UI access: after the match ends, open the match in your replay list,
scroll to the "Developer tools" row, click "Bot logs · seat N" to open
the log drawer. A "Decisions" tab parses the log into a timing table.

### 5.2 `DECIDE` trace format

If your adapter opts in, you can emit one structured INFO line per
decision. The platform's log parser recognizes this format:

```
DECIDE match=<id> hand=<n> phase=<preflop|flop|turn|river> \
  board=<cards> hole=<cards> stack=<int> pot=<int> to_call=<int> \
  legal_cz=[fold,call,raise] legal_pb=[fold,call,raise_0.33pot,...] \
  pb_label=<label> cz_action=<wire_action> \
  elapsed_ms=<float> drain_ms=<float>
```

Key timing fields:

- **`elapsed_ms`** — your `decide()`'s total wall time.
- **`drain_ms`** — time spent processing queued `on_turn_result`
  / `on_phase_change` events before search could start. If this is a
  sizable fraction of `elapsed_ms`, your pre-decide bookkeeping is your
  bottleneck, not your strategy code.

When the adapter can't use the chosen label and falls back, you get a
WARN line with the same shape plus a `reason=` field:

```
FALLBACK match=<id> hand=<n> phase=<...> reason=<...> \
  cz_action=<safe_action> elapsed_ms=<float>
```

Write your own bots to the same kv format if you want the UI parser to
pick them up.

### 5.3 Server-side round-trip line

Emitted by the game engine at `WARN` when the round-trip exceeds 80% of
the budget, otherwise `DEBUG`:

```
Bot turn_action round-trip for match <short_id>: <rt_ms>ms (budget=<budget_ms>ms)
```

This measures from server sending `turn_request` to receiving
`turn_action`, so it includes: network WS roundtrip + SDK queue drain +
your `decide()` + WS serialize back. The server's per-match-type decision
timeout (§6) is what this is measured against.

The round-trip lines go to the API process's own stdout/structlog, not
the per-match container log. Grep the API logs for `round-trip for match
<short_match_id_prefix>`.

### 5.4 Parsed-decisions tab

The "Decisions" tab in the log drawer renders `DECIDE` / `FALLBACK` rows
joined with server-side round-trip lines into a table:

| Hand | Phase | Action | `elapsed_ms` | `drain_ms` | `round_trip_ms` |

Rows where `round_trip_ms > 0.8 * budget_ms` highlight amber; rows where
`elapsed_ms > timeout_ms` (fallback fired) are red. Backend source: `GET
/matches/{match_id}/bots/{participant_id}/logs?format=parsed`.

### 5.5 `bot_error` WebSocket event

When your bot fails visibly (container never attached, disconnected
mid-match, decision timeout, invalid action, server-side exception), the
server sends a `bot_error` message to the human's UI so the human knows
they are not playing against the bot's real strategy. Structure:

```json
{
  "type": "bot_error",
  "reason": "bot_decision_timeout",
  "message": "my-bot did not respond within 5000ms.",
  "hand_number": 3,
  "phase": "flop",
  "match_continues": true
}
```

Reasons:

| `reason` | Meaning |
|---|---|
| `bot_container_failed_to_attach` | Container didn't send `hello` before the attach budget. |
| `bot_connector_disconnected_midmatch` | WebSocket closed before match end. |
| `bot_decision_timeout` | No `turn_action` within `bot_decision_timeout_ms`. |
| `bot_invalid_action` | Malformed action, wrong `request_id`, or not in `valid_actions`. |
| `bot_exception` | Server-side exception in the bridge (rare). |

After three consecutive errors in one match, the server aborts with
`reason=bot_failed`. If you see this repeatedly, file an issue — the
server should make the underlying reason loud enough that you don't
have to guess.

### 5.6 Per-bot dashboard

Aggregated cross-match view of timing, fallback counts, and `bot_error`
events. Use the per-match drawer plus the API endpoint in §5.4.

---

## 6. Performance

Your `decide()` must respond end-to-end within the **per-match-type
decision timeout** (§6.2 below). The timeout covers everything from
server-side send to server-side receive, not just your Python body — see
the round-trip breakdown below.

### 6.1 Budget breakdown (observed)

Measured on a non-trivial bot adapter over real matches:

| Component | Typical | Notes |
|---|---|---|
| Network WS hop (send) | 10-50 ms | Same-host containers faster, cross-AZ slower. |
| SDK message queue drain | **100 ms - 3 s** | `on_turn_result` / `on_phase_change` run serially before the next `turn_request` — see §6.3. |
| Your `decide()` body | your budget | Whatever is left. |
| Network WS hop (return) | 10-50 ms | |
| Server-side bookkeeping | 5-20 ms | Envelope, validation, broadcast. |

If you see server-side `round-trip = 3-4 s` with your own `decide()`
returning in 100 ms, your queue drain is the culprit — your hooks are
doing more work than you think.

### 6.2 Decision timeout by match type

The server resolves your bot's per-decision timeout by **match type**,
not by user tier:

| Match type | Server field | Default |
|---|---|---|
| Ranked bot-vs-bot challenges | `bot_match_decision_timeout_ms` | 2000 ms |
| Tournament bot-vs-bot | `bot_tournament_decision_timeout_ms` | 2000 ms |
| Human-vs-bot (free `/play`) | `bot_decision_timeout_ms` | 5000 ms |
| Absolute fallback (any path with no per-match-type knob) | `bot_decision_timeout_ms` | 5000 ms |

Bot-vs-bot is intentionally tighter than human-vs-bot — competitive play
demands snappier decisions; free human-vs-bot play allows the bot extra
headroom for pre-decide queue drain (§6.3) so the experience stays
forgiving for in-development bots.

> Earlier versions of this manual referenced a per-user-tier table
> (`tier_decision_timeout_ms_{free,pro,elite}`). Those knobs were
> removed in favor of match-type resolution; tier no longer affects
> decision timeout.

### 6.3 Why the queue drains first

The SDK client processes WebSocket messages serially on a single asyncio
task. When a `turn_request` arrives after a burst of `turn_result` /
`phase_change` messages (which happens every time an opponent acts or the
board changes), the SDK calls your `on_turn_result` / `on_phase_change`
hooks *before* dispatching the `turn_request` to `decide()`. If those hooks
do meaningful work (e.g., belief-tracker updates), the queue drain can eat
your budget.

Two fixes:

1. **Keep hooks cheap.** Do not run expensive inference inside
   `on_turn_result`. Queue the work and process it lazily in `decide()`.
2. **Run your own `ThreadPoolExecutor`** for genuinely parallel work. The
   SDK does not start threads for you; if you spawn one, you own its
   lifecycle. `loop.run_in_executor` or `asyncio.to_thread` are both fine.

If your hooks are slow, the standard pattern is to move the expensive work
into a background task (`asyncio.create_task` or a `ThreadPoolExecutor`)
queued from the hook, then read its result lazily inside `decide()`. That
way `decide()` starts immediately when `turn_request` arrives.

### 6.4 Tips for staying under budget

- **Return synchronously from `decide()`.** Starting an `asyncio.Task` or
  a thread inside `decide()` to do the real work does not help — the SDK
  waits for `decide()`'s return value.
- **Profile once, not every hand.** Cache expensive lookups (preflop range
  charts, solver tables) at `on_match_start` time.
- **Watch `drain_ms`.** If it's > 20% of `elapsed_ms`, fix the hooks, not
  `decide()`.
- **Respect the match-type ceiling** (§6.2). The server safe-defaults
  (`check` if legal, else `fold`) on timeout and sends `bot_error` with
  `reason=bot_decision_timeout` to the human. Your bot appears to fold
  to every raise — a confusing failure mode if you're not watching the
  logs, because the bot looks like it's playing badly rather than timing
  out. Always check `DECIDE` traces or the per-match log drawer when a
  bot's decisions look unexpectedly passive.

---

## 7. Containerization

Chipzen accepts pre-built container images (`docker save | gzip >
bot.tar.gz`), not source code. The upload pipeline retags, stores the image
in private container storage, and runs a smoke test before approval.

### 7.1 Required contract

Your image must:

1. **Have an `ENTRYPOINT`** that runs your bot. `CMD` is ignored by the
   executor; `ENTRYPOINT` is not. See [`examples/reference-bot/Dockerfile`](../examples/reference-bot/Dockerfile):
   ```dockerfile
   ENTRYPOINT ["python", "-u", "/bot/bot.py"]
   ```
   `-u` (unbuffered) is important — without it your stdout/stderr can
   be lost when the container exits.
2. **Read `CHIPZEN_WS_URL` and `CHIPZEN_TOKEN`** (or `CHIPZEN_TICKET`)
   from the environment. The executor injects these at launch. Optional:
   `CHIPZEN_MATCH_ID` (the SDK extracts it from the URL by default).
3. **Connect within the attach budget.** The server waits up to ~15 s for
   your container to send its `hello`. Images loading large models can
   trip this — see §9.
4. **Run as non-root if possible.** The executor layers `--user 10001:10001`,
   `--read-only`, `--cap-drop=ALL`, `--security-opt seccomp=…` on top,
   but a non-root `USER` in your Dockerfile is defense in depth.
5. **Treat `/tmp` as your only writable path.** The bot-match container
   runtime runs with a read-only root filesystem. `/tmp` is mounted on a
   small ephemeral tmpfs scratch volume — it's the only place your bot
   can write. Anywhere else (`/app`, `/home`, `/var`) will raise
   `OSError: [Errno 30] Read-only file system`. The tmpfs goes away when
   the task stops, so don't put state there you need across matches.

### 7.2 Resource limits per tier

Current per-tier resource caps:

| Resource | Free | Pro | Elite |
|---|---|---|---|
| Upload size (compressed) | 5 MB | 25 MB | 100 MB |
| CPU cores | 0.5 | 1.0 | 2.0 |
| Memory | 256 MB | 512 MB | 1024 MB |
| tmpfs `/tmp` | 10 MB | 50 MB | 200 MB |
| Decision timeout (ranked) | 500 ms | 1000 ms | 2000 ms |
| Max bots per user | 1 | 5 | 20 |

Decompressed image size is capped at 100 MB independent of tier.
Human-vs-bot play uses the global 5000 ms (see §6.2).

### 7.3 Size budget

The reference bot (Alpine + SDK + 60-line `bot.py`) is ~20 MB compressed.
A non-trivial bot with numpy/scipy + a small model checkpoint typically
runs 100–150 MB compressed and may exceed the free tier cap. If you
are hitting the size cap:

- **`python:3.11-alpine` instead of `slim`.** ~50 MB base vs ~125 MB.
  Only works with pure-Python deps; numpy/scipy need musl wheels or a
  build chain.
- **Strip pycache and tests.** See
  [`examples/reference-bot/Dockerfile`](../examples/reference-bot/Dockerfile)
  for the pattern.
- **Multi-stage build.** Compile deps in a builder stage, copy only the
  installed packages into the final stage.

### 7.4 Seccomp

The executor layers a seccomp profile on top of Docker's default. Most
syscalls you need are allowlisted, but C extensions that use unusual
syscalls can crash your container silently on startup, *before* Python
even starts — `docker logs` shows nothing.

If your container exits immediately with no logs:

1. Try running locally without `--security-opt seccomp=…` to confirm
   it's the profile. If the bot starts without seccomp but crashes with
   it, that's the symptom.
2. Strace the offending call (`strace -f -e trace=all -o trace.log …`)
   and identify the blocked syscall.
3. File an SDK issue with the minimal repro and the syscall name. The
   maintainers can review the seccomp profile and add common syscalls
   (e.g. `arch_prctl` for glibc-based images) to the allowlist when
   warranted.

A platform startup probe fails launches loudly if the container exits
in under 2 s, so if you see `bot_container_failed_to_attach` errors in
the human UI, step 1 above is the first thing to check.

---

## 8. Platform rules

The canonical Platform Rules document is presented in-app when you
upload a bot, and you must accept it before submission. Read it before
uploading. Summary of the sections most relevant to bot authors:

- **§1.2 Account requirements.** One account per person. API keys and
  session tokens are bound to your account.
- **§3.1 Original work.** Bot code must be yours, or properly licensed.
  Credit third-party solvers / models in your bot description.
- **§3.2 Code standards.** No sandbox escapes, no attacks on other bots
  or platform infra, no miners, no exfiltration.
- **§3.3 Resource limits.** Exceeding your tier's CPU / memory / decision
  timeout means the server applies a safe default. Repeat crashes can
  trigger submission throttling.
- **§4.1 Match format.** Bot-vs-bot is elimination: you play until one
  bot has all chips. Bot-vs-bot matches can have stakes and entry fees.
- **§8.2 License to Chipzen.** By submitting a container image, you grant
  Chipzen a non-exclusive license to run it for platform purposes
  (matches, testing, promotional demos). You retain ownership of the
  source code — we never receive it.

### 8.1 Free-play vs ranked

This boundary is hard-coded in the platform:

- **Human-vs-bot** matches (the `/play` routes) are **always free**. No
  stakes, no entry fees, no prizes. Used for spectators, training, and
  human engagement.
- **Bot-vs-bot** matches (the `/challenges` routes, tournaments) **can**
  have stakes and entry fees.

These are separate systems with separate APIs. Your bot doesn't need to
care which one it's in — the WebSocket protocol is the same.

### 8.2 Slot model

Free tier gets **3 bot slots** (from `tier_max_bots_free = 3` in
config). Pro: 5. Elite: 20.

Uploads are not versioned: each upload creates a new independent bot.
To upload a fourth bot on the free tier, delete one first. The backend
enforces the cap with HTTP 409.

### 8.3 Lifecycle in one glance

After upload your bot moves through these states:

```
uploading -> pending_review -> reviewing -> approved -> active
                                            |   ^
                                            |   | PUT /bots/{id}/activate
                                            v   | PUT /bots/{id}/deactivate
                                          rejected
```

- `approved` — free-play eligible (your bot card shows "Play").
- `active` — promoted to ranked matchmaking. Only one of your bots
  can be `active` at a time; activating another one deactivates
  the previous.

The full state table and the `status`/`is_active` invariant are
documented on the developer site (link in the README).

---

## 9. Troubleshooting

Each pattern lists the *specific log line or UI indicator* that
identifies it, so you don't have to guess.

### 9.1 "My bot got rejected during review"

**Where it shows up:** Bot card flips from `reviewing` to `rejected` in
the Developer UI. Click "Show rejection reason" — it's in
`bot.rejection_reason`.

**Common reasons:**

- `Image exceeds <N> MB compressed` — you're over the tier upload cap
  (§7.2). Use Alpine + wheels-only + strip pycache.
- `Smoke test failed: no hello within 10s` — the bot never completed the
  `authenticate` + `hello` handshake. Check `ENTRYPOINT`, check
  `CHIPZEN_WS_URL` parsing, check `python -u`.
- `Smoke test failed: invalid action` — the bot sent an action not in
  `valid_actions`, or a raise outside `[min_raise, max_raise]`. Run
  `chipzen-sdk validate` locally; it catches most of these.
- `Image scan failed: <cve>` — ECR found a known vulnerable package.
  Rebuild from a patched base image.

### 9.2 "My bot always folds / server shows safe default"

**Where it shows up:** Match plays normally but your bot appears to fold
(or check) at every decision. In the API log you see one or more of:

```
WARN safe_default fold ...
Bot turn_action round-trip for match <id>: 5001.3ms (budget=5000ms)
```

**Root cause:** your `decide()` is timing out. The server applies the
safe default (§6) and sends `bot_error` with
`reason=bot_decision_timeout` to the human.

**Fix:**

1. Open the match replay → log drawer → Decisions tab. If `elapsed_ms >
   5000`, your `decide()` body is slow. If `drain_ms` is a large fraction,
   your `on_turn_result` / `on_phase_change` hooks are slow (§6.3).
2. Time `decide()` directly with your own profiling harness — call your
   `Bot.decide()` against a representative `GameState` you've recorded
   from a real match, in a tight loop, and measure the distribution.
3. If you're using a heavyweight engine (solver, LLM), pre-compute at
   `on_match_start` and cache.

### 9.3 "My `decide()` is timing out"

**Where it shows up:**

- `bot_error` event in the human's UI with
  `reason=bot_decision_timeout`.
- `DECIDE` trace line with `elapsed_ms > 5000` (preceded by `FALLBACK` if
  you emit the pattern).
- Server log: `round-trip for match <id>: 5001.3ms (budget=5000ms)`.

**Diagnosis:** compare `elapsed_ms` (adapter-side) to `round_trip_ms`
(server-side). If they match, fix `decide()`. If `round_trip_ms`
materially exceeds `elapsed_ms`, the gap is WebSocket latency or message
queue drain — see §6.3.

### 9.4 "Bot can't reach the server / WS upgrade fails"

**Where it shows up:**

- Container log: `ConnectionRefusedError` or similar on
  `websockets.connect`.
- API log: no `Launched bot X` followed by a real handshake — just the
  launch line and then `bot_container_failed_to_attach`.

**Common causes:**

- **`ws://127.0.0.1:...` on native Linux with `--network=host`.** The
  container's loopback is not the host's loopback. Use
  `ws://0.0.0.0:...` (bind the API to `0.0.0.0`) or
  `ws://host.docker.internal:...`. Docker Desktop injects
  `host.docker.internal` automatically; native Linux does not.
- **Wrong port.** API defaults to 8001 in dev. Frontend proxies to 8001.
  If you copied a production URL into a local test, the port is wrong.
- **Auth rejection.** Check `CHIPZEN_TOKEN` / `CHIPZEN_TICKET`. Local dev
  often accepts an empty token for the `/bot` endpoint; staging does
  not.

### 9.5 "Container dies immediately with no logs"

**Where it shows up:** `docker inspect <id>` shows `State.ExitCode != 0`
and `State.FinishedAt` within ~1 s of `StartedAt`. `docker logs <id>`
is empty. The platform's `bot_container_failed_to_attach` startup probe
catches this and fails the launch loudly.

**Most common cause:** seccomp blocking a required syscall (§7.4). Try:

```bash
docker run --rm -e CHIPZEN_WS_URL=ws://... my-bot  # no seccomp
docker run --rm --security-opt seccomp=docker/seccomp-bot.json \
  -e CHIPZEN_WS_URL=ws://... my-bot                # with profile
```

If the first works and the second doesn't, the profile is the
culprit. Strace, identify, file an issue.

Other causes:

- **Missing shared library.** Alpine + musl vs glibc mismatch. Use
  `ldd /bot/your_binary` inside the container.
- **Entrypoint not executable.** `chmod +x` or use
  `ENTRYPOINT ["python", ...]` (which doesn't need the exec bit).
- **Glibc TLS abort:** `Fatal glibc error: Cannot allocate TLS block`.
  Glibc-based images need `arch_prctl` (and a small set of related
  syscalls) on the seccomp allowlist; the standard profile includes
  these, but if you see this with a custom profile or a future glibc
  version, file an issue.

---

## 10. Getting help

- **SDK / starter / protocol bugs** —
  [chipzen-ai/chipzen-sdk issues](https://github.com/chipzen-ai/chipzen-sdk/issues).
- **Platform / matchmaking / billing / account issues** —
  email `support@chipzen.ai`. Don't open them on the SDK repo.
- **Security issues** — email `security@chipzen.ai`, do not post
  publicly. See [SECURITY.md](../SECURITY.md).

When filing a bot-runtime bug, the most useful artifacts are:

1. The match ID and your bot's participant ID (visible in the log drawer
   URL).
2. The relevant `DECIDE` / `FALLBACK` lines from
   `data/bot_logs/<match_id>-<pid>.log`.
3. The server-side `round-trip for match …` lines from the API log, if
   you have access to the platform-side logs.
4. A minimal Python reproducer that constructs the relevant `GameState`
   and calls your `Bot.decide()` directly — the smaller the snippet,
   the faster the triage.

---

*Last updated 2026-04-15. Sourced from SDK version 0.2.0. If any example
in this manual doesn't work against the current SDK, file an issue — the
manual is expected to be executable, not aspirational.*
