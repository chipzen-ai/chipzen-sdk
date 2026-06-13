<!-- Canonical public copy. Mirrored from the Chipzen platform repo (docs/COMMON-PITFALLS.md); platform-internal issue references appear as CZ#NNNN. -->

# Common Pitfalls — Bot Failure Modes

> **Living document.** This is the running catalogue of failure modes new
> Chipzen bot developers hit. Each entry covers: what it looks like, why it
> happens, how to detect it, and how to fix it. Append new entries as we
> observe them in alpha and beta — do **not** delete entries even when the
> underlying bug is fixed platform-side, because old SDK versions and old bot
> images keep tripping the same patterns in the wild.
>
> **Tone:** narrower honest claims, with specific numbers and concrete log
> lines. If you find yourself writing "should usually" or "in most cases,"
> stop and look up the actual config value or grep the source.

**Related docs:**

- [`sdk/QUICKSTART.md`](QUICKSTART.md) — first-timer walk-through
  (build → upload → play).
- [`docs/DEV-MANUAL.md`](DEV-MANUAL.md) — canonical bot developer manual;
  every pitfall below has more depth in there under its corresponding
  section.
- [`docs/ERROR-CODES.md`](ERROR-CODES.md) — API + wire-error catalogue.
  Pitfalls in this doc tag the relevant `bot_error.reason` and HTTP
  error code so you can cross-reference from a stack trace.
- [`docs/protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) +
  [`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)
  — the wire-level specs. Authoritative if this doc and they disagree.

---

## How to use this doc

1. **Match the symptom first.** Each pitfall starts with the *exact log
   line, UI indicator, or status flip* that identifies it. If your symptom
   doesn't match, the cause is probably different.
2. **Read "Why it happens" before applying the fix.** Several pitfalls
   below look identical in the match replay (`bot folds every hand`) but
   have very different root causes — applying the wrong fix wastes hours.
3. **Reproduce locally with `chipzen-sdk validate`** when possible (add
   `--check-connectivity` to exercise the wire protocol against the
   in-process mock server — see `DEV-MANUAL.md` §4). For strategy and
   timing bugs, drive `decide()` directly with scripted states in unit
   tests — minutes instead of hours.

---

## Table of contents

