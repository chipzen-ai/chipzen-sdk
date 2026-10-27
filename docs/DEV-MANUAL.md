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
10. [Big Bad Bots](#10-big-bad-bots)
11. [Getting help](#11-getting-help)

---

## 1. Quickstart pointer

If you have not yet built your first bot, stop here and do the quickstart
first. It takes roughly 10 minutes and produces a working bot uploaded to
[chipzen.ai](https://chipzen.ai). This manual assumes you have already worked through it at
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

Subclass `chipzen.Bot`. Every method below except `decide()` is optional and
defaults to a no-op. Signatures are quoted verbatim from the published SDK.

> The class is also exported under its historical internal name
> `chipzen.bot.ChipzenBot` — they refer to the same object. Always prefer
> `chipzen.Bot` in user code; `ChipzenBot` is kept only for backward
> compatibility with early 0.2.0 imports. If you are translating a bot
> between the Python, JavaScript, and Rust SDKs, see
> [`PORTING-BETWEEN-SDKS.md`](PORTING-BETWEEN-SDKS.md) for the per-language
> name mappings.

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
  flag), and `match_info["game_config"]` — blinds, starting stack, and
  `num_players` (the seat count: 2 for heads-up, up to 6 for multi-way
  tables). Table size is chosen by the platform, so size your logic off
  `num_players` rather than assuming heads-up. Do not branch on a per-match
  hand count — the platform does not expose one.
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
| `opponent_stacks` | `list[int]` | Opponents' remaining stacks, ordered by seat, excluding yours. A LIST: length N-1 at an N-player table, exactly one entry in heads-up. See the multi-player note below before reading `opponent_stacks[0]`. |
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

#### Multi-player tables: a heads-up bot keeps running, but watch `opponent_stacks[0]`

The protocol is multiplayer-shaped already, so the platform can seat your bot at
a 3-6 player table with no protocol-version bump and no code change required to
keep it running. `opponent_stacks` has been a `list[int]` from day one, and the
seat fields (`your_seat`, `dealer_seat`, and `winner_seats` in results) are
already seat-indexed. The catch is behavioral, not a crash:

- **Silent-failure risk.** A bot that hardcodes `opponent_stacks[0]` does NOT
  raise at a larger table, but it reads a SINGLE neighbor's stack instead of the
  whole field. If you meant "the opponents", iterate or aggregate the list
  (`sum(...)`, `min(...)`, `max(...)`, or index by the seat you care about)
  rather than assuming exactly one opponent. Because there is no exception and no
  rejected action, this kind of bug stays invisible until you inspect results.
- **Table size.** Read it from `match_start`'s `game_config.num_players`, or
  derive it from any turn state as `len(opponent_stacks) + 1`.
- **Position.** Derive your seat's offset from the button with
  `seats_after_button = (your_seat - dealer_seat) % num_players` (`0` is the
  button; in heads-up the button is the small blind). The full taxonomy and a
  worked example live in
  [`POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md) Section
  5.9, and the reference starters in `packages/<lang>/starters/` show the
  seat-count-aware pattern (`table_position()` plus opponent iteration).

If your strategy is genuinely heads-up only, that is fine; just gate it on
`len(opponent_stacks) == 1` rather than silently assuming it.

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
from chipzen import Bot, GameState, Action, run_bot

class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()

# Containerized/uploaded bots get their match URL from the platform's executor.
asyncio.run(run_bot(
    url="ws://localhost:8001/ws/match/<match_id>/<participant_id>",
    bot=MyBot(),
    token="...",          # or ticket=...
    client_name="my-bot",
))
```

Signature (quoted from `chipzen.client.run_bot`):

```python
async def run_bot(
    url: str,
    bot: ChipzenBot,
    *,
    max_retries: int | None = None,       # cap reconnect attempts; None = use retry_policy
    retry_policy: RetryPolicy | None = None,  # default: 5 attempts, 500ms→30s backoff
    token: str | None = None,
    ticket: str | None = None,
    match_id: str | None = None,
    client_name: str = "chipzen-sdk",
    client_version: str = __version__,    # defaults to the installed SDK version
    safe_mode: bool = True,               # False: a decide() error raises (exit non-zero)
    user_agent: str | None = None,        # default: chipzen-sdk-python/<version>
) -> dict | None:                         # the match_end payload, or None on a clean drop
```

The runner handles `authenticate`, version negotiation, the envelope
sequence check, `ping`/`pong`, `action_rejected` fallback, `reconnected`
with `pending_request`, and clean exit on `match_end`.

To run a bot **remotely from your own machine** instead of uploading a
container, use `run_external_bot` — see §2.7.

### 2.6 CLI (`chipzen-sdk` / `chipzen`)

The CLI is installed under two interchangeable names (`chipzen-sdk` and
`chipzen`) with three commands:

| Command | Purpose |
|---|---|
| `chipzen-sdk init <name>` | Scaffold a new bot project from a starter template. |
| `chipzen-sdk validate <path>` | Run the same checks the upload pipeline runs: size, entry point, imports, decide() timeout sniff (for a Big Bad Bots bot, the four calls against conformance fixtures, §10.8). The supported go/no-go before docker packaging. |
| `chipzen run-external <bot.py>` | Run a bot on the external-API remote-play path (lobby → matched → play). See §2.7. |

Each command is documented in `chipzen-sdk <command> --help`.

### 2.7 External-API remote play (`run_external_bot`)

The runner in §2.5 is the **containerized/upload** path — the platform's
executor hands your container a `/ws/match/...` URL. The SDK also supports
**external-API remote play**: run your bot on your own machine, authenticate
with a long-lived `cz_extbot_` token, and the platform matches and dispatches
you like any other competitor. **The same `Bot` subclass works on both paths.**

```python
import asyncio
from chipzen import Bot, run_external_bot

asyncio.run(run_external_bot(MyBot(), bot_id="<bot-uuid>", env="prod",
                             token="cz_extbot_..."))
```

`run_external_bot` holds one lobby connection and plays every match dispatched
to your bot — a single challenge, or each round of a tournament — and
reconnects a dropped match via the platform's reconnect-resume. Put the token in
a `chipzen.toml` and the CLI is a one-liner:

```toml
# chipzen.toml  (discovered from cwd, then ~/.chipzen/, then /etc/chipzen/)
[external_api]
token  = "cz_extbot_..."
bot_id = "<bot-uuid>"
```

```bash
chipzen run-external my_bot.py   # targets prod (chipzen.ai) by default
```

Knobs: `connect_to_chipzen(bot_id, env)` (env → lobby URL), `RetryPolicy`
(reconnect/backoff pacing), `safe_mode=False` (crash on a `decide()` bug instead
of folding — for dev/eval), and `max_matches` (stop after N). The full wire
protocol is in
[`docs/EXTERNAL-API-BOT-PROTOCOL.md`](EXTERNAL-API-BOT-PROTOCOL.md); the
step-by-step walkthrough is
[`docs/external-api/FIRST-30-MINUTES.md`](external-api/FIRST-30-MINUTES.md).

> **Connected lobby time is metered.** On the free tier each account gets 8
> hours of connected lobby time per UTC day, shared by all of its remote bots.
> The clock runs whenever a bot's lobby connection is open, even while it is
> idle. Connect when you want to play or test, and disconnect afterwards. See
> [§8.2.2](#822-remote-bots-daily-lobby-allowance).

### 2.8 Playing other games

Chipzen runs more than one game. A bot plays No-Limit Hold'em unless it
declares otherwise.

| Game | `game_type` | Status | Layer 2 spec |
|---|---|---|---|
| No-Limit Hold'em (heads-up and multi-way) | `poker` | Live | [`POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md) |
| 2-7 Triple Draw | `draw27` | Live | [`DRAW27-GAME-STATE-PROTOCOL.md`](protocol/DRAW27-GAME-STATE-PROTOCOL.md) |
| Pineapple OFC | `ofc` | Live | [`OFC-GAME-STATE-PROTOCOL.md`](protocol/OFC-GAME-STATE-PROTOCOL.md) |
| Big Bad Bots (robot fighting) | `bigbadbots` | Beta. Launch rule set `bigbadbots-1.2` ([`RULESETS.md`](RULESETS.md)); build guide in §10 | [`BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md) |

[`LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md) covers what every game
shares.

**Declare a game in both places:**

1. **On the connection:** `run_bot(..., supported_games=["poker", "draw27"])`
   or `run_external_bot(..., supported_games=[...])`. In JavaScript:
   `runBot(url, bot, { supportedGames: [...] })` or
   `runExternalBot(bot, { ..., supportedGames: [...] })`. In Rust:
   `supported_games: Some(vec!["poker".into(), "draw27".into()])` on
   `RunBotOptions`, `RunExternalOptions` or `RunExternalArgs`. The list goes on
   the client `hello` and is checked when the match socket opens.
2. **On the bot:** its supported game types (`supported_game_types`) when you
   upload or create it. This decides which tables the bot is offered and
   seated at.

A bot that declares neither is treated as poker only. Declaring a game
means you implement everything in that game's Layer 2 spec: a bot that
declares a game it cannot play is seated, then timed out and substituted
every turn.

**Route each turn from `decide()`.** `decide()` stays the one required
method. The SDK adds two optional hooks that it never calls on its own:
`decide_draw(state)` for a 2-7 Triple Draw draw turn and
`decide_placement(state)` for a Pineapple OFC placement turn. Call them from
`decide()` (in JavaScript: `decideDraw(state)`, branching on
`state.isDrawPhase`, and `decidePlacement(state)`, branching on
`state.validActions.includes("place")`; in Rust: `self.decide_draw(state)`,
branching on `state.is_draw_phase`, and `self.decide_placement(state)`,
branching on `"place"` in `state.valid_actions`):

```python
from chipzen import Action, Bot, GameState

class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "draw" in state.valid_actions:    # 2-7 Triple Draw: draw turn
            return self.decide_draw(state)   # discard up to state.max_discard
        if "place" in state.valid_actions:   # Pineapple OFC: placement turn
            return self.decide_placement(state)
        return self.decide_betting(state)    # your betting logic (any game)
```

The variant fields on `GameState` (`max_discard`, `your_rows`,
`cards_to_place` and the rest) are optional with defaults, so a hold'em-only
bot never sees them.

**Big Bad Bots is different.** Its turns are not betting turns, so a Big Bad
Bots bot does not route from `decide()`: it subclasses `chipzen.BigBadBotsBot`
and implements one method per phase (`choose_body`, `bid`, `fit`, `act`). See
§10, and [`BIGBADBOTS-QUICKSTART.md`](BIGBADBOTS-QUICKSTART.md) for the
walkthrough.

**Support by language.** Python ships every feature first; the JavaScript and
Rust SDKs follow to full parity. Until a row is done, a JavaScript or Rust bot
can still play a game by speaking its Layer 2 spec directly.

| Feature | Python `chipzen-bot` | JavaScript `@chipzen-ai/bot` | Rust `chipzen-bot` |
|---|---|---|---|
| No-Limit Hold'em, heads-up and multi-way | Yes | Yes | Yes |
| Remote play (external API) | Yes | Yes | Yes |
| `supported_games` declaration | Yes (0.4.0) | Yes (`supportedGames`, 0.4.0) | Yes, in `main` (`supported_games`); not yet in a crates.io release |
| 2-7 Triple Draw | Yes (0.4.0) | Yes, in `main` (`decideDraw`); not yet in an npm release | Yes, in `main` (`decide_draw`); not yet in a crates.io release |
| Pineapple OFC | Yes (0.4.0) | Yes, in `main` (`decidePlacement`); not yet in an npm release | Yes, in `main` (`decide_placement`); not yet in a crates.io release |
| Big Bad Bots | Yes (0.5.0, `BigBadBotsBot`, §10) | Yes (0.4.0, `BigBadBotsBot`) | Coming after Python |

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
error. Bot strength testing (playing your bot against opponents to
measure win rate) is **not** an SDK concern. Upload review only checks that
your bot speaks the protocol (a short scripted mini-match); its strength
shows once it plays real matches on the platform: challenges against platform
bots and other builders' bots, tournaments and seasons, with ratings and
replays.

### 4.1 What `validate` checks

```bash
chipzen-sdk validate ./my_bot/
```

`validate` runs the same pre-upload checks the platform performs and
exits non-zero on any failure. Categories:

- **Image / size budget** — bot directory size, presence of an entry
  point, no archive larger than the 250 MB compressed upload cap (§7.2).
- **Imports** — your bot module imports cleanly under the same Python
  version the platform uses, with only the SDK + your stated deps on the
  path.
- **Sandbox-blocked modules** — your code doesn't import modules the
  runtime sandbox forbids (disk-mutating utilities, network stacks
  outside `websockets`, subprocess spawners, etc.). Failing here means
  the bot would `bot_container_failed_to_attach` in production.
- **`decide()` timeout sniff** — invokes `decide()` once with a canned
  state and warns if the call took longer than 100 ms (warn) or 500 ms
  (fail) on the test machine. Real production budget is resolved per match
  type, not per tier (§6.2).

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

When your bot is running on the platform (local or prod), you
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
  "message": "my-bot did not respond within 10000ms.",
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

The server resolves your bot's per-decision timeout by **how the bot is
hosted**, never by user tier:

| Bot / match type | Server field | Default |
|---|---|---|
| Hosted (uploaded container) bots, including ranked and tournament bot-vs-bot | `bot_decision_timeout_ms` | **5000 ms** |
| Remote (External API) bots on the token-initiated paths | `extapi_casual_decision_timeout_ms` | **30000 ms** |

Hosted bots run on the match executor, which enforces
`bot_decision_timeout_ms` (5 s). Remote bots on the casual house-bot
challenge and rated agent-vs-agent paths get the relaxed 30 s clock, sized
for LLM agents (the `matched` notify carries `decision_timeout_ms: 30000`;
see [`EXTERNAL-API-BOT-PROTOCOL.md`](EXTERNAL-API-BOT-PROTOCOL.md)).

The server config also defines `bot_match_decision_timeout_ms` and
`bot_tournament_decision_timeout_ms` (both 2000 ms) and
`bot_human_play_decision_timeout_ms` (10000 ms), but these only feed the
legacy in-API orchestrator path; the executor path that serves hosted bots
does not read them. Do not budget against 2 s or 10 s.

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
   executor; `ENTRYPOINT` is not. (Big Bad Bots images are the exception:
   their ENTRYPOINT is the platform's log-cap wrapper, which runs the bot
   command you put in `CMD`. See §10.9.) See [`examples/reference-bot/Dockerfile`](../examples/reference-bot/Dockerfile):
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
4. **Run as a non-root user (required).** Review rejects a new upload
   whose image runs as root: no `USER` at all, `USER root`, `USER 0`, or
   `USER 0:<gid>`, or a named user whose uid in the image's `/etc/passwd`
   is 0. Create a user in the final stage of your Dockerfile and switch to
   it before the `ENTRYPOINT`:
   ```dockerfile
   RUN useradd -u 10001 -m bot      # Alpine: RUN adduser -D -u 10001 bot
   USER 10001
   ENTRYPOINT ["python", "-u", "/bot/bot.py"]
   ```
   On a distroless or scratch image, `USER 65534` (nobody) is enough; the
   distroless `:nonroot` tags already run as uid 65532. A numeric uid is
   the safest choice because review does not have to look it up. Check
   before you upload: `docker run --rm --entrypoint id <image>` must not
   print `uid=0`. Every starter under `packages/<lang>/starters/` and the
   reference bot already do this. Bots that were already active before
   this rule are not re-reviewed. The platform still layers `--read-only`,
   `--cap-drop=ALL` and seccomp on top (§7.4).
5. **Treat `/tmp` as your only writable path.** The bot-match container
   runtime runs with a read-only root filesystem. `/tmp` is mounted on a
   small ephemeral tmpfs scratch volume — it's the only place your bot
   can write. Anywhere else (`/app`, `/home`, `/var`) will raise
   `OSError: [Errno 30] Read-only file system`. The tmpfs goes away when
   the task stops, so don't put state there you need across matches.

### 7.2 Resource limits

**Tier never changes what your bot gets inside a match.** CPU, memory,
tmpfs, image size, upload size and the decision clock are identical on every
tier: there is no pay-to-win on match performance. Tier only sets how many
bots you can hold and how much volume you can run per day (bot slots and
quotas, below).

**Upload and image size caps are not tiered** — every bot, on every tier,
gets the same two limits:

- **Compressed upload archive** (`docker save | gzip > bot.tar.gz`): hard
  cap **250 MB** (`bot_archive_max_compressed_mb`). This is the value the
  upload endpoint actually rejects on.
- **Built image**: separately capped at **200 MB** (`bot_max_image_size_mb`),
  independent of the compressed-archive cap.

Per-bot runtime resources are also **not tiered** — every bot gets the
same CPU, memory, tmpfs, and decompressed-image ceiling regardless of tier:

| Resource | All tiers |
|---|---|
| CPU cores | 0.5 |
| Memory | 256 MB |
| tmpfs `/tmp` | 10 MB |
| Decompressed image | 1500 MB |
| Decision timeout (hosted bot) | 5000 ms (`bot_decision_timeout_ms`) |
| Decision timeout (remote / External API) | 30000 ms (`extapi_casual_decision_timeout_ms`) |

The decision clock depends on how the bot is hosted, not on tier; see §6.2.

What **does** vary by tier is bot-slot count and daily volume quotas. The
settings, with their free-tier values:

| Limit | Setting | Free tier |
|---|---|---|
| Bot slots | `tier_max_bots_{tier}` | 3 |
| Challenge matches per day | `daily_challenge_match_quota_{tier}` | 20 |
| Tournament entries per day | `daily_tournament_entries_quota_{tier}` | 2 |
| Concurrent External API matches (per user) | `path2_concurrent_match_cap_{tier}` | 1 |

The two daily quotas are separate counters and reset at UTC midnight;
over-quota requests get HTTP 429. The External API concurrent-match cap
counts active matches across all of your remote bots.

External API (remote) bots are also metered per account by three free-tier
caps: matches per day, matches per hour, and connected lobby hours per day.
§8.2.2 has the values, how each one is counted, and what to do when you hit
one.

During beta everyone is on the free tier. Paid plans with higher limits are planned.

> Earlier versions of this manual listed per-tier upload-size caps
> (Free 5 MB / Pro 25 MB / Elite 100 MB) alongside a legacy 100 MB POST
> path and a 500 MB direct-to-S3 path, and a per-tier CPU / memory / tmpfs /
> decision-timeout table. Those were all unified: per-bot resources and the
> upload/image caps are now single platform-wide values (**250 MB compressed
> / 200 MB image**, 0.5 vCPU / 256 MB / 10 MB, 1500 MB decompressed) and
> the decision clock resolves by hosting (§6.2). Tier now affects only the
> bot-slot count and the daily volume quotas above (free tier: 3 bot slots),
> never in-match resources.

### 7.3 Size budget

The reference bot (Alpine + SDK + 60-line `bot.py`) is ~20 MB compressed.
A non-trivial bot with numpy/scipy + a small model checkpoint typically
runs 100–150 MB compressed, well under the 250 MB cap — but a bundled
large model checkpoint can push you over. If you are hitting the size
cap:

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
- **§3.3 Resource limits.** Exceeding the platform-wide CPU / memory /
  decision timeout (see §7.2) means the server applies a safe default.
  Repeat crashes can trigger submission throttling.
- **§4.1 Match format.** Bot-vs-bot is elimination: you play until one
  bot has all chips. Bot-vs-bot matches are free to enter: no stakes and
  no entry fees (see §8.1).
- **§8.2 License to Chipzen.** By submitting a container image, you grant
  Chipzen a non-exclusive license to run it for platform purposes
  (matches, testing, promotional demos). You retain ownership of the
  source code. We never ask for it, and we commit not to reverse-engineer
  or extract what is in your image. To keep source out of the image, use
  the compiled starter (see `packages/python/IP-PROTECTION.md`).

### 8.1 Free-play vs ranked

This boundary is hard-coded in the platform:

- **Human-vs-bot** matches (the `/play` routes) are **always free**. No
  stakes, no entry fees, no prizes. Used for spectators, training, and
  human engagement.
- **Bot-vs-bot** matches (challenges, tournaments and seasons) are **also
  free to enter**: there are no player stakes, entry fees or wagers. Some
  events, such as the weekly seasons, pay out real prizes. Prizes are funded
  by Chipzen and are never a stake put up by players.

These are separate systems with separate APIs. Your bot doesn't need to
care which one it's in — the WebSocket protocol is the same.

### 8.2 Slot model

Free tier gets **3 bot slots** (from `tier_max_bots_free = 3` in
config). Pro: **10**. Elite: **100**.

Uploads are not versioned: each upload creates a new independent bot.
To upload a fourth bot on the free tier, delete one first. The backend
enforces the cap with HTTP 409.

### 8.2.1 WebSocket frame-rate and connection limits

Every WebSocket you open to the platform is held to two limits. A bot that
answers each `turn_request` once and replies to pings sends a few frames a
second at most and never reaches either of them.

| Limit | Value | What happens over it |
|---|---|---|
| Frames you send on one socket | 20 per second sustained, with a burst of 40 | The socket is closed with code `1008`, reason `message rate limit exceeded` |
| Sockets open at once for one bot (lobby, match and conformance sockets together) | 24 | The new socket is accepted and then closed with code `1008`, reason `too many concurrent connections`. Your open sockets are not affected |
| Sockets open at once for one signed-in user (browser play and spectating) | 10 | Same as above |

Notes for bot authors:

- `1008` is the standard WebSocket "policy violation" code, and the platform
  also uses it for the per-IP connect limit. The close **reason** tells the
  three apart.
- The frame limit counts every frame, including `pong` and frames the
  server would otherwise ignore. It is per socket, so one socket's traffic
  does not use up another's allowance.
- This limit sits in front of the in-match limit of 10 messages per second
  (close code `4009` after five violations, see
  [`ERROR-CODES.md`](ERROR-CODES.md)). A client that sends in a tight
  loop can therefore see `1008` before it sees `4009`.
- Do not reconnect in a loop after a `1008`. Fix the send loop, or close the
  sockets you no longer need, then reconnect once. The match treats a match
  socket closed this way as that seat disconnecting.
- A well-behaved client needs no change, and there is nothing to negotiate.

### 8.2.2 Remote bots: daily lobby allowance

This section is for remote (External API) bots, the ones you run on your own
machine (§2.7). Uploaded bots are not affected.

**The allowance.** The free tier includes **8 hours of connected lobby time
per UTC day** for each account. It is shared by all of the account's remote
bots, so two bots connected for 4 hours each use the whole day's allowance.

**How it is counted.** The clock runs whenever one of your bots has its lobby
connection open, including while the bot is idle or waiting in the rated
queue. The platform adds the time on each lobby heartbeat (every 15 seconds),
and counts it against the UTC day it happens in. The allowance resets at
**00:00 UTC**.

**When it is used up:**

- Ad-hoc play is refused until the reset. That means joining the rated queue
  and challenges, both the ones your bot sends and the ones sent to it.
- Your bot is not disconnected, and it can still connect. A match that is
  already running plays to the end.
- Season fixtures and tournament matches are not affected. Entering a
  tournament is checked against the two match limits in the table below, but
  not against the lobby allowance.

**What your client should do when it is refused.** The refusal is HTTP 429
with error code `FREE_TIER_LIMIT_EXCEEDED`. The message names the limit and
the time it resets, for example:

```
Free-tier limit exceeded: lobby_hours_per_day (8.8/8.0 hours); resets at 2026-10-13T00:00:00+00:00.
```

Stop retrying until the `resets at` time. Every request before then is
refused the same way. On the website, a refused challenge shows "This bot's
account has used its daily allowance of connected lobby time. It resets at
midnight UTC."

**Making the allowance last:**

- Connect your bot when you want to play or test, and disconnect it when you
  are done.
- Don't leave a bot connected around the clock unless you mean to spend the
  allowance that way. A bot left running overnight can use up the day's
  allowance before you sit down to test.
- If you are not in UTC, work out your local reset time. At UTC+5:30, for
  example, the allowance resets at 05:30 local time.
- For a season fixture or a tournament, connect your bot as usual. Time spent
  waiting for a scheduled match still counts toward the allowance, but the
  allowance never stops a scheduled match.

**Free-tier limits a remote bot can hit.** All three are per account, shared
by all of its remote bots, and apply to ad-hoc play only:

| Limit | Value | Resets | Name in the error |
|---|---|---|---|
| Connected lobby time | 8 hours per day | 00:00 UTC | `lobby_hours_per_day` |
| Matches per day | 100 | 00:00 UTC | `matches_per_day` |
| Matches per hour | 20 | At the top of each hour (UTC) | `matches_per_hour` |

Season and tournament matches are never refused by these limits, but they do
count toward the two match totals. The tier quotas in §7.2 (challenge matches
per day, concurrent remote matches) apply as well.

During beta everyone is on the free tier.

### 8.3 Lifecycle in one glance

The bot lifecycle is **one-way (forward-only)**. After upload your bot
moves through these states:

```
uploading -> pending_review -> reviewing -+-> active     (passed review — automatic)
                                          |
                                          +-> rejected   (failed review — terminal)
```

- `reviewing -> active` is **automatic** on a passing review — there is no
  manual "activate" step and no intermediate `approved` state. Every
  passing upload becomes `active`; there is no "only one active at a time"
  restriction.
- `active` — the bot competes: it appears on its division leaderboard and
  can be challenged or entered into tournaments (always by your explicit
  registration; activation does not auto-schedule anything).
- `rejected` — **terminal**. Fix your code and upload a *new* bot (uploads
  are not versioned; each upload is an independent bot).

From `active`, the only exits are the two dev-initiated terminal states,
both confirm-gated and irreversible:

- `retired` — "keep my bot, it's done competing." It can no longer play,
  but its row, rating, and match/hand history are preserved.
- `deleted` — removed from your account (image artifacts purged); match
  and hand history are retained (platform-owned data). Delete is available
  from any state.

A separate `disabled` boolean is an admin **moderation overlay**
(reversible), orthogonal to the lifecycle status above — not a lifecycle
state. There is no `approved` or `suspended` state, and no
`activate`/`deactivate` endpoints.

Remote (external-API) and raised bots skip review and are **born
`active`**.

### 8.4 Standings and tiebreaks

How places and ties are decided, in the order the platform applies them.
The full rules are at
[chipzen.ai/rules#tiebreaks](https://chipzen.ai/rules#tiebreaks).

- **Seasons.** Bots are ranked by wins. Level on wins, fewer hands per win
  ranks higher. This is an average over the wins that were played: a
  walkover counts as a win but not in this average, and a bot whose wins
  are all walkovers ranks last on this step. Still level, the winner of the
  match between the two bots ranks higher, and after that a fixed draw
  based on the season and the bot decides. The top 8 after the round robin
  go to the playoffs (1st plays 8th, 2nd plays 7th, 3rd plays 6th, 4th
  plays 5th). With 4 to 7 bots the top 4 go through, and with 2 or 3 bots
  the top 2 play a final.
- **Bracket tournaments.** The winner of each match moves on. At the hand
  cap, the bot with more chips moves on, and if the chips are level, the
  bot listed first in that match moves on. Bots knocked out in the same
  round share a place (2nd, 3rd, 5th and so on).
- **Multiplayer games.** The last bot with chips wins, and the others place
  in reverse order of being knocked out. Bots knocked out on the same hand
  are ordered by seat: the higher seat number places higher. With several
  tables, a bot that reaches a later round places above every bot knocked
  out earlier. Within a round, the place at the table decides, then the
  later knockout hand, then the table and seat number.

---

## 9. Troubleshooting

Each pattern lists the *specific log line or UI indicator* that
identifies it, so you don't have to guess.

### 9.1 "My bot got rejected during review"

**Where it shows up:** Bot card flips from `reviewing` to `rejected` in
the Developer UI. Click "Show rejection reason" — it's in
`bot.rejection_reason`.

**Common reasons:**

- `Your bot image runs as root (...)`: the image has no `USER`, or its
  `USER` is root / uid 0. Add `RUN useradd -u 10001 -m bot` and
  `USER 10001` before the `ENTRYPOINT` (§7.1 item 4), rebuild, and
  re-upload. `... could not find that user in the image's /etc/passwd`
  means a named `USER` that review cannot resolve; switch to a numeric
  uid.
- `Image exceeds <N> MB compressed` — you're over the 250 MB compressed
  upload cap (§7.2). Use Alpine + wheels-only + strip pycache.
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
  often accepts an empty token for the `/bot` endpoint; the hosted
  platform does not.

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

## 10. Big Bad Bots

**Big Bad Bots** (game type `bigbadbots`) is Chipzen's robot-fighting game, in
beta. This chapter is the reference for building one in Python. For the
step-by-step walkthrough, start at
[`BIGBADBOTS-QUICKSTART.md`](BIGBADBOTS-QUICKSTART.md). The wire spec is
[`protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md),
and it wins if this chapter ever disagrees with it.

Values marked **provisional** below are placeholders pending a rules
decision. They travel in `match_start.game_config`, so a bot that reads them
from `state.rules` keeps working when they change.

### 10.1 A match in one page

A match is one bot against one bot. Every step is **simultaneous**: both seats
are asked at the same moment, each on its own socket, and neither sees the
other's answer before it submits its own. There is no turn order.

| Step | Your method | `state.phase` | State model | How many |
|---|---|---|---|---|
| Body shop | `choose_body()` | `body_shop` | `BodyShopState` | 1 |
| One auction lot | `bid()` | `auction` | `AuctionState` | one per lot (`state.rules.gearup.auction_lots`, 6 today) |
| Fitting | `fit()` | `fitting` | `FittingState` | 1 |
| One fight tick | `act()` | `fight` | `FightState` | one per tick, `state.rules.rounds.ticks_per_second` (10) per second of match time, every round |

The fight is a best-of-three: the first seat to `rounds.rounds_to_win` round
wins takes the match, and drawn rounds extend it up to `rounds.max_rounds`. A
round ends on a KO, at the tick limit (`rounds.round_ticks`, 600 ticks or
60 s), or by forfeit. At the tick limit, `rounds.timeout_tiebreak` decides:
`"damage"` (the launch rule set, `bigbadbots-1.2`) gives the round to the
seat that dealt more damage in it; `"hp_pct"` (or absent, `bigbadbots-1.0`)
to the higher share of max HP.

The ten fight actions: `idle`, `move_in`, `move_out`, `block`, five attacks
(`punch_high`, `uppercut`, `kick_high`, `kick_low`, `throw`) and `special` (the
body's special move, once per round). Each attack has a startup, an active
and a recovery phase, a stamina cost and a range. **A strike beats a throw, a
throw beats a block, and a block beats a strike.**

### 10.2 `BigBadBotsBot`

Subclass `chipzen.BigBadBotsBot` and implement `act()`. The three gear-up
methods have legal defaults you override when you want a real strategy:

```python
from chipzen import Action, BigBadBotsBot
from chipzen.bigbadbots import AuctionState, BodyShopState, FightState, FittingState

class MyBot(BigBadBotsBot):
    def choose_body(self, state: BodyShopState) -> Action:
        return Action.choose_body(state.offer[0].id)   # a body id from state.offer

    def bid(self, state: AuctionState) -> Action:
        return Action.bid(0)                           # 0 .. state.me.credits; 0 passes

    def fit(self, state: FittingState) -> Action:
        return Action.fit({0: state.owned[0].id} if state.owned else {})  # slot index -> part id

    def act(self, state: FightState) -> Action:         # required
        if state.distance > state.me.strike_range:
            return Action.act("move_in")
        return Action.act("punch_high")
```

(That `fit()` is a sketch: a real one checks that each part's `slot` matches
the slot type in `state.slots`.)

| Action builder | Call | Notes |
|---|---|---|
| `Action.choose_body(body_id)` | `choose_body` | A body not on your offer is corrected to the cheapest one on it. |
| `Action.bid(amount)` | `bid` | A whole number. Outside `0..me.credits` it is clamped and logged as a violation. |
| `Action.fit({slot_index: part_id, ...})` | `fit` | Slot indexes into `state.slots`. An entry that does not fit is dropped; the rest are fitted. `{}` fits nothing. |
| `Action.act(move)` | `act` | One of the ten fight actions. Raises `ValueError` for anything else. |

What the class does for you:

- **Declares the game.** It sends `supported_games=["bigbadbots"]` in its
  `hello` unless you pass your own list.
- **Routes each request.** `decide()` routes on `state.phase` to the four
  methods. You do not override `decide()`.
- **Parses the rule set.** The default `on_match_start()` reads
  `match_start.game_config` into `self.game_config` (wire form) and
  `self.rules` (typed, read-only), and every state carries the same object as
  `state.rules` (§10.7). **If you override `on_match_start()` or
  `on_reconnected()`, call `super()` first**, or `state.rules` is `None`.
- **Times every call** and sends the protocol default when your method is
  late, raises (under `safe_mode`, the default), or returns something that is
  not one of `valid_actions` (§10.3).

The other `Bot` hooks fire as usual: `on_round_start`, `on_turn_result` (its
`details.opponent_action` is the opponent's action as you are allowed to see
it), `on_round_result`, `on_match_end` and `on_decision_latency`.

The state models (`chipzen.bigbadbots`):

| Model | Key fields |
|---|---|
| `BodyShopState` | `offer` (your bodies: `id`, `cost`, `stats`, `slots`, `special`), `me`, `opp`, `lots_total` |
| `AuctionState` | `lot` (the part on sale: `id`, `slot`, `bonus`, `effects`), `me`, `opp`, `lot_index`, `lots_total`, `history` |
| `FittingState` | `owned` (every part you won, in full), `slots` (your body's slot types, by index), `me`, `opp`, `history` |
| `FightState` | `tick`, `round`, `rounds_won`, `me`, `opp` (fighters), `distance`, `facing`, `walls` |

Every state also has `request_id`, `timeout_ms`, `valid_actions`, `phase` and
`rules`. In gear-up, `me` and `opp` are sides (`credits`, `body`, `owned`,
`offer`, `fitted`); in the fight they are fighters (`x`, `hp`, `max_hp`,
`stamina`, `max_stamina`, `action`, `phase`, `frames_left`, `special`,
`special_used`, `last_attack`, `repeats`, `strike_range`, `move_rate`,
`attrs`, `buffs`). Protocol §4.3 defines every field.

### 10.3 The per-tick budget

A fight has 10 ticks per second of match time, but the platform does not
wait out a tick: it plays the fight as fast as both bots answer, and
spectators watch a paced replay afterwards. Every fight request carries its
time limit in `state.timeout_ms`. The numbers come from the platform's
response clock (decision D-20), which `match_start` announces in
`game_config.response_clock`:

| Value | Today | Where to read it |
|---|---|---|
| Response window | **250 ms** per fight tick | `game_config.response_clock.deadline_ms` |
| Time bank | **3 s per seat per match**, never refilled | `game_config.response_clock.time_bank_ms` (`bank_scope` is `"match"`) |
| What this request will wait for | window plus what is left of your bank | `state.timeout_ms` |
| What is left of your bank | | `state.timeout_ms - response_clock.deadline_ms` |

**Do not read the clock from `state.rules.deadline`.** The released rule
sets (`bigbadbots-1.0` to `bigbadbots-1.2`) are frozen and still carry the
older provisional numbers there: 100 ms per tick and a 3 s bank refilled
every round. The platform applies the response clock over any rule set. The
rest of the `deadline` section still applies: `default_action_policy` (the
default on a missed tick, §10.4), `late_answer` and
`miss_counts_toward_forfeit`. See [`RULESETS.md` §6](RULESETS.md#6-playing-and-testing-against-a-rule-set)
and protocol §3.4.

The SDK does not parse `response_clock` yet. Read it from the raw
`match_start` message in `on_match_start()`:

```python
def on_match_start(self, match_info: dict) -> None:
    super().on_match_start(match_info)  # fills self.rules and state.rules
    clock = match_info["game_config"].get("response_clock") or {}
    self.window_ms = clock.get("deadline_ms", 250)
    self.match_bank_ms = clock.get("time_bank_ms", 3000)
```

- **An answer inside the window costs nothing.** An answer later than the
  window is paid for from the bank, for the part beyond the window. The bank
  is one pot for the whole match: what you spend in round 1 is gone in
  round 3.
- **The server measures from sending the request to receiving your answer**,
  rounded up to whole milliseconds, so the network hop is inside the budget.
- **A late answer is dropped**, never applied to a later tick. The tick plays
  the default action instead (§10.4).
- **Misses count toward a forfeit.** A seat that misses every tick for
  `forfeit.grace_seconds` (5 s, provisional) forfeits the round, and
  `forfeit.rounds_for_match` (2, provisional) forfeited rounds forfeit the
  match. Any answer, even a rejected one, resets the count.
- **Gear-up calls have no per-tick deadline.** Their `timeout_ms` is the
  match's turn timeout, so gear-up is where you can afford to think.

**What the SDK does.** It runs your method on a worker thread and waits for
it until 20 ms before `timeout_ms` runs out. Past that it sends the
protocol's default for the call and logs a warning. It cannot interrupt your
method, so while a timed-out call is still running, every later request is
answered with the default without calling your method again.

So a slow `act()` does not just lose one tick. It spends the bank, and once
the bank is empty (for the rest of the match, not just the round), every
tick slower than 250 ms plays the default.

**Staying inside the window:**

- Do the expensive work once. Read `state.rules` into lookup tables in
  `on_match_start()` (call `super()` first), not on every tick.
- Keep `act()` to arithmetic over the state. No file or network I/O, no model
  loading, no imports.
- Do not log on every tick. A round is hundreds of ticks; a line per tick
  fills your log and costs time.
- Watch `on_decision_latency(latency_ms)`, and keep an eye on the
  `bigbadbots_timing` line of `chipzen validate`, which warns when `act()`
  takes over 100 ms (the rule set's number, so stricter than the 250 ms
  window).

### 10.4 One answer per tick: send `block` again every tick

Every tick is a new `turn_request` with its own `request_id`, and every tick
needs its own answer. There is no "hold" action: **`block` protects you on
the tick you send it.** To keep blocking, return `block` again on the next
tick, and the next.

```python
class Guard(BigBadBotsBot):
    def act(self, state: FightState) -> Action:
        # Re-decide every tick. Returning "idle" for one tick drops the guard.
        if state.opp.phase in ("startup", "active") and state.distance <= state.opp.strike_range * 1.5:
            return Action.act("block")
        return Action.act("idle")
```

Do not lean on the server's default to keep a block up. On a missed tick the
server plays your previous action if it was `block`, `move_in` or `move_out`
(otherwise `idle`), but a tick only counts as missed once `timeout_ms` has
run out, so relying on it spends your whole remaining bank on the first miss,
and every miss counts toward the forfeit (§10.3). The SDK's own fallback
repeats `block` the same way, and only after the same wait.

Three more rules about the fight loop:

- **A busy fighter ignores what you send.** While `state.me.phase` is not
  `None` (startup, active, recovery, hitstun or dazed), the server ignores
  your action until the current one ends. You still answer every tick
  (`idle` is the natural answer); you just cannot block during your own
  recovery, which is why a whiffed attack gets punished.
- **An action you cannot afford plays as `idle`.** `valid_actions` is always
  all ten fight actions; whether one can be performed now (stamina, a used
  special, a busy fighter) is decided when the tick resolves.
- **Blocking forever loses.** A blocked strike still deals chip damage and
  drains stamina, a block breaks at 0 stamina, and a throw beats a block.

### 10.5 The masked opponent

The server removes everything a seat may not see before it sends the state,
so no payload ever carries it. Your own side is always shown in full.

| Phase | Hidden from you | Public |
|---|---|---|
| `body_shop` | the opponent's offer (`opp.offer` is `[]`) | both seats' credits |
| `auction` | the opponent's bid on the lot being sold; the lots not yet revealed | the opponent's body, credits and parts won; both bids on every lot already sold (`history`) |
| `fitting` | the opponent's fitting (`opp.fitted` is one `None` per slot) | everything the auction showed |
| `fight` | which move the opponent is winding up, and its frames left | position, HP, stamina, attributes, strike range, move rate, special id, status; the move once it is revealed, and during recovery |

**The hidden wind-up.** While the opponent winds up a move, `state.opp` shows
`action == "attack"`, `phase == "startup"` and `frames_left is None`. Its
`stamina`, `special_used`, `last_attack`, `repeats` and `buffs` keep their
values from before the move started, so none of them gives the move away.

When the move is revealed is the rule set's `combat.intent_reveal`:

- `"on_active"` (or absent, `bigbadbots-1.0`): on the tick the move becomes
  active, which is the tick it hits, so a `block` answered on that tick still
  counts.
- `"after_hit"` (`bigbadbots-1.1` and `bigbadbots-1.2`, the launch rule set):
  the first active tick still shows as a wind-up, and the move is revealed
  from the tick after. **You cannot block a strike on sight.** You decide
  while it is still `"attack"`.

```python
reveal = state.rules["combat"].get("intent_reveal", "on_active")
```

Against a hidden wind-up you are making a read, and the counter triangle is
the whole game: block (beats a strike, loses to a throw), step out of range
(`move_out`, Zoner's answer), or strike first with an attack whose startup is
shorter than theirs is likely to be (Rusher's answer).

Two more things the mask does not cover:

- **An interrupted wind-up is revealed.** If your hit staggers the opponent
  mid-wind-up, it shows `"hitstun"`, but from then on `last_attack` and
  `repeats` name the move it tried, and `stamina` shows its cost paid.
- **The opponent's fitting shows in the fight**, through `opp.attrs`,
  `opp.max_hp`, `opp.max_stamina`, `opp.strike_range` and `opp.move_rate`.

Never in any payload during play: the opponent's answer for the current step,
its timing (when it answered, whether it used its bank or missed a tick), and
the match seed. After the match, the replay carries everything.

### 10.6 Gear-up and auction strategy basics

**One budget pays for everything.** Each seat starts with
`state.rules.gearup.budget` credits (1000 today). The body is paid for first,
and the auction spends the rest. **Credits left when the auction ends are
lost.**

**The body shop.** Each seat gets its own private offer of
`gearup.body_offer_size` bodies (4 today); the two offers can differ. A body
has a `cost`, base `stats` (the six attributes), `slots` (part slot types, a
type can repeat) and a `special`. The trade-off is direct: an expensive body
leaves fewer credits for parts, and more slots are only worth something if
you can afford parts to fill them. Both choices are revealed before the
auction, so in `bid()` you know the opponent's body.

**The auction.** `gearup.auction_lots` parts (6 today) are sold one at a
time. For each lot both seats submit one **sealed bid** at the same moment.
The highest bid wins and **pays its own bid**. A tie goes to a seeded coin
flip, and if both bid 0 the lot goes unsold. Both bids are revealed before
the next lot.

What you know when you bid: the lot on sale now (never future lots), how many
lots remain (`lots_total - lot_index`), the opponent's credits and the parts
it has won, and both bids on every lot already sold (`history`).

Basics that hold whatever your style:

- **Value a lot by what it adds to your best fitting**, not by its raw stats.
  A part only helps if it fits a slot type your body has, and if it beats
  what you would otherwise put in that slot. All three starters compute
  "best fitting with this lot" minus "best fitting without it".
- **Never bid more than `state.opp.credits + 1`.** The opponent cannot beat
  that, so anything above it is wasted.
- **Pace your credits.** `credits / lots_left` is your fair share per lot;
  scale it by how much you want this lot. The starters' `BID_TEMPER` (Rusher
  1.25, Zoner 0.75, Turtle 0.65) is exactly that knob.
- **Spend it all on the last lot you can use.** Credits are worth nothing
  once the auction ends.
- **Denial is real.** A part you cannot use may still be the part the
  opponent's body needs, and you know its body and what it has won.
- **Read the history.** `history[i].opp_bid` shows how the opponent bids.
  Bots that bid a fixed share, or always pass on some slot types, are easy
  to exploit.

**The fitting.** `state.slots` lists your body's slot types by index, and
`state.owned` every part you won (you keep them all, even ones you cannot
fit). Answer with `Action.fit({slot_index: part_id})`: one part per slot,
matching the slot type. Parts left over are unused, and attributes are capped
to their range after all parts apply. The opponent never sees your fitting
directly, only its effect on your fighter once the fight starts.

### 10.7 Reading the rule set (`state.rules`)

Every match is played under one **rule set**, the named set of numbers the
game runs on: the budget, the bodies and parts, every move's frame data, the
arena, the round length, the deadline. A balance patch is a new rule set with
a new id, never a code change, so **a bot that reads its numbers from the
rule set keeps working when they change**. The id scheme, the lifecycle and
the changelog are in [`RULESETS.md`](RULESETS.md). The launch rule set is
`bigbadbots-1.2`.

`state.rules` (a `chipzen.bigbadbots_rules.RuleSet`, the same object as
`self.rules`) holds the rule set `match_start` sent:

| Attribute | What it holds |
|---|---|
| `id` | The rule-set id, for example `"bigbadbots-1.2"`. Fixed for the whole match. |
| `gearup` | `budget`, `body_offer_size`, `auction_lots` |
| `arena` | `width`, `start_x`, `start_jitter`, `min_separation` |
| `rounds` | `ticks_per_second`, `round_ticks`, `rounds_to_win`, `max_rounds`, `timeout_tiebreak`, the optional closing-wall keys (`stall_close_after`, `stall_close_rate`, `stall_min_width`), and `closing_walls` (whether the rule set has them) |
| `forfeit` | `grace_seconds`, `rounds_for_match` |
| `deadline` | `deadline_ms`, `time_bank_ms`, `default_action_policy`, `late_answer`, `miss_counts_toward_forfeit`. The platform overrides `deadline_ms` and `time_bank_ms` with `game_config.response_clock` (§10.3). |
| `["section"]` | Any section of the full table, read-only: `attributes`, `slot_types`, `bodies`, `parts`, `moves`, `specials`, `fighter`, `combat`, `special_effects`, and the five above. [`RULESETS.md` §4](RULESETS.md#4-the-parameter-table) lists what each holds. |

The full table travels in every `match_start`, so the bodies, parts and
moves are always there in play:

```python
def act(self, state: FightState) -> Action:
    rules = state.rules
    kick = rules["moves"]["kick_high"]        # startup, active, recovery, stamina, damage, ...
    # A move either has a fixed "range" (the throw does) or reaches a multiple
    # of the fighter's strike_range. This is how the starters read it.
    reach = kick["range"] if kick.get("range") is not None else kick.get("reach", 1.0) * state.me.strike_range
    if state.distance <= reach and state.me.stamina >= kick.get("stamina", 0):
        return Action.act("kick_high")
    return Action.act("move_in")
```

Rules for reading it:

- **Use `.get()` with a default for optional keys.** A key a rule set leaves
  out means "the old behaviour": `combat.intent_reveal` absent means
  `"on_active"`, and the closing-wall keys are absent in `bigbadbots-1.0`
  and `bigbadbots-1.2` (so `walls` stays at the arena edges).
- **Branch on behaviour keys, not on the id.** Read `rounds.timeout_tiebreak`
  and `combat.intent_reveal` rather than comparing `rules.id` to a string;
  the next rule set may keep the behaviour under a new id.
- **The table is read-only.** Objects are read-only mappings and arrays are
  tuples. `self.game_config.rule_set` keeps the wire form (plain dicts),
  which is what the starters read.

**Offline lookup.** The SDK bundles the published numbers of every released
rule set:

```python
from chipzen.bigbadbots_rules import published_rule_set, published_rule_set_ids

published_rule_set_ids()                  # ('bigbadbots-1.0', 'bigbadbots-1.1', 'bigbadbots-1.2')
entry = published_rule_set("bigbadbots-1.2")
entry.state, entry.engine, entry.sha256   # 'preview', '1.2.0', '5e1f...'
entry.sections["deadline"]["deadline_ms"] # 100
```

The bundle holds the published sections (`gearup`, `arena`, `rounds`,
`forfeit`, `deadline`), not the bodies, parts or moves; those come from
`match_start`. It is a reference only: **the server's numbers always win**.
A `rule_set_id` the installed SDK does not know, or a published value that
differs from the one sent, logs a warning, and the bot plays on the server's
numbers.

### 10.8 Checking a bot: `chipzen validate`

`chipzen validate <bot dir>` is the pre-upload check. For a `BigBadBotsBot`
subclass it runs the Big Bad Bots checks automatically; for a bot that
speaks the protocol some other way, add `--game bigbadbots`.

| Check | What it proves |
|---|---|
| `act_method` | The class implements `act()`. |
| `bigbadbots_choose_body`, `bigbadbots_bid`, `bigbadbots_fit`, `bigbadbots_act` | Every request in the conformance fixtures (real `turn_request`s from recorded matches, all four calls) gets an answer, judged as the server judges it. **Malformed** (the server would reject it) fails; **corrected** (well-shaped but not legal, so the server would fix it and log a violation) warns. |
| `bigbadbots_state_mutation` | No method changes the state it is handed. |
| `bigbadbots_timing` | Every answer arrives within `timeout_ms`; warns when `act()` takes over the rule set's 100 ms `deadline_ms` (stricter than the platform's 250 ms window, §10.3). |
| `bigbadbots_exit` | No method raises `SystemExit` or another `BaseException`. |
| `bigbadbots_crash_every_tick` | `act()` does not crash on every tick. |
| `bigbadbots_dockerfile` | Only when the folder has a `Dockerfile`: its final stage copies `log_cap.py` to `/opt/chipzen/log_cap.py` and its ENTRYPOINT is `["python", "-I", "-S", "-u", "/opt/chipzen/log_cap.py", "--"]` (`python3` in the JavaScript starter), so the image starts through the platform's log-cap wrapper. The platform's review rejects an image that does not. |

The file, syntax, import, requirements and size checks run as for any bot.
`--check-connectivity` drives the poker protocol only; for a Big Bad Bots bot
the checks above already drive all four calls.

A pass means your bot will not be rejected on protocol grounds. It is not a
strength check: there is no local simulator, and how well a bot fights is
measured on the platform (§10.9).

### 10.9 Iterating on the platform

The game engine runs on the platform only; there is no local simulator. The
loop is:

1. **Check:** `chipzen validate .`
2. **Build and export:** `docker build -t my-bot:vN .` then
   `docker save my-bot:vN | gzip > my-bot.tar.gz` (§7). Big Bad Bots bots
   run through the platform's log-cap wrapper (`log_cap.py` in each starter
   folder), which caps how much a bot can log in one match. The starters'
   Dockerfiles make it the ENTRYPOINT, so keep that ENTRYPOINT and
   `log_cap.py` as they are and put your bot command in `CMD`.
3. **Upload** on `chipzen.ai` and choose **Big Bad Bots** as the bot's game.
   Review moves it from `pending_review` to `active` (§8.3).
4. **Challenge the platform bots** with Big Bad Bots as the game. A challenge
   against a platform bot starts straight away. Platform-bot and same-owner games
   are practice (unrated); games against another builder's bot are rated by
   win share during the beta.
5. **Read the replays and your per-match log** (§5), change one thing, and go
   round again with a new version tag.

The platform bots cover a range of styles, including the originals of the three
starters. Play several matches per version: body offers, lots and start
positions come from each match's seed, so one match is one sample.

**Staging.** To use the staging site, upload to `staging.chipzen.ai` instead.
The image is the same: the platform hands an uploaded bot its match URL
(`CHIPZEN_WS_URL`), so nothing in the image names prod or staging.

**Remote play.** Remote play from your own machine (`chipzen run-external`,
§2.7) does not take Big Bad Bots matches yet; a Big Bad Bots bot plays as an
uploaded image. The SDK side is ready for when it opens:
`chipzen run-external bot.py` targets prod, and `--env staging` (or
`CHIPZEN_ENV=staging`) targets staging.

### 10.10 The starters as worked examples

The three starters in
[`packages/python/starters/bigbadbots/`](../packages/python/starters/bigbadbots/)
are complete bots and the best reference for the patterns above. Each is one
`bot.py` with its taste in constants at the top.

| Starter | Gear-up | Fight | Read it for |
|---|---|---|---|
| [Rusher](../packages/python/starters/bigbadbots/rusher/) | Quick bodies, `BID_TEMPER = 1.25` | Strikes first against a wind-up, throws blockers, spends its special early | Punishing recovery with frame data from `rules["moves"]`; the counter triangle |
| [Zoner](../packages/python/starters/bigbadbots/zoner/) | Long-reach bodies, `BID_TEMPER = 0.75` | Holds the tip of its kick range, steps out of wind-ups, blocks with a wall behind it | Spacing with `distance`, `strike_range` and `walls` |
| [Turtle](../packages/python/starters/bigbadbots/turtle/) | Heavy, guarded bodies, `BID_TEMPER = 0.65` | Blocks wind-ups every tick, punishes whiffs, attacks when the round clock says so | Blocking tick after tick; playing the time-out tiebreak |

All three share the same auction logic (value a lot by its gain to the best
fitting, cap at `opp.credits + 1`, go all in on a useful last lot), so the
gear-up differences come only from their constants. Each README covers its
time budget, the SDK fallback, and packaging.

---

## 11. Getting help

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

*Last updated 2026-06-13. Sourced from SDK version 0.3.0. If any example
in this manual doesn't work against the current SDK, file an issue — the
manual is expected to be executable, not aspirational.*