1. [`decide()` exceeds the 5000 ms budget — auto-fold + ELO loss](#1-decide-exceeds-the-5000-ms-budget--auto-fold--elo-loss)
2. [Card encoding mismatch (`As` vs `AS` vs `A♠`)](#2-card-encoding-mismatch-as-vs-as-vs-a)
3. [Hand-evaluator format mismatch — pluggable evaluators don't match the wire format](#3-hand-evaluator-format-mismatch--pluggable-evaluators-dont-match-the-wire-format)
4. [Missing or stale `request_id` echo — every action gets rejected](#4-missing-or-stale-request_id-echo--every-action-gets-rejected)
5. [Raise amount sent as increment instead of total bet](#5-raise-amount-sent-as-increment-instead-of-total-bet)
6. [Illegal action: `check` when there's a bet to call (or vice versa)](#6-illegal-action-check-when-theres-a-bet-to-call-or-vice-versa)
7. [No error handling around WebSocket disconnect — bot dies on first dropped frame](#7-no-error-handling-around-websocket-disconnect--bot-dies-on-first-dropped-frame)
8. [Bot hangs after winning a hand — state-machine assumes the match continues forever](#8-bot-hangs-after-winning-a-hand--state-machine-assumes-the-match-continues-forever)
9. [Trying to write GTO/CFR from scratch — quality cliff before any results land](#9-trying-to-write-gtocfr-from-scratch--quality-cliff-before-any-results-land)
10. [Assuming infinite decision time / misunderstanding the compute-fairness model](#10-assuming-infinite-decision-time--misunderstanding-the-compute-fairness-model)
11. [Hardcoded blinds — bot mis-sizes after blind escalation](#11-hardcoded-blinds--bot-mis-sizes-after-blind-escalation)
12. [Greedy resource use triggers sandbox kill — silent container exit](#12-greedy-resource-use-triggers-sandbox-kill--silent-container-exit)
13. [Writing outside `/tmp` — `Errno 30 Read-only file system`](#13-writing-outside-tmp--errno-30-read-only-file-system)
14. [Image exceeds platform upload cap — rejected at upload](#14-image-exceeds-platform-upload-cap--rejected-at-upload)
15. [Cold start > 15 s — `bot_container_failed_to_attach`](#15-cold-start--15-s--bot_container_failed_to_attach)
16. [Wrong WS bind address on native Linux — bot can't reach the server](#16-wrong-ws-bind-address-on-native-linux--bot-cant-reach-the-server)
17. [Expensive work inside `on_turn_result` / `on_phase_change` — drains your decide budget](#17-expensive-work-inside-on_turn_result--on_phase_change--drains-your-decide-budget)
18. [Branching on `total_hands` or remaining-hands — field no longer exists](#18-branching-on-total_hands-or-remaining-hands--field-no-longer-exists)
19. [Persisting per-match state across matches by accident](#19-persisting-per-match-state-across-matches-by-accident)
20. [Forgetting `python -u` (unbuffered stdout) — log lines silently dropped](#20-forgetting-python--u-unbuffered-stdout--log-lines-silently-dropped)

---

## 1. `decide()` exceeds the 5000 ms budget — auto-fold + ELO loss

**Symptom.**

- Match plays normally but your bot appears to fold (or check) at every
  decision after the first hand or two.
- API log: `WARN safe_default fold ...`
- API log: `Bot turn_action round-trip for match <id>: 5001.3ms (budget=5000ms)`
- Human-vs-bot UI shows a `bot_error` toast with
  `reason=bot_decision_timeout`.

**Why it happens.**

`decide()` must return end-to-end within the server's decision budget. The
absolute fallback budget is **5000 ms** (the platform's
`bot_decision_timeout_ms` setting); ranked bot-vs-bot matches use **2000 ms**
(`bot_match_decision_timeout_ms`), tournaments use **2000 ms**, and
human-vs-bot uses **10000 ms** (`bot_human_play_decision_timeout_ms`) to
give heavy sidecar models cold-start headroom. The budget covers everything
between the server sending `turn_request` and the server receiving
`turn_action`: WebSocket hops + SDK queue drain + your Python body + WS
serialize back. If you overshoot, the server applies the safe default
(`check` if legal, else `fold`) on your behalf and surfaces a `bot_error`
with `reason=bot_decision_timeout` to the human's UI (so the human knows
they're playing a stub, not your real strategy).

**How to detect.**

- Open the match replay → log drawer → **Decisions** tab. The
  `elapsed_ms` column is your `decide()` body's wall time. If it's above
  the budget, your strategy code is the bottleneck. If `elapsed_ms` is
  fine but `round_trip_ms` is over budget, queue drain or WS latency is
  eating you alive — see pitfall #17.
- Look for `FALLBACK` lines in `data/bot_logs/<match_id>-<pid>.log`. Each
  one corresponds to a single timeout.
- Per-bot dashboard at `/bots/<bot_id>/dashboard` shows aggregate
  fallback counts across matches.

**How to fix.**

1. **Profile once, not every hand.** Cache expensive lookups (preflop
   range charts, solver tables, opponent-model loads) in `on_match_start`,
   not inside `decide`.
2. **Return synchronously from `decide`.** Starting an `asyncio.Task` or
   a thread *inside* `decide` to do the real work does not help — the SDK
   blocks on `decide`'s return value.
3. **Add a hard internal cutoff.** Wrap your search in
   `asyncio.wait_for(search(), timeout=4.0)` or set a `Stop` flag that
   your evaluator polls; return your best-found action when it fires. The
   server's 5 s ceiling is hard. Internal 4 s + 1 s margin is a safe
   pattern.
4. **Verify locally first.** Time `decide()` directly over a few hundred
   scripted states (a plain loop with `time.monotonic()` is enough). If
   you're already over budget locally, you'll be over budget on the
   platform too.

**Cross-references.**

- `docs/DEV-MANUAL.md` §6 (Performance), §9.2, §9.3
- `docs/ERROR-CODES.md` — `BOT_003` (HTTP) +
  `bot_error.reason=bot_decision_timeout` (wire)
- Historical incident: CZ issue 1091

---

## 2. Card encoding mismatch (`As` vs `AS` vs `A♠`)

**Symptom.**

- Your evaluator scores `Ah` and `AH` differently, or crashes on `A♠`.
- Bot makes nonsense preflop decisions: folds AA, raises 72o.
- `KeyError: 'AH'` or `IndexError` inside your strength function on hand 1.

**Why it happens.**

The Chipzen wire format is fixed: every card is a two-character ASCII
string `[2-9TJQKA][cdhs]`. Specifically:

- Ranks are **uppercase letters**: `2 3 4 5 6 7 8 9 T J Q K A`. There is
  no `1`, no `10`.
- Suits are **lowercase letters**: `c d h s`. There are no Unicode
  symbols (`♣ ♦ ♥ ♠`) and no uppercase suits on the wire.

So an Ace of Spades is **`As`**, never `AS`, never `A♠`, never `aS`. This
is documented in `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` §1 ("Card
Notation"). Many open-source hand evaluators use different conventions:
[Treys](https://github.com/ihendley/treys) and `pokerkit` accept `As`;
the original `Deuces` library uses `As` too; but some libraries from
Texas Hold'em strategy projects use `Ah → A♥` Unicode rendering, and a
handful of poker engines normalize to all-uppercase `AS`.

**How to detect.**

- Print `state.your_hole_cards` and `state.board` once in `decide()`.
  Compare to the literal `^[2-9TJQKA][cdhs]$` regex.
- Bot log: `KeyError` or `ValueError` from your evaluator on the first
  hand.
- `chipzen-sdk validate <path>` runs the smoke test and catches
  evaluators that crash on a real protocol payload.

**How to fix.**

Add a normalization step at the boundary between the wire and your
strategy code:

```python
def to_eval_format(card: str) -> str:
    """Convert Chipzen wire ('As') to your evaluator's format.

    Adjust the right-hand side to match whichever evaluator you use.
    """
    rank, suit = card[0], card[1]
    # Treys / pokerkit / Deuces all accept this as-is.
    return f"{rank}{suit}"
```

If you adopted an evaluator that uses non-Chipzen conventions, write the
adapter once, not at every call site. Don't try to "fix" the wire format
on the way in — the protocol won't change to accommodate any single
library.

**Cross-references.**

- `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` §1 (Card Notation)
- `docs/DEV-MANUAL.md` §2.3 (`GameState`)
- the Python SDK's `Card.from_str` helper (parses the wire format)

---

## 3. Hand-evaluator format mismatch — pluggable evaluators don't match the wire format

**Symptom.**

- Bot ranks AA below 72o on the flop.
- Win-rate stuck near 50% against random, when it should be ~85%.
- Pre-flop hand-key lookups (`"AKs"`, `"AKo"`) return uniform garbage
  for any hand.

**Why it happens.**

Chipzen's SDK intentionally does **not** ship a built-in hand evaluator
— the protocol is game-agnostic at Layer 1, and bundling a poker
evaluator into the runtime would lock devs into one ranking convention.
You plug in your own (Treys, pokerkit, your custom 7-card lookup, etc).
The downside: any mismatch between the evaluator's expected input format
and what the wire delivers is silent. The evaluator just returns the
wrong rank.

The most common shape this takes: a library expects a 2-char string
like `"Ah"` but your code passes a tuple `("A", "h")`; or the library
ranks aces low (some old Lin Bin / "ace-to-five" lowball libs do this);
or the library uses bitstring encodings (`treys.Card.new("Ah")` ->
opaque int) and you're trying to compare opaque ints across libraries.

**How to detect.**

- Unit-test your evaluator against ten known fixtures before uploading.
  Royal flush > steel wheel > top set > two pair > high card. If any of
  those orderings comes out wrong, fix it locally.
- Add an assertion at startup: `assert evaluate("Ah As Kh Qh Jh".split())
  > evaluate("2c 2d 3c 3d 4s".split())`. Crashing on boot is louder than
  silently misranking.
- Run your evaluator over a scripted batch of known matchups (AA vs
  72o across random boards). A working evaluator wins the AA side
  decisively; a broken one comes out near 50/50.

**How to fix.**

Pick one evaluator, write one adapter function, and use it everywhere.
A typical Treys adapter:

```python
from treys import Card as TreysCard, Evaluator

_E = Evaluator()

def hand_strength(hole: list[str], board: list[str]) -> int:
    """Lower = better. Treys returns 1 for royal flush, 7462 for high card."""
    hole_t = [TreysCard.new(c) for c in hole]
    board_t = [TreysCard.new(c) for c in board]
    return _E.evaluate(board_t, hole_t)
```

Then your strategy code only ever sees a single integer with the same
sort order across all hands. The boundary is one function; bugs land
once and stay landed.

**Cross-references.**

- `docs/DEV-MANUAL.md` §4.3 (local harness uses a naive evaluator — do
  **not** trust local-harness win rates as ground truth for showdown
  outcomes).
- `examples/reference-bot/bot.py` — example of a no-evaluator strategy
  using only `hand_key()` shorthand (`AKs`, `AKo`).

---

## 4. Missing or stale `request_id` echo — every action gets rejected

**Symptom.**

- Bot log: repeated `action_rejected (Action ... -- N ms remaining,
  retrying with safe fallback)`.
- Match plays but your bot keeps falling back to check/fold even when
  you intended to raise.
- Auto-substitute streak limit eventually trips:
  `BOT_UNRESPONSIVE_AUTO_SUBSTITUTE_LIMIT` ends the match with
  `status=error`.

**Why it happens.**

Every `turn_request` message carries a `request_id` (a Layer 1 envelope
field). Your `turn_action` reply **must echo that exact `request_id`
verbatim**. The server uses it for:

- Correlation (which request your reply matches).
- Idempotency under reconnect (so a reply you re-send after WS reconnect
  isn't double-counted).
- Rejection retries (if the server replies `action_rejected`, you re-send
  with the **same** `request_id`, and the server treats it as the same
  decision, not a new turn).

If you reuse a stale `request_id` from a previous turn, the server
treats your action as stale and rejects it. If you omit `request_id`
entirely, the server can't correlate and rejects with
`reason=missing_request_id`.

**How to detect.**

- Bot log: `Action rejected (Stale request_id) -- N ms remaining`.
- Server log: `Action rejected: request_id mismatch (expected <X>, got
  <Y>)`.
- The Python SDK (`chipzen.Bot` + `run_bot`) handles `request_id`
  automatically — if you see this in a bot using the SDK, something
  about your runtime is bypassing the SDK's envelope handling. If you're
  writing a raw-WebSocket bot in a starter language (JS, Rust, raw
  Python), this is on you to wire up.

**How to fix.**

Capture `request_id` from every incoming `turn_request` and write it
back unchanged on the matching `turn_action`. From
`examples/reference-bot/bot.py`:

```python
elif mtype == "turn_request":
    state = msg.get("state", {})
    valid = msg.get("valid_actions", [])
    request_id = msg.get("request_id")  # capture
    action = decide(state, valid)
    await send_json(ws, {
        "type": "turn_action",
        "match_id": match_id,
        "request_id": request_id,        # MUST echo
        "action": action["action"],
        "params": action.get("params", {}),
    })
```

For `action_rejected` retries: re-send with the **same** `request_id`
that was rejected, with a corrected action, while `remaining_ms > 0`. Do
not increment, hash, or generate a new ID for retries — the server keys
the in-flight decision off `request_id`, so a new ID looks like a new
turn (which the server didn't open).

**Cross-references.**

- `docs/protocol/TRANSPORT-PROTOCOL.md` §7 (Message Envelope)
- `docs/DEV-MANUAL.md` §3.2 (Envelope fields), §3.3 (Error handling)
- Historical bugs:
  CZ issue 1721
  (SDK retry handler did not include `valid_actions` in the rejection
  payload),
  CZ issue 1779
  ("Duplicate action for decision point" — bot resent with a new id).

---

## 5. Raise amount sent as increment instead of total bet

**Symptom.**

- Bot log: `Action rejected (Raise 50 outside min/max [200, 10000])`.
- You meant to raise *to* 200; you sent `amount: 50`.
- Bot accidentally bets all-in: meant to raise by 200; sent `amount: 10000`.

**Why it happens.**

Chipzen raise amounts are **total bet sizes** — the chips you want in
the pot *after* the action, not the increment above the current bet.
This is consistent with the way most online poker UIs display amounts
("Raise to 200"), but it's the opposite of how many people verbalize
poker out loud ("raise 200 more"). It's also the opposite of some other
poker APIs (notably the original ACPC/2017 wire format used increments).

Concretely: if `to_call = 100` and you want to put another 100 chips on
top, the right wire value is `amount: 200`, not `amount: 100`.

The valid range is in `GameState.min_raise` and `GameState.max_raise`,
which the server includes in every `turn_request`. Both are **total**
bet sizes. If `min_raise == 0` and `max_raise == 0`, raising isn't legal
this turn — use a different action.

**How to detect.**

- Bot log: `Action rejected (Raise <amount> outside min/max [<min>,
  <max>])`.
- Repeated `action_rejected` followed by SDK falling back to check/fold.
- Local: print `min_raise`, `max_raise`, `your_intended_amount` from
  `decide` and verify the math.

**How to fix.**

Use the helper if you're on the Python SDK:

```python
return Action.raise_to(amount)  # amount is total bet
```

Or, if writing raw WebSocket, compute the total bet explicitly:

```python
# Want to "raise 100 more" on top of a 100 to_call:
target = state.to_call + 100  # 200 total
target = max(state.min_raise, min(state.max_raise, target))
return {"action": "raise", "params": {"amount": target}}
```

Always **clamp to `[min_raise, max_raise]`** as a safety net. Pot-sized
raises in particular can over-shoot `max_raise` (your effective
all-in) — when that happens, use `Action.all_in()`, not
`raise_to(max_raise+1)`.

**Cross-references.**

- `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` §2 (Action Vocabulary)
- `docs/DEV-MANUAL.md` §2.3 (`min_raise` / `max_raise`), §2.4 (`Action`),
  §3.4 (Action vocabulary)
- `docs/ERROR-CODES.md` — `bot_error.reason=bot_invalid_action`

---

## 6. Illegal action: `check` when there's a bet to call (or vice versa)

**Symptom.**

- Bot log: `Action rejected (Action 'check' is not valid. Valid actions:
  ['fold', 'call', 'raise'])`.
- Repeated rejections at the **first submission** for each decision —
  not just after a retry.
- Match runs to completion but the bot looks like it folds every other
  hand.

**Why it happens.**

`state.valid_actions` carries the canonical legal-action set for *this*
turn, as computed by the server. If you ignore it (or read a stale copy
from a previous turn) and pick an action that isn't currently legal,
the server rejects with `reason=bot_invalid_action`.

The most common shape: hardcoded "always try `check` first, fall back to
`fold`" logic that never consults `state.valid_actions`. When facing a
bet (`to_call > 0`), `check` is illegal — the legal set is
`{fold, call, raise}`. When checking through (`to_call == 0`), `fold`
is illegal — the legal set is `{check, raise}` (you can't surrender a
hand you owe nothing on).

Real-world incident:
CZ issue 1765 caught a
house-bot adapter that emitted illegal first-actions across multiple
matches because `state.valid_actions` was being parsed as empty due to
an SDK schema drift between v0.2.0 and v0.2.1. The adapter's fallback
heuristic then synthesized an action without cross-checking against the
real legal set — producing systematically wrong first-submissions whose
legal sets didn't even intersect.

**How to detect.**

- Bot log: `Action rejected (Action ... is not valid. Valid actions:
  [...])`. If you see this on the *first* submission for a decision
  point, your code isn't reading `state.valid_actions`.
- Add an assertion early in `decide`:
  ```python
  assert state.valid_actions, f"Empty valid_actions: {state}"
  ```
  If this trips, your wire-parsing is the bug, not your strategy.

**How to fix.**

1. **Always branch on `state.valid_actions`.** Never assume `check`,
   `fold`, `call`, or `raise` is legal — check the array.
2. **Fall back conservatively.** If your strategy picks an action that
   *might* be illegal, the safe last resort is:
   - `check` if `"check" in state.valid_actions`
   - else `fold` if `"fold" in state.valid_actions`
   - else `call` (which is in the legal set on every checked-down or
     bet-faced turn — `call` becomes a 0-cost "stay in" when there's
     nothing to call)
3. **Validate on output.** Before sending `turn_action`, assert that
   `action.action in state.valid_actions`. Catching the bug at your own
   boundary is faster than catching it via `action_rejected`.

**Cross-references.**

- `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` §2 (Action Vocabulary)
- `docs/DEV-MANUAL.md` §2.3 (`valid_actions`), §2.4 (`Action`)
- Historical incidents:
  CZ issue 1765,
  CZ issue 1721

---

## 7. No error handling around WebSocket disconnect — bot dies on first dropped frame

**Symptom.**

- Container exits with code 1 after a brief network hiccup.
- API log: `bot_connector_disconnected_midmatch` for your participant.
- Human's UI: `bot_error` toast with `reason=bot_connector_disconnected_midmatch`.
- Match completes with one bot eliminated by auto-fold for every
  post-disconnect hand — exactly the silent-fold-cascade pattern from
  CZ issue 1682.

**Why it happens.**

WebSocket connections drop. Container networking glitches; the server
restarts a stuck task; a TCP keepalive expires under an idle proxy. Most
naive raw-WebSocket clients propagate the disconnect as a hard
exception, the bot's `main` exits, and the container terminates — the
match runner classifies the disconnect as a forfeit and auto-folds the
seat for every subsequent action.

The Python SDK (`chipzen.client.run_bot`) handles this for you: it
reconnects with exponential backoff (5 attempts by default, configurable
via `RetryPolicy`), with the `reconnected` message carrying a
`pending_request` so you can finish the in-flight turn without losing
the budget. If you're using a starter SDK (JS, Rust) or a hand-rolled
client, you own the reconnect logic.

The server's reconnect grace is generous —
`ws_reconnect_grace_seconds=300` (5 minutes) in `config.py`. So a
healthy reconnect loop with exponential backoff almost always succeeds
inside one match.

**How to detect.**

- Container exits prematurely; `docker logs <id>` shows a stack trace
  ending in `websockets.exceptions.ConnectionClosedError` or similar.
- API log: `bot_connector_disconnected_midmatch` (this is the canonical
  `bot_error.reason`).
- Match-runner: status is `completed` but the loser's stack went to zero
  via 100+ auto-folds — exactly the fold-cascade pattern caught by the
  `BOT_UNRESPONSIVE_AUTO_SUBSTITUTE_LIMIT` guard added in #1682. The
  match will now end with `status=error` after 15 consecutive
  auto-substituted actions, so you won't silently lose the whole stack
  to a single dropped frame anymore — but you also won't recover the
  match.

**How to fix.**

Use the Python SDK runner whenever possible — it's already correct.

For starter / raw clients, wrap your message loop in a reconnect loop:

```python
import asyncio
import websockets

MAX_RETRIES = 5
BASE_BACKOFF = 1.0

async def main(url, token):
    retries = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                retries = 0  # reset after a clean connect
                await handshake(ws, token)
                await message_loop(ws)
        except (websockets.exceptions.ConnectionClosedError, OSError) as e:
            if retries >= MAX_RETRIES:
                log(f"giving up after {MAX_RETRIES} reconnect attempts: {e}")
                raise
            wait = min(BASE_BACKOFF * (2 ** retries), 30)
            log(f"reconnect attempt {retries+1} in {wait:.1f}s: {e}")
            await asyncio.sleep(wait)
            retries += 1
        except Exception as e:
            log(f"fatal: {e}")
            raise
```

Key points:

- Cap retries (3-5 is reasonable; the server's reconnect grace is 300 s).
- Exponential backoff with a cap (don't hammer the server on
  back-to-back failures).
- Reset the counter after a clean connect, otherwise the second drop in
  the same match triggers giveup with one retry left.
- Don't blanket-catch and ignore — let unexpected exceptions propagate
  so you can fix them.

**Cross-references.**

- `docs/protocol/TRANSPORT-PROTOCOL.md` §11 (Reconnection)
- `docs/DEV-MANUAL.md` §2.5 (Runner) — the SDK handles this for you
- `docs/ERROR-CODES.md` —
  `bot_error.reason=bot_connector_disconnected_midmatch`
- Historical incident:
  CZ issue 1682 (the
  fold-cascade masking bug that motivated the consecutive-auto-fold
  limit)

---

## 8. Bot hangs after winning a hand — state-machine assumes the match continues forever

**Symptom.**

- Bot wins a hand decisively (opponent eliminated, all chips in your
  stack) and then never sends another action.
- API log: `bot_decision_timeout` on the *next* hand that doesn't
  arrive.
- Match status: `completed` after the auto-fold timeout streak trips,
  with the *winner* of the elimination hand showing as the
  `BOT_UNRESPONSIVE` side.

**Why it happens.**

Heads-up matches end when one bot is eliminated. Pre-elimination
sequence: `round_result` (the win) → `match_end`. If your state machine
treats every `round_result` as a setup for the next `round_start` and
blocks on receiving one, you'll hang at the end of the match because
the server has sent `match_end` and is waiting for your container to
exit cleanly.

The shape this most often takes: a synchronous `while True` loop that
calls `recv()` and dispatches by message type, with no explicit handler
for `match_end`. The next `recv()` blocks indefinitely; the container
process never exits; the executor's container-cleanup eventually kills
it with `SIGKILL`. The replay shows the bot "won the last hand" but
fails to play a match it doesn't realize it's already won.

Until CZ issue 1597 shipped
in v0.3.46-alpha (2026-04-30), `match_start.game_config` carried a
`total_hands` field that bots could (wrongly) use to terminate. That
field is gone — matches play until elimination, with
`_MATCH_HAND_LIMIT_SAFETY = 1000` as the only non-elimination terminal
condition. Any bot logic that branches on remaining-hands needs to be
removed.

**How to detect.**

- Add a handler for `match_end` that logs and breaks the message loop.
  If you never see the log line on completed matches, the handler isn't
  wired.
- Container `STATE.FinishedAt` is later than the match's
  `completed_at` — the server moved on, your bot didn't.

**How to fix.**

Handle `match_end` explicitly:

```python
async for raw in ws:
    msg = json.loads(raw)
    mtype = msg.get("type")
    # ... other handlers ...
    elif mtype == "match_end":
        log(f"Match ended: reason={msg.get('reason')}")
        break  # exit the loop; let the container terminate cleanly
```

In the Python SDK, override `on_match_end(results)` to do any final
bookkeeping (flush opponent-model caches, write a summary) — the SDK
handles the clean exit itself.

Do not block on a specific message type. Always `recv()` in a loop,
dispatch by `type`, and treat unknown types as `pass` (forward-compat —
the protocol may add new message types in minor versions).

**Cross-references.**

- `docs/protocol/TRANSPORT-PROTOCOL.md` §6 (Server State Machine)
- `docs/DEV-MANUAL.md` §2.2 (`on_match_end`)
- Removed field: CZ issue 1597
  (`total_hands` no longer in `game_config`)

---

## 9. Trying to write GTO/CFR from scratch — quality cliff before any results land

**Symptom.**

- Two-week implementation of vanilla CFR; bot still loses to a
  hand-tuned check-call bot.
- Solver runs are taking 12+ hours per matchup with no convergence in
  sight.
- You've rewritten the same abstraction three times trying to bound
  memory.

**Why it happens.**

Real CFR-class solvers are a multi-year engineering project. The
platform's house solver bot took over a year of full-time work to
reach competitive strength against scripted opponents, and it is
still being actively developed. Naive CFR scales
poorly: information-set memory grows super-linearly with abstraction
granularity, and any non-trivial NLHE state space requires action
abstraction + card abstraction + bucketing schemes that each take weeks
to implement and validate.

It's a well-known dev failure mode: if you've never built a solver
before, don't start with one. The cliff is real — months of work
before you can outperform a 100-line tight-aggressive rule bot.

**How to detect.**

You probably know already, but signals to watch for:

- You're more than three weeks in and have not yet beaten the reference
  bot (`examples/reference-bot/`) over 1000 hands of local play.
- You're tuning hyperparameters that you can't articulate the meaning
  of.
- You're spending more time on abstraction tooling than on
  strategy-level decisions.

**How to fix.**

Reorder the work:

1. **Ship a tight-aggressive rule bot first.** The starter at
   `examples/reference-bot/bot.py` is ~85 lines and already beats random.
   Tighten the preflop ranges, add positional awareness, add an
   opponent-betting-frequency tracker, and you'll outperform anything
   under three weeks of CFR development.
2. **Layer a Monte Carlo / direct-simulation equity estimator.** Cheap,
   robust, gets you postflop fold-equity intuition without solver
   complexity.
3. **Only then consider learned/solver components,** and start from
   proven baselines (Slumbot/RPSB-style references) rather than a
   from-scratch abstraction stack.

The funnel is: templates → hand-tuned bots → solver-class bots.
Skipping the first two steps doesn't get you to step three faster.

**Cross-references.**

- `QUICKSTART.md` (reference bot)
- `examples/reference-bot/bot.py` (tight-aggressive starter)

---

## 10. Assuming infinite decision time / misunderstanding the compute-fairness model

**Symptom.**

- Bot uses 4500 ms on every postflop turn even when there's nothing
  hard to decide.
- Or: bot uses 50 ms uniformly even on river all-in decisions.
- Win-rate plateaus low; opponents adapt to your timing tells.

**Why it happens.**

Chipzen's compute-fairness model is a single hard budget per decision
(see pitfall #1 for the exact numbers). It is **not** a time bank that
banks unused budget across turns, and it is **not** an explicit
fair-allocation mechanism that grants more time on harder decisions.
That has two implications most devs miss:

1. **You don't need to use the full budget on easy decisions.** A snap
   check on the BB with bottom pair is identical strategically whether
   it takes 50 ms or 4500 ms. Burning compute every turn doesn't
   improve your strategy and risks tripping the budget on the one turn
   that does need real search.
2. **Hard turns get exactly as much time as easy ones.** River all-in
   decisions with a complex board don't get more headroom than preflop
   fold-or-call-22 spots. Budget your search to the *hardest* turn you
   expect to face, not the average.

This is a deliberate platform design choice: a tight budget rewards
search efficiency, learned policies, and fast heuristics over slow
external calls.

**How to detect.**

- Per-bot dashboard at `/bots/<bot_id>/dashboard` — look at avg / p99 /
  max decision latency. If avg is anywhere near max budget, you're
  burning compute on easy decisions.
- `DECIDE` log lines: compare `elapsed_ms` across easy (preflop fold)
  and hard (river all-in) spots in the same match. They should differ
  by an order of magnitude.

**How to fix.**

- **Bucket your decisions by difficulty.** Trivial preflop fold/raise
  decisions: heuristics in < 50 ms. Postflop continuation-bet decisions:
  cached preflop ranges + a 1-card lookahead in < 200 ms. River
  decisions facing aggression: full search up to ~3000 ms.
- **Treat the 5000 ms ceiling as a brick wall.** Set an internal soft
  cap of 4000 ms with an anytime fallback — the moment your search
  passes 4000 ms, return the best-found action immediately and emit a
  log line noting the early termination. You can mine those for "spots
  where my search is too slow" data.
- **Don't sleep to look human.** Pacing is the server's job
  (`spectated_match_action_delay_ms=1500` provides the spectator-friendly
  delay). Your bot should respond as fast as it can; the server
  inserts visible pacing for the human-vs-bot UI.

**Cross-references.**

- `DEV-MANUAL.md` §6 (Performance), especially §6.1 (Budget
  breakdown) and §6.4 (Tips for staying under budget)

---

## 11. Hardcoded blinds — bot mis-sizes after blind escalation

**Symptom.**

- Bot's "min-raise = 50" logic works hand 1, breaks hand 30 when blinds
  have escalated to 200/400.
- Bot folds AA on hand 100 because "stack < 20 BB → tournament shove
  mode" never fires when the blinds it expected don't materialize.

**Why it happens.**

Chipzen tournaments and longer matches escalate blinds (see
`docs/REQUIREMENTS.md` and the matchmaking tournament runner). The
authoritative blind sizes are in `state.action_history` (look for the
synthetic `post_small_blind` / `post_big_blind` entries at the start of
each hand) and in `match_info["game_config"]` for the starting sizes.
If you hardcode blinds at match start and never update them, your
post-flop sizing logic drifts as the structure escalates.

**How to detect.**

- Add a log line in `decide` that prints the *current-hand* blinds
  (extracted from `state.action_history`). If the value stays
  constant across a 100-hand match with known escalation, your code
  isn't reading them dynamically.
- For tournaments, check `match_info["game_config"]["blind_schedule"]`
  (when present) — it gives you the escalation schedule explicitly.

**How to fix.**

Read blinds from `state` each hand, never cache them across hands:

```python
def current_blinds(state) -> tuple[int, int]:
    sb = bb = 0
    for entry in state.action_history:
        if entry["action"] == "post_small_blind":
            sb = entry["amount"]
        elif entry["action"] == "post_big_blind":
            bb = entry["amount"]
        else:
            break  # past the synthetic blind entries
    return sb, bb
```

Compute stack-in-BB **every hand** for ICM / push-fold logic:

```python
sb, bb = current_blinds(state)
stack_in_bb = state.your_stack / bb
```

This also future-proofs you against blind-schedule changes — the server
controls the schedule, and your bot just reads what the server posted.

**Cross-references.**

- `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` §2 (Synthetic Actions)
- `docs/DEV-MANUAL.md` §2.3 (`action_history`)

---

## 12. Greedy resource use triggers sandbox kill — silent container exit

**Symptom.**

- Container exits with code 137 (SIGKILL — OOM) or 139 (SIGSEGV) mid-match.
- `docker logs` shows the bot starting normally and then nothing.
- `bot_error.reason=bot_connector_disconnected_midmatch` from the
  server's perspective.

**Why it happens.**

Per-bot resource limits (platform-enforced). Per the platform decision
/ CZ issue 2404 free-tier limits design, these are **platform-wide** —
identical across tiers (pay-to-win on per-match performance is locked
out by the monetization compass):

| Resource | All tiers |
|---|---|
| CPU cores | 0.5 |
| Memory | 256 MB |
| tmpfs `/tmp` | 10 MB |

Beyond these caps, the kernel OOM-kills your container. The bot dies
mid-match without writing a Python traceback — the kill is
process-external. Common causes:

- Loading a multi-GB model (won't fit in 256 MB — the platform-wide cap).
- Caching every observed hand history in a `list` that grows without
  bound over a 200-hand match.
- Running multiple parallel search threads on a 0.5-CPU allocation —
  the threads thrash without making progress.

Additionally, the executor applies a seccomp profile
(`seccomp-bot.json`, applied platform-side) that
whitelists a minimal syscall set. C extensions that use unusual
syscalls can crash the container silently on startup, *before* Python
even starts — see pitfall in `docs/DEV-MANUAL.md` §7.4.

**How to detect.**

- `docker inspect <container_id>` → `State.ExitCode` is 137 (OOM) or
  139 (segfault).
- Bot's logs cut off mid-stream with no exception trace.
- Per-bot dashboard shows the bot disconnecting consistently around
  hand N (correlate to total-allocated-memory growth).

**How to fix.**

1. **Size your image to the platform cap.** The upload cap is
   platform-wide (no tier-keyed multiplier): **250 MB compressed**.
   The house solver bot (Debian slim + numpy + scipy + a 900k-state
   CFR checkpoint) is ~136 MB compressed — comfortably under that cap.
   The reference bot is ~20 MB compressed and runs comfortably in
   256 MB RAM. (Per-tier resource caps no longer exist — see the
   resource table above.)
2. **Cap your memory use explicitly.** Use `resource.setrlimit` (Linux)
   or watch `psutil.Process().memory_info().rss` and bail out early.
3. **Don't accumulate state.** Persist per-match work into a bounded
   structure (LRU cache, fixed-size deque). Flush at `on_match_end`.
4. **Match parallelism to your CPU allocation.** On 0.5 vCPU, single-
   threaded code is usually faster than 4 threads contending. Set
   `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1` in your Dockerfile
   ENV.

**Cross-references.**

- `docs/DEV-MANUAL.md` §7.2 (Resource limits per tier)
- Historical issue: CZ issue 1295
  (bot container resource limit policies)

---

## 13. Writing outside `/tmp` — `Errno 30 Read-only file system`

**Symptom.**

- Container starts, logs the handshake, dies with
  `OSError: [Errno 30] Read-only file system: '/app/cache.pkl'`.
- Or: `PermissionError` writing to `/home/bot/.cache/`.

**Why it happens.**

Bot containers run with `--read-only` (root filesystem is read-only) +
`--user 10001:10001` (non-root) + `--cap-drop=ALL`. `/tmp` is mounted
as a tmpfs scratch volume — it's the **only** writable path. Anywhere
else raises `Errno 30`. The tmpfs is per-task and disappears when the
container stops, so don't put cross-match state there.

This is documented in `DEV-MANUAL.md` §7.1 contract item #5 and
enforced by the platform's container runner.

**How to detect.**

- Container log: `OSError: [Errno 30] Read-only file system: ...` or
  `PermissionError`.
- Add `TMPDIR=/tmp` to your env in the Dockerfile and use
  `tempfile.NamedTemporaryFile()` everywhere — it picks up `TMPDIR`
  automatically.

**How to fix.**

- **All scratch writes go to `/tmp`.** Caches, intermediate files,
  log spillover. Size your scratch use to the platform-wide tmpfs cap
  (10 MB — same value for all tiers).
- **Use the `tempfile` module** (Python) or `os.tmpdir()` (Node) — they
  respect `TMPDIR` and avoid hardcoding paths.
- **Don't write to `/app`, `/home`, `/var`.** If your library expects
  to (e.g. `huggingface` caching to `~/.cache/huggingface`), set the
  relevant env var in your Dockerfile:
  ```dockerfile
  ENV HF_HOME=/tmp/huggingface
  ENV TRANSFORMERS_CACHE=/tmp/huggingface
  ```
- **Pre-bake static artifacts into the image at build time.** Anything
  you can compute or download at build time should be in the image
  layer (read-only is fine — you're reading) so you don't have to
  re-fetch it on every match.

**Cross-references.**

- `DEV-MANUAL.md` §7.1 (Required contract — item 5)

---

## 14. Image exceeds platform upload cap — rejected at upload

**Symptom.**

- Upload returns HTTP 413 with detail `"Archive exceeds … cap"`.
- Or upload status flips `pending_review → reviewing → rejected`
  with `rejection_reason` mentioning the cap.

**Why it happens.**

Upload caps (platform-wide, — platform-wide, NOT tier-keyed):

| Cap | Value | Knob |
|---|---|---|
| Compressed | 250 MB | `bot_archive_max_compressed_mb` |
| Decompressed (security ceiling) | 1500 MB | `bot_max_decompressed_mb` |

Both caps apply uniformly across tiers. The compressed cap is what the
upload pipeline checks first, so a small .tar.gz that decompresses to
above 1500 MB fails the second check.

The house solver bot (Debian slim + numpy + scipy + 900k CFR checkpoint)
is ~136 MB compressed: comfortably under the 250 MB cap. The reference
bot (Alpine + SDK + 60-line `bot.py`) is ~20 MB compressed.

**How to detect.**

```bash
ls -lh my-bot.tar.gz                 # compressed
gunzip -l my-bot.tar.gz | tail -1    # uncompressed
docker images my-bot:v1 --format '{{.Size}}'  # layered size
```

**How to fix.**

- **`python:3.11-alpine` instead of `slim`.** ~50 MB base vs ~125 MB.
  Only works with pure-Python deps; numpy/scipy need musl wheels or a
  build chain.
- **Multi-stage build.** Compile deps in a builder stage, copy only the
  installed packages into the final stage.
- **Strip pycache + tests.** See
  [`examples/reference-bot/Dockerfile`](../examples/reference-bot/Dockerfile)
  for the pattern.
- **Don't bake training data into the image.** Move large model
  artifacts to a download-at-build-time pattern (curl from your own
  bucket → checksum → write to image). The smaller the image, the
  faster the executor pulls it on cold start, which also helps
  pitfall #15.

**Cross-references.**

- `docs/DEV-MANUAL.md` §7.2 (Resource limits), §7.3 (Size budget)

---

## 15. Cold start > 15 s — `bot_container_failed_to_attach`

**Symptom.**

- Bot upload approves, but on the *first* play attempt the human's UI
  shows: `bot_error: bot_container_failed_to_attach. Bot container for
  session X did not attach within 15s`.
- Subsequent plays may or may not succeed depending on what's cached
  on the host.

**Why it happens.**

The executor waits up to ~15 s for your container to send its `hello`
after launch. The clock starts the moment the executor calls
`docker run` (or `ecs RunTask`). That window covers:

- Image pull (first-time) — bigger images take longer.
- Python interpreter startup.
- Import-time work (loading models, opening checkpoint files,
  initializing CUDA, etc.).
- Establishing the WebSocket and sending the `authenticate` + `hello`
  messages.

Heavyweight bots that load a multi-GB model at import time can blow
the budget on the first match where the image isn't host-cached.

**How to detect.**

- API log: `bot_container_failed_to_attach`.
- Add a print right after import:
  ```python
  import time
  print(f"[bot] import done at {time.monotonic():.2f}", file=sys.stderr)
  ```
  If you see this line more than ~10 s after the executor's "Launched
  bot X" line, you're eating most of the attach budget on imports.

**How to fix.**

- **Move expensive init to `on_match_start`**, after the handshake
  completes. The 5000 ms `decide` budget then includes some of that
  cost, but the 15 s attach budget no longer does.
- **Pre-bake artifacts into the image** so they're available without
  download/decompression at startup.
- **Use Alpine + pure-Python deps** when feasible — `python:3.11-alpine`
  startup is dramatically faster than `slim` for cold containers.
- **Avoid heavy import-time side effects.** `import numpy` is fine;
  `import some_lib_that_loads_a_4gb_model` at module top-level is not.
  Lazy-import what you can.

**Cross-references.**

- `docs/DEV-MANUAL.md` §7.1 (Required contract — item 3), §9.5
  (Container dies immediately with no logs)
- Historical issue: CZ issue 1086

---

## 16. Wrong WS bind address on native Linux — bot can't reach the server

**Symptom.**

- Container log: `ConnectionRefusedError: [Errno 111] Connection refused`
  on `websockets.connect`.
- API log: `Launched bot X` followed by `bot_container_failed_to_attach`,
  no handshake.

**Why it happens.**

On native Linux with `--network=host`, the container's loopback
(`127.0.0.1`) is the container's loopback, not the host's. The
API is bound to `127.0.0.1:8001` by default; the container can't reach
it. Docker Desktop (Windows/macOS) injects `host.docker.internal`
automatically, so `127.0.0.1` "works" — but actually it works because
Docker Desktop is using a network bridge under the hood.

**How to detect.**

- You're on native Linux.
- You see `ConnectionRefusedError` to `127.0.0.1` or `localhost`.
- The same image works on Docker Desktop and fails on a Linux server.

**How to fix.**

- **Bind the API to `0.0.0.0`** instead of `127.0.0.1` on native Linux,
  or
- **Use `host.docker.internal`** in your `CHIPZEN_WS_URL` (works on
  Docker Desktop; configure manually on native Linux:
  `--add-host=host.docker.internal:host-gateway`).
- **Check `CHIPZEN_WS_URL` for stale values.** If you copy-pasted a
  production URL into a local test, the port might be wrong (API
  defaults to 8001 in dev). If you copy-pasted from a teammate's
  staging notes, the host is wrong.

**Cross-references.**

- `docs/DEV-MANUAL.md` §9.4 ("Bot can't reach the server")
- `sdk/QUICKSTART.md` step 7 ("Common first-timer mistakes" — API bind
  address)

---

## 17. Expensive work inside `on_turn_result` / `on_phase_change` — drains your decide budget

**Symptom.**

- `DECIDE` log line: `elapsed_ms=200`, but server-side
  `round-trip = 3500ms`.
- Per-decision dashboard: `drain_ms` is a large fraction of
  `elapsed_ms` (often > 50%).
- Bot looks correct but slow; postflop decisions hit the budget that
  preflop never would.

**Why it happens.**

The SDK client processes WebSocket messages serially on a single asyncio
task. When a `turn_request` arrives after a burst of `turn_result` /
`phase_change` messages (which happens every time an opponent acts or
the board changes), the SDK calls your `on_turn_result` /
`on_phase_change` hooks **before** dispatching the `turn_request` to
`decide`. If those hooks do meaningful work — belief-tracker updates,
neural-net inference, hand-history persistence — the queue drain eats
your decision budget.

This is exactly the house bot's drain fix
(CZ issue 1731): the
adapter's `_drain_observes` was synchronously processing every queued
`observe_action` before `decide` could start, and on busy multi-action
streets the drain dominated.

**How to detect.**

- Open the match replay → log drawer → **Decisions** tab. The
  `drain_ms` column is the time spent processing queued events before
  search started.
- If `drain_ms` is > 20% of `elapsed_ms` consistently, your hooks are
  the bottleneck, not your strategy code.

**How to fix.**

1. **Keep hooks cheap.** Defer expensive inference out of
   `on_turn_result`. Queue the raw observation (one append) and process
   the queue lazily inside `decide`.
2. **Background thread for genuinely parallel work.** The SDK does not
   start threads for you. If you spawn one, you own its lifecycle. Use
   `asyncio.to_thread` or `loop.run_in_executor` to push the work onto
   a thread pool while `decide` runs.
3. **Cap total drain time.** Even if your observation processing is
   fast, processing 20 queued observations at 50 ms each costs 1 s of
   your budget. Bound the per-decide drain (e.g. process at most 5
   queued events before yielding to `decide`).

**Cross-references.**

- `docs/DEV-MANUAL.md` §6.3 (Why the queue drains first)
- `docs/DEV-MANUAL.md` §5.2 (`DECIDE` trace format — `drain_ms` field)
- Historical incidents:
  CZ issue 1093 (made
  `on_turn_result` async so decide starts immediately),
  CZ issue 1731 (cap
  drain total time)

---

## 18. Branching on `total_hands` or remaining-hands — field no longer exists

**Symptom.**

- Bot crashes on first `match_start` with
  `KeyError: 'total_hands'` or `TypeError: 'NoneType'`.
- Or: tournament-style push-fold logic never activates because the
  "we're close to the end" trigger never fires.

**Why it happens.**

Up to v0.3.45-alpha, `match_start.game_config` carried a `total_hands`
field bots could read to plan endgame strategy. As of v0.3.46-alpha
(deployed 2026-04-30 via
CZ issue 1597), the field
is gone. Matches play **until one bot is eliminated**, with
`_MATCH_HAND_LIMIT_SAFETY = 1000` as the only non-elimination terminal
condition. Bots should not branch on remaining-hands.

This catches devs who copied an older starter, or whose AI assistant
generated code based on the older protocol shape.

**How to detect.**

- Bot crashes on `match_start` with `KeyError` on `total_hands`.
- Bot has an unused "endgame mode" code path that never activates.

**How to fix.**

Drop `total_hands` logic entirely. Branch on the stack ratio instead:

```python
def on_match_start(self, match_info: dict) -> None:
    cfg = match_info.get("game_config", {})
    self.starting_stack = cfg.get("starting_stack", 10000)
    self.starting_bb = cfg.get("big_blind", 100)

def decide(self, state):
    bb = current_blinds(state)[1]  # see pitfall #11
    stack_in_bb = state.your_stack / bb
    if stack_in_bb < 15:
        # tournament push-fold mode
        ...
```

`stack_in_bb` is a robust proxy: short stacks correspond to "late in
the match" reliably; you don't need to know the hand count.

**Cross-references.**

- `docs/DEV-MANUAL.md` §2.2 (`on_match_start` — explicit "no total_hands
  anymore" note)
- Removed field: CZ issue 1597

---

## 19. Persisting per-match state across matches by accident

**Symptom.**

- Bot wins hand 1 of match 1, then plays like it's holding an opponent
  model that doesn't match any opponent it has ever seen.
- Memory grows monotonically across matches when running the bot
  long-running (sidecar mode).

**Why it happens.**

The Python SDK creates a single `Bot` instance and reuses it across
matches if your runtime is long-lived (e.g. a sidecar). Anything
you store on `self.foo` in `__init__` or in `on_match_start` is **not**
reset between matches unless you reset it explicitly.

Most starter bots don't hit this because their containers exit after
one match. But it bites anyone running long-lived bot containers
(sidecar patterns and custom long-lived task patterns).

**How to detect.**

- Memory growth across matches when running a sidecar — `psutil`
  RSS climbs without bound.
- Opponent model from the previous opponent influences the next
  opponent's decisions (you can see this in DECIDE traces if your
  bot logs opponent-state).

**How to fix.**

Always reset per-match state in `on_match_start`:

```python
class MyBot(Bot):
    def __init__(self):
        super().__init__()
        # everything per-match goes through reset()
        self._reset_state()

    def _reset_state(self) -> None:
        self.opponent_actions = []
        self.belief_tracker = BeliefTracker.fresh()
        self.hand_history = []

    def on_match_start(self, match_info: dict) -> None:
        self._reset_state()  # critical — never trust __init__ alone
```

For the same reason, write to `on_match_end` for cleanup that needs to
happen before the *next* match — e.g. flushing learned-state to `/tmp`,
emitting a summary log line.

**Cross-references.**

- `docs/DEV-MANUAL.md` §2.2 (`on_match_start` / `on_match_end`)

---

## 20. Forgetting `python -u` (unbuffered stdout) — log lines silently dropped

**Symptom.**

- `docker logs <id>` shows nothing or shows the first few lines but not
  the rest.
- Log drawer in the match-replay UI shows "(no log output)" even
  though you added `print` everywhere.
- The bot's container exits and its last 30+ log lines are gone.

**Why it happens.**

Python's stdout is line-buffered when attached to a terminal but
**fully buffered** when attached to a pipe or file. Docker's log
capture is a pipe. Without `-u` (unbuffered) or
`PYTHONUNBUFFERED=1` in the env, log lines sit in libc's stdio buffer
until either the buffer fills, you flush manually, or the process exits
cleanly. Containers that crash mid-match drop everything in the buffer.

**How to detect.**

- Log drawer shows fewer lines than your `print` calls predict.
- `docker logs` returns less output than you expect.
- Container exits with exit code != 0 and the last few seconds of logs
  are gone.

**How to fix.**

In your Dockerfile:

```dockerfile
# Either of these works; -u is the most idiomatic.
ENTRYPOINT ["python", "-u", "/bot/bot.py"]
# OR
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/bot/bot.py"]
```

For Node, set `process.stdout.write` directly or use `console.log` (which
is line-buffered against TTYs but flushes more aggressively than Python).
For Rust, prefer `eprintln!` over `println!` for diagnostic output —
stderr is unbuffered by default.

**Cross-references.**

- `docs/DEV-MANUAL.md` §7.1 (Required contract — item 1: ENTRYPOINT
  uses `python -u`)
- `sdk/QUICKSTART.md` step 10 (Inspect logs)

---

## Reporting a new pitfall

If you hit a failure mode that isn't on this list, please file an issue
on [`chipzen-ai/chipzen-sdk`](https://github.com/chipzen-ai/chipzen-sdk/issues)
with the `documentation` label and a title starting `pitfall:` — e.g.
`pitfall: my bot crashes when ante is non-zero`. Include:

1. The match ID (visible in the log drawer URL).
2. The bot log excerpt showing the symptom.
3. The fix you applied (so the doc can land both the diagnosis *and*
   the resolution in one go).

This doc is append-only by design — keep entries even after the
underlying bug is fixed platform-side, because old SDK versions and old
bot images keep tripping the same patterns in the wild.

---

*Last updated 2026-06-12. Sourced from real alpha incidents.*
