# Big Bad Bots Game State Protocol (Layer 2)

**Date:** 2026-09-29
**Status:** Draft. No Big Bad Bots table exists on the platform yet.
**Version:** 1.0 draft (Layer 1 protocol version is **unchanged at `1.0`**)

> **Provisional values.** Every number in this document marked **provisional** is a placeholder
> pending a rules decision, and may change before rule set `bigbadbots-1.0` is frozen:
>
> | Value | Placeholder | Decision |
> |---|---|---|
> | Per-tick deadline | **100 ms** (provisional) | D-01 |
> | Time bank | **3 s per seat per round**, reset every round (provisional) | D-01 |
> | Default action on a missed tick | repeat `block` / `move_in` / `move_out`, otherwise `idle` (provisional) | D-01 |
> | Disconnect grace | **5 s** of missed ticks, then the seat forfeits the round (provisional) | D-03 |
> | Match forfeit | **2** forfeited rounds forfeit the match (provisional) | D-03 |
>
> All five travel in `match_start.game_config` (§4.1). A bot MUST read them from there and never hard-code
> them.

---

## Overview

This document defines the **Big Bad Bots Game State Protocol**: the content that travels inside the game-agnostic Transport Protocol (Layer 1) at a `bigbadbots` table. It is a sibling of [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md), not a revision of it: NLHE payloads are byte-unchanged by everything below.

A Big Bad Bots match is one bot against one bot, in two stages:

1. **Gear-up.** Each bot buys a fighter body from a private offer, bids in a sealed-bid auction for body-mod parts, and fits the parts it won into its body's slots.
2. **Fight.** The two fighters fight a best-of-three (extended by drawn rounds) on a one-dimensional arena. Time advances in **ticks**, and on every tick both bots choose an action.

Big Bad Bots breaks more NLHE assumptions than any earlier dialect:

- **Both seats act at the same time.** Every decision is simultaneous: the body choice, every bid, the fitting, and every fight tick. There is no turn order and no `action_on`.
- **The decision rate is high.** A fight asks each seat for an action on every tick, several times a second.
- **There are no cards and no chips.** Credits are spent in gear-up; nothing is wagered.
- **The opponent's move is hidden while it winds up.** A bot sees what the opponent is doing only once it can no longer be answered on sight (§5).

The platform `game_type` is `bigbadbots`, and so are the Layer 2 `variant` and `state_shape` (the id was `fighter` in drafts before 2026-10-01; it was renamed before any match was played, chipzen-ai/Chipzen#5291). **The rule set is not the variant.** The rules a match is played under are named by `game_config.rule_set_id` (for example `bigbadbots-1.0`), which versions independently of this document: see [`docs/RULESETS.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md) (chipzen-ai/chipzen-sdk#138). A balance change is a new rule set, never a new dialect.

**Status.** This is a draft (chipzen-ai/chipzen-sdk#137). Every `turn_request` and `turn_action` example below is a real message and validates against the Big Bad Bots wire schemas the server checks messages against. The shapes in §4.5 to §4.8 (`turn_result`, `phase_change`, `round_start`, `round_result`) are not yet pinned by those schemas and are marked as such. Section 7 lists the places where Big Bad Bots meets a Layer 1 behavioural rule that was written for turn-based games; each needs review before a runner ships.

---

## 1. What this document inherits

[`LAYER2-COMMON.md`](LAYER2-COMMON.md) states the part of every Layer 2 dialect that is not game-specific, and it is **normative here**: *its* §1 (Layer 1 is untouched), *its* §2 (how a client learns which game it is at), *its* §3 (the five backward-compatibility rules) and *its* §4 (the versioning policy). Read it once; this document does not restate it.

What follows in this section and in §2 is only Big Bad Bots' **deltas** against that baseline.

### 1.1 The `bigbadbots` `game` descriptor

The server `hello` at a `bigbadbots` table carries this additive `game` descriptor (mechanism: [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §2):

```json
{
  "game_type": "bigbadbots",
  "variant": "bigbadbots",
  "actions": [
    "choose_body",
    "bid",
    "fit",
    "idle",
    "move_in",
    "move_out",
    "block",
    "punch_high",
    "uppercut",
    "kick_high",
    "kick_low",
    "throw",
    "special"
  ],
  "phases": ["body_shop", "auction", "fitting", "fight"],
  "state_shape": "bigbadbots"
}
```

`actions` is every `turn_action.action` string the dialect uses: the three gear-up actions and the ten fight actions. It does **not** include `fold`, `check`, `call` or `raise`. `variant` and `state_shape` are `bigbadbots` under every rule set.

Declaring `"bigbadbots"` in `supported_games` is an assertion that the client implements everything in this document.

---

## 2. Backward-compatibility deltas

The five rules and the reasoning behind them are in [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §3, and they bind every future revision of this document. Big Bad Bots' values under each:

| Rule | Big Bad Bots delta |
|---|---|
| **1** — valid cards only, never a placeholder | Big Bad Bots has no cards. **`board` and `your_hole_cards` are `[]` in every `turn_request.state`**, never omitted. |
| **2** — the six numeric fields stay present and numeric | Big Bad Bots has no chips. **`pot`, `to_call`, `min_raise`, `max_raise` and `your_stack` are always `0`, and `opponent_stacks` is always `[0]`** (heads-up: one opponent, pinned to 0). Credits live in `me.credits` / `opp.credits`, never in the chip fields. |
| **3** — `phase` stays a free string | Big Bad Bots uses four phase strings, none of them NLHE's: `body_shop`, `auction`, `fitting`, `fight` (§3.1). |
| **4** — new action parameters nest under `params` | Big Bad Bots' new parameters are `body`, `amount` and `fit` (§4.4). The fight actions carry no parameters. **Big Bad Bots adds no top-level field.** |
| **5** — new keys only | Big Bad Bots' new keys are `offer`, `lot`, `owned`, `slots`, `me`, `opp`, `lot_index`, `lots_total`, `history`, `tick`, `round`, `rounds_won`, `distance` and `facing` (§4.3). |

A deployed SDK that does not implement this document parses a `bigbadbots` `turn_request` without throwing (the Rule 1 and Rule 2 fields are all present and well-typed) but sees none of the Big Bad Bots keys, so it cannot play. Big Bad Bots support in the SDKs is tracked separately (chipzen-ai/chipzen-sdk#139).

---

## 3. The simultaneous model on Layer 1 v1.0

Simultaneous play needs **no Layer 1 change** and no version bump ([`LAYER2-COMMON.md`](LAYER2-COMMON.md) §1). Everything below uses Layer 1 messages exactly as [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md) specifies them.

### 3.1 Steps and calls

A match is a sequence of **steps**. Every step asks **both** seats for one answer, and resolves only when both answers are in (or their time has run out). There are four kinds of step, each with its own call name, phase and `valid_actions`:

| Step | Call | `state.phase` | `valid_actions` | How many |
|---|---|---|---|---|
| Body shop | `choose_body` | `body_shop` | `["choose_body"]` | 1 |
| One auction lot | `bid` | `auction` | `["bid"]` | one per lot (`rule_set.gearup.auction_lots`) |
| Fitting | `fit` | `fitting` | `["fit"]` | 1 |
| One fight tick | `act` | `fight` | the ten fight actions | one per tick, every round |

The call name is also the `action` string of the gear-up answers; in the fight the `action` string is the fight action itself.

### 3.2 One step is two `turn_request`s at the same moment

- For each step the server builds **both** seats' requests before either is answered, and sends them **at the same moment**, each on its own seat's socket. Each carries its own `request_id`, and `seat` is the receiving seat: at a simultaneous step, both seats are the seat whose turn it is.
- **Neither request depends on the other seat's answer or on its timing.** A seat never sees the other seat's answer for a step before it submits its own; the answers are applied together when the step resolves.
- There is no turn order. A client MUST NOT infer anything from the order in which it receives messages relative to its opponent: the opponent's thinking time never reaches its payloads.

### 3.3 Pacing

Fight ticks are paced in real time at `rule_set.rounds.ticks_per_second`. Each tick's requests go out on that tick's boundary. A tick resolves when both answers are in or both seats' time has run out, so the pace is an upper bound: a seat drawing on its time bank (§3.4) slows the fight for both seats while it does. Gear-up steps are not paced; the next gear-up step goes out as soon as the previous one resolves.

### 3.4 The per-tick deadline and the time bank (provisional)

Every fight `turn_request` carries its deadline in the existing Layer 1 field **`turn_request.timeout_ms`**. No new field is needed.

- **Deadline: 100 ms per tick (provisional, D-01).** An answer within the deadline costs nothing.
- **Time bank: 3 s per seat per round, reset at the start of every round (provisional, D-01).** An answer later than the deadline is paid for from the bank, for the part beyond the deadline, while the bank lasts.
- **`timeout_ms` = the deadline plus what is left of this seat's bank.** That is the whole time the server will wait for this answer (Layer 1 §8.5: "time remaining to submit an action"). A bot that wants to stay inside the deadline reads `game_config.rule_set.deadline.deadline_ms`; `timeout_ms - deadline_ms` is its bank.
- **Measurement.** The server measures from sending the request to receiving the answer (Layer 1 §10.2), and rounds up to whole milliseconds: 100.001 ms counts as 101 ms, which is late for a 100 ms deadline.
- **Gear-up calls have no per-tick deadline.** Their `timeout_ms` is the match's `match_start.turn_timeout_ms` (Layer 1 §10.1). The gear-up examples below show 1000 ms, which is illustrative (provisional).

### 3.5 Missed ticks and the default action (provisional)

A seat **misses** a tick when no acceptable answer arrives within `timeout_ms`, or when it is disconnected (§3.9).

- **The late answer is dropped.** It is never applied to a later tick. A `turn_action` carrying the `request_id` of a tick that has already resolved is dropped silently, as any unsolicited action is (Layer 1 §12.2).
- **The bank is spent**, except on a disconnect: no time was waited.
- **The tick plays the default action (provisional, D-01):** the seat's previous tick's action if that was `block`, `move_in` or `move_out`, otherwise `idle`. So a seat that was holding a block keeps blocking, and a seat that was walking keeps walking, rather than dropping its guard for one tick of network jitter.
- The server sends `action_timeout` with `auto_action` set to the action it applied, and the seat's `turn_result` for the tick carries `is_timeout: true`.
- A miss is not a rules violation. It does count toward the disconnect forfeit (§3.9).

Gear-up calls default too, when no acceptable answer arrives in time: the **first body on the offer**, a **bid of 0**, and an **empty fitting**.

This replaces Layer 1 §8.12's "`check` if legal, otherwise `fold`", which names two actions Big Bad Bots does not have. The Pineapple OFC dialect set the precedent ([`OFC-GAME-STATE-PROTOCOL.md`](OFC-GAME-STATE-PROTOCOL.md) §5.9).

### 3.6 One answer per request

A request is answered once. **The first acceptable `turn_action` carrying its `request_id` is final.** A second answer, or an answer for a stale or unknown `request_id`, is dropped (Layer 1 §12.2). In the fight this means one answer per seat per tick.

### 3.7 Rejected answers and the retry

- An answer whose `action` is not in `valid_actions`, or whose `params` do not have the call's shape (§4.4), is **rejected**: the server sends `action_rejected` with `remaining_ms` and `valid_actions`, exactly as Layer 1 §8.11 specifies.
- **A retry is legal, but it must land inside the same deadline.** The seat stays in `awaiting_action`; it may resubmit with the **same** `request_id` before `remaining_ms` runs out. A rejection does not extend the deadline or refill the bank.
- If no acceptable answer arrives in time, the call's default applies (§3.5) and the rejected answer is recorded as a rules violation. It does not count as a miss, because the seat did answer.
- Every rejection counts toward Layer 1's invalid-action limit (§13.1). See §7.

**A well-shaped answer is never rejected for its content.** Big Bad Bots corrects instead:

| Call | Well-shaped but not legal | What is applied |
|---|---|---|
| `choose_body` | a body that is not on this seat's offer | the cheapest body on the offer |
| `bid` | a bid outside `0..me.credits` | the bid clamped into range |
| `fit` | an entry naming a part the seat does not own, a part of the wrong slot type, or a slot that does not exist | that entry is dropped; the rest are fitted |
| `act` | an action the fighter cannot perform now (still busy with a move, not enough stamina, special already used) | `idle` for an action it cannot afford; nothing new while it is busy (§6.2) |

Each correction is logged as a violation against the seat in the match record.

### 3.8 `turn_result`: once per step, to each seat

When a step resolves, the server sends **one `turn_result` to each seat**. Its `seat` is the receiving seat, `is_timeout` says whether that seat's own action was a default (§3.5), and `details` carries that seat's action as applied **and the opponent's action as that seat is allowed to see it** (§4.5). So each seat learns both actions of the step, masked exactly as the next `turn_request.state` masks them.

The next `turn_request.state` is authoritative. `turn_result` is a convenience for logging and statistics, and a bot can ignore it.

### 3.9 Disconnects and forfeits (provisional)

- **The tick clock does not stop for a disconnected seat.** Its ticks are misses (§3.5): each plays the default action, and none spends its bank.
- **Disconnect grace: 5 s (provisional, D-03).** A seat that misses **every tick for 5 s** of match time forfeits the round, and the opponent takes it. The grace is counted in ticks, `rule_set.forfeit.grace_seconds × rule_set.rounds.ticks_per_second` (50 ticks at 10 per second), so the same match replays the same way. Because ticks are paced in real time, it is also 5 s of wall-clock time.
- **Any answer resets the count**, including one that is rejected or corrected. The check runs after each tick and only while both fighters are standing, so a KO on the same tick takes precedence.
- **Match forfeit: 2 forfeited rounds (provisional, D-03).** A seat that has forfeited that many rounds forfeits the match, and the opponent wins it however many rounds either had won.
- If both seats run out of grace on the same tick, the round has no winner and both are charged a forfeit. If both forfeit the match at once, the match is a **no-contest**: it has no winner and no coin flip. How `match_end` encodes a no-contest is open (§4.8).
- **Reconnecting** follows Layer 1 §11 unchanged: the seat reconnects, receives `reconnected`, and if a tick is in flight, `reconnected.pending_request` is that tick's `turn_request` with the time remaining. Play resumes on the next tick. Gear-up calls have no grace rule; a disconnected seat's gear-up calls take their defaults.

### 3.10 `session_control` pause and resume

- A pause is **match-wide**: both seats receive `session_control` with `action: "pause"`.
- **A pause takes effect between steps, never inside one.** The step in flight resolves first (both answers, or both deadlines), then the server pauses and sends no `turn_request` until it resumes (Layer 1 §6). An answer is therefore never lost to a pause.
- While paused, no tick advances: no bank is spent, and paused time never counts toward the disconnect grace.
- On `resume` the next step's requests go out, on a fresh tick boundary in the fight.
- `terminate` ends the match; `intervention` is informational. Both are as Layer 1 §8.13 specifies.

---

## 4. Payload schemas

Every integer in a Big Bad Bots payload is a JSON integer in the signed 64-bit range, and every float is finite. A number with a fractional part, including `12.0`, is not an integer: a bid of `12.0` is malformed. Size caps: a bot message is at most **4096 bytes** (Layer 1 §3.5; a longer one closes the connection with `4008`). A Big Bad Bots server message is at most **8192 bytes**, measured as compact UTF-8 JSON, so a bot can read with a fixed buffer.

### 4.1 Game config (`match_start.game_config`)

Sent once, at the start of the match. It names the rule set and carries its **complete parameter table**, so a bot never needs any other source for the numbers it plays by.

```json
{
  "variant": "bigbadbots",
  "rule_set_id": "bigbadbots-1.0",
  "num_players": 2,
  "rule_set": {
    "gearup": {"budget": 1000, "body_offer_size": 4, "auction_lots": 6},
    "arena": {"width": 12.0, "start_x": [4.0, 8.0], "start_jitter": 1.0, "min_separation": 1.15},
    "rounds": {"ticks_per_second": 10, "round_ticks": 600, "rounds_to_win": 2, "max_rounds": 5},
    "forfeit": {"grace_seconds": 5, "rounds_for_match": 2},
    "deadline": {
      "deadline_ms": 100,
      "time_bank_ms": 3000,
      "default_action_policy": "repeat_block_or_move",
      "late_answer": "drop",
      "miss_counts_toward_forfeit": true
    }
  }
}
```

The example is **abridged**: it shows five of the rule set's sections. A real `rule_set` carries every section listed in `docs/RULESETS.md`, including the bodies, the parts, the moves and the specials. `forfeit` and `deadline` hold the provisional values of the table at the top of this document.

| Field | Type | Required | Description |
|---|---|---|---|
| `variant` | string | Yes | Always `"bigbadbots"`. Selects this document. |
| `rule_set_id` | string | Yes | The rule set this match is pinned to, e.g. `"bigbadbots-1.0"`. Fixed when the match is created and never changes during it. See `docs/RULESETS.md`. |
| `num_players` | integer | Yes | Always `2`. |
| `rule_set` | object | Yes | The rule set's full parameter table, by section. **Read the numbers from here**; they differ between rule sets. |

`match_start.turn_timeout_ms` (Layer 1) is the timeout of each gear-up call (§3.4).

### 4.2 Round start (`round_start.state`)

**Not yet pinned by the wire schemas.** A Layer 1 round is one **fight round**. Gear-up happens between `match_start` and the first `round_start`, and is not a round.

```json
{
  "hand_number": 1,
  "round": 1,
  "phase": "fight",
  "rounds_won": [0, 0],
  "board": [],
  "your_hole_cards": []
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hand_number` | integer | Yes | The round number, carried under the NLHE name so an existing parser reads a sensible value. |
| `round` | integer | Yes | **New key.** The fight round, 1-based. Equal to `hand_number`. |
| `phase` | string | Yes | Always `"fight"`. |
| `rounds_won` | array of integer | Yes | **New key.** `[mine, opponent's]` before this round. |
| `board` | array | Yes | Always `[]`. |
| `your_hole_cards` | array | Yes | Always `[]`. |

### 4.3 Turn request state (`turn_request.state`)

The core decision payload. Every `turn_request.state` carries the phase and the Rule 1 and Rule 2 fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `phase` | string | Yes | `body_shop`, `auction`, `fitting` or `fight`. |
| `board` | array | Yes | Always `[]` (Rule 1). |
| `your_hole_cards` | array | Yes | Always `[]` (Rule 1). |
| `pot`, `to_call`, `min_raise`, `max_raise`, `your_stack` | integer | Yes | Always `0` (Rule 2). |
| `opponent_stacks` | array of integer | Yes | Always `[0]` (Rule 2). |

The rest of the payload depends on the call. The objects it uses (Body, Part, Side, SoldLot, Fighter) are defined in §4.3.5.

#### 4.3.1 `choose_body` (phase `body_shop`)

```json
{
  "type": "turn_request",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "seq": 4,
  "server_ts": "2026-09-29T12:00:00.000Z",
  "seat": 1,
  "request_id": "req_00000",
  "timeout_ms": 1000,
  "valid_actions": ["choose_body"],
  "state": {
    "phase": "body_shop",
    "board": [],
    "your_hole_cards": [],
    "pot": 0,
    "to_call": 0,
    "min_raise": 0,
    "max_raise": 0,
    "your_stack": 0,
    "opponent_stacks": [0],
    "offer": [
      {
        "id": "warden",
        "cost": 450,
        "stats": {"health": 45, "power": 35, "speed": 35, "reach": 35, "guard": 45, "stamina": 45},
        "slots": ["arms", "core", "head"],
        "special": "counter"
      },
      {
        "id": "golem",
        "cost": 550,
        "stats": {"health": 80, "power": 50, "speed": 22, "reach": 35, "guard": 52, "stamina": 42},
        "slots": ["core"],
        "special": "stone_skin"
      },
      {
        "id": "brawler",
        "cost": 410,
        "stats": {"health": 42, "power": 44, "speed": 40, "reach": 28, "guard": 30, "stamina": 52},
        "slots": ["legs", "arms", "core", "head"],
        "special": "frenzy"
      },
      {
        "id": "scrapper",
        "cost": 370,
        "stats": {"health": 35, "power": 35, "speed": 35, "reach": 35, "guard": 35, "stamina": 45},
        "slots": ["legs", "legs", "arms", "arms", "core", "head"],
        "special": "overdrive"
      }
    ],
    "me": {"credits": 1000, "offer": ["warden", "golem", "brawler", "scrapper"], "body": null, "owned": [], "fitted": []},
    "opp": {"credits": 1000, "offer": [], "body": null, "owned": [], "fitted": []},
    "lot_index": 0,
    "lots_total": 6,
    "history": []
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `offer` | array of Body | Yes | **New key.** This seat's own offer, in full. The opponent's offer is never shown. |
| `me` | Side | Yes | **New key.** This seat's side. `body` is `null` and `owned` is empty. |
| `opp` | Side (masked) | Yes | **New key.** The opponent's side. `offer` is always `[]`; `body` is `null` until the body shop resolves. |
| `lot_index` | integer | Yes | **New key.** `0`: no lot has been sold yet. |
| `lots_total` | integer | Yes | **New key.** How many lots the auction will sell. |
| `history` | array of SoldLot | Yes | **New key.** Empty. |

#### 4.3.2 `bid` (phase `auction`)

```json
{
  "type": "turn_request",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "seq": 5,
  "server_ts": "2026-09-29T12:00:00.000Z",
  "seat": 1,
  "request_id": "req_00001",
  "timeout_ms": 1000,
  "valid_actions": ["bid"],
  "state": {
    "phase": "auction",
    "board": [],
    "your_hole_cards": [],
    "pot": 0,
    "to_call": 0,
    "min_raise": 0,
    "max_raise": 0,
    "your_stack": 0,
    "opponent_stacks": [0],
    "lot": {"id": "spring", "slot": "legs", "bonus": {"speed": 25, "stamina": -10}, "effects": {}},
    "me": {"credits": 450, "offer": ["warden", "golem", "brawler", "scrapper"], "body": "golem", "owned": [], "fitted": [null]},
    "opp": {"credits": 740, "offer": [], "body": "wisp", "owned": [], "fitted": [null, null, null]},
    "lot_index": 0,
    "lots_total": 6,
    "history": []
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `lot` | Part | Yes | **New key.** The part being sold now. Future lots are never shown. |
| `me`, `opp` | Side | Yes | Both bodies are now public. `opp.credits` and `opp.owned` are public; `opp.offer` stays `[]`. |
| `lot_index` | integer | Yes | 0-based index of the lot being sold. |
| `lots_total` | integer | Yes | How many lots the auction sells. |
| `history` | array of SoldLot | Yes | Every lot already sold, in order, with **both** bids. The opponent's bid on the lot being sold is never in any payload. |

#### 4.3.3 `fit` (phase `fitting`)

```json
{
  "type": "turn_request",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "seq": 11,
  "server_ts": "2026-09-29T12:00:00.000Z",
  "seat": 1,
  "request_id": "req_00007",
  "timeout_ms": 1000,
  "valid_actions": ["fit"],
  "state": {
    "phase": "fitting",
    "board": [],
    "your_hole_cards": [],
    "pot": 0,
    "to_call": 0,
    "min_raise": 0,
    "max_raise": 0,
    "your_stack": 0,
    "opponent_stacks": [0],
    "owned": [{"id": "capacitor", "slot": "core", "bonus": {"stamina": 20}, "effects": {}}],
    "slots": ["core"],
    "me": {"credits": 354, "offer": ["warden", "golem", "brawler", "scrapper"], "body": "golem", "owned": ["capacitor"], "fitted": [null]},
    "opp": {"credits": 512, "offer": [], "body": "wisp", "owned": ["spring", "tactical", "strider"], "fitted": [null, null, null]},
    "lot_index": 6,
    "lots_total": 6,
    "history": [
      {"part": "spring", "my_bid": 0, "opp_bid": 92, "winner": "opp"},
      {"part": "shield_arm", "my_bid": 0, "opp_bid": 0, "winner": null},
      {"part": "tactical", "my_bid": 0, "opp_bid": 30, "winner": "opp"},
      {"part": "strider", "my_bid": 0, "opp_bid": 106, "winner": "opp"},
      {"part": "hammer", "my_bid": 0, "opp_bid": 0, "winner": null},
      {"part": "capacitor", "my_bid": 96, "opp_bid": 36, "winner": "me"}
    ]
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `owned` | array of Part | Yes | **New key.** Every part this seat won, in full. A seat keeps every part it wins, including more than its slots hold and parts for slot types its body lacks. |
| `slots` | array of string | Yes | **New key.** This seat's body's slot types, by slot index. The answer names slots by this index. |
| `me`, `opp`, `lot_index`, `lots_total`, `history` | | Yes | As in `bid`. `lot_index` equals `lots_total`: the auction is over. |

#### 4.3.4 `act` (phase `fight`)

```json
{
  "type": "turn_request",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "seq": 33,
  "server_ts": "2026-09-29T12:00:00.000Z",
  "seat": 1,
  "request_id": "req_00029",
  "timeout_ms": 3100,
  "valid_actions": ["idle", "move_in", "move_out", "block", "punch_high", "uppercut", "kick_high", "kick_low", "throw", "special"],
  "state": {
    "phase": "fight",
    "board": [],
    "your_hole_cards": [],
    "pot": 0,
    "to_call": 0,
    "min_raise": 0,
    "max_raise": 0,
    "your_stack": 0,
    "opponent_stacks": [0],
    "tick": 21,
    "round": 1,
    "rounds_won": [0, 0],
    "me": {
      "body": "golem",
      "special": "stone_skin",
      "x": 7.541,
      "hp": 200.62,
      "max_hp": 226.0,
      "stamina": 91.84,
      "max_stamina": 109.6,
      "action": null,
      "phase": null,
      "frames_left": 0,
      "special_used": false,
      "last_attack": "kick_high",
      "repeats": 0,
      "strike_range": 1.615,
      "move_rate": 0.188,
      "attrs": {"health": 80, "power": 50, "speed": 22, "reach": 35, "guard": 52, "stamina": 62},
      "buffs": {}
    },
    "opp": {
      "body": "wisp",
      "special": "blink",
      "x": 5.743,
      "hp": 175.3,
      "max_hp": 182.8,
      "stamina": 78.69,
      "max_stamina": 101.6,
      "action": "attack",
      "phase": "startup",
      "frames_left": null,
      "special_used": false,
      "last_attack": "kick_high",
      "repeats": 0,
      "strike_range": 1.687,
      "move_rate": 0.5,
      "attrs": {"health": 44, "power": 38, "speed": 100, "reach": 43, "guard": 26, "stamina": 52},
      "buffs": {}
    },
    "distance": 1.798,
    "facing": -1
  }
}
```

Here `timeout_ms` is 3100: the 100 ms deadline plus an untouched 3000 ms bank (both provisional). The opponent is winding up a move, so it shows as `"attack"` with `frames_left: null` (§5).

| Field | Type | Required | Description |
|---|---|---|---|
| `tick` | integer | Yes | **New key.** The tick within the round, 0-based. |
| `round` | integer | Yes | **New key.** The fight round, 1-based. |
| `rounds_won` | array of integer | Yes | **New key.** `[mine, opponent's]`. |
| `me` | Fighter | Yes | **New key.** This seat's fighter, in full. |
| `opp` | Fighter (masked) | Yes | **New key.** The opponent's fighter as this seat may see it (§5). |
| `distance` | number | Yes | **New key.** The distance between the two fighters, in arena units. |
| `facing` | integer | Yes | **New key.** `1` if the opponent is to this seat's right, `-1` if to its left. |

`valid_actions` is always all ten fight actions. Whether one can be performed right now (stamina, a busy fighter, a used special) is decided when the tick resolves (§3.7), not by removing it from the list.

#### 4.3.5 Objects

**Body** — a body on offer. Every field is the seat's own copy.

| Field | Type | Description |
|---|---|---|
| `id` | string | The body's id. |
| `cost` | integer | Credits it costs. |
| `stats` | object | Attribute name to base value. |
| `slots` | array of string | The body's part slots, by slot type. A type can repeat. |
| `special` | string | The id of the body's special move. |

**Part** — a body-mod part.

| Field | Type | Description |
|---|---|---|
| `id` | string | The part's id. |
| `slot` | string | The slot type it fits. |
| `bonus` | object | Attribute name to the amount it adds (negative subtracts). |
| `effects` | object | Effect name to a number, for parts that change a rule rather than an attribute. Empty for most parts. |

**Side** — one seat's gear-up state, as `me` (in full) or `opp` (masked).

| Field | Type | `me` | `opp` |
|---|---|---|---|
| `credits` | integer | Credits left. | Credits left. **Public.** |
| `offer` | array of string | This seat's offer, by body id. | **Always `[]`.** |
| `body` | string or null | The chosen body; `null` in the body shop. | Same: public once the body shop resolves. |
| `owned` | array of string | Parts won, by id. | Parts won, by id. **Public.** |
| `fitted` | array of null | One `null` per slot of the chosen body (`[]` before one is chosen), throughout gear-up. | Same: **the opponent's fitting is never shown**. |

**SoldLot** — one entry of `history`, seat-relative.

| Field | Type | Description |
|---|---|---|
| `part` | string | The part that was sold. |
| `my_bid` | integer | This seat's bid, as applied (after clamping). |
| `opp_bid` | integer | The opponent's bid, as applied. Revealed once the lot resolves. |
| `winner` | string or null | `"me"`, `"opp"`, or `null` when both bid 0 and the lot went unsold. |

**Fighter** — one fighter in the fight, as `me` (in full) or `opp` (masked, §5).

| Field | Type | Description |
|---|---|---|
| `body` | string | Body id. |
| `special` | string | Special move id. |
| `x` | number | Position on the arena. |
| `hp`, `max_hp` | number | Hit points now, and the round's maximum. |
| `stamina`, `max_stamina` | number | Stamina now, and the round's maximum. |
| `action` | string or null | What the fighter is doing: a move or special id, `"hitstun"`, `"dazed"`, or `null` when free. For `opp` during a wind-up: `"attack"`. |
| `phase` | string or null | `"startup"`, `"active"`, `"recovery"`, `"hitstun"`, `"dazed"`, or `null` when free. |
| `frames_left` | integer or null | Ticks left in the current phase. For `opp` during a wind-up: `null`. |
| `special_used` | boolean | Whether the special has been used this round. |
| `last_attack` | string or null | The last attack started, for the repeated-move penalty. |
| `repeats` | integer | How many times in a row `last_attack` has been used. |
| `strike_range` | number | The fighter's strike range, from its Reach. |
| `move_rate` | number | Distance per tick when moving, from its Speed. |
| `attrs` | object | The six attributes after fitting and live buffs. |
| `buffs` | object | Active effect name to what is left of it: ticks for a timed effect, strikes for a per-strike one. |

### 4.4 Turn action params (`turn_action.params`)

| Call | `action` | `params` | Constraint |
|---|---|---|---|
| `choose_body` | `"choose_body"` | `{"body": <body id>}` | Required. One body from `offer`. |
| `bid` | `"bid"` | `{"amount": <integer>}` | Required. A JSON integer; clamped to `0..me.credits`. `0` passes. |
| `fit` | `"fit"` | `{"fit": {"<slot index>": "<part id>", ...}}` | Required. Slot indexes are **decimal strings**, because JSON object keys are strings. At most one part per slot, of the slot's type. `{}` fits nothing. |
| `act` | one of the ten fight actions | omitted, or `{}` | No parameters. |

`params` admits no other keys: an extra key makes the answer malformed, and it is rejected (§3.7).

**Example: choose a body.**

```json
{
  "type": "turn_action",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "request_id": "req_00000",
  "action": "choose_body",
  "params": {"body": "golem"}
}
```

**Example: bid.**

```json
{
  "type": "turn_action",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "request_id": "req_00001",
  "action": "bid",
  "params": {"amount": 0}
}
```

**Example: fit.**

```json
{
  "type": "turn_action",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "request_id": "req_00007",
  "action": "fit",
  "params": {"fit": {"0": "capacitor"}}
}
```

**Example: one fight tick.**

```json
{
  "type": "turn_action",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "request_id": "req_00029",
  "action": "kick_low"
}
```

### 4.5 Turn result details (`turn_result.details`)

**Not yet pinned by the wire schemas.** One `turn_result` per seat per step (§3.8). `details` always carries:

| Field | Type | Description |
|---|---|---|
| `phase` | string | The step's phase. |
| `action` | string | This seat's action as applied: its answer, or the default (§3.5). |
| `params` | object | This seat's params as applied, after any correction (§3.7). `{}` in the fight. |

and, by phase, what the step revealed about the opponent:

| Phase | Extra fields |
|---|---|
| `body_shop` | `opponent_body`: the opponent's chosen body. |
| `auction` | `lot_index`, and `sold`: the SoldLot entry for this lot, with both bids. |
| `fitting` | None. The opponent's fitting is never shown. |
| `fight` | `round`, `tick`, and `opponent_action`: the opponent's `action` exactly as the next `act` request will show it, so `"attack"` while it winds up. |

**Example: a fight tick** (illustrative values):

```json
{
  "type": "turn_result",
  "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
  "seq": 34,
  "server_ts": "2026-09-29T12:00:00.100Z",
  "seat": 1,
  "is_timeout": false,
  "details": {"phase": "fight", "action": "kick_low", "params": {}, "round": 1, "tick": 21, "opponent_action": "attack"}
}
```

**Example: an auction lot** (`details` only):

```json
{"phase": "auction", "action": "bid", "params": {"amount": 0}, "lot_index": 0, "sold": {"part": "spring", "my_bid": 0, "opp_bid": 92, "winner": "opp"}}
```

### 4.6 Phase change (`phase_change.state`)

**Not yet pinned by the wire schemas.** Sent to both seats when gear-up moves on: after the body shop resolves (to `auction`), after the last lot (to `fitting`), and after the fitting resolves (to `fight`, before the first `round_start`).

```json
{"phase": "auction", "board": []}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `phase` | string | Yes | The new phase. |
| `board` | array | Yes | Always `[]`. |

### 4.7 Round result (`round_result.result`)

**Not yet pinned by the wire schemas.** Sent to both seats when a fight round ends.

```json
{
  "hand_number": 1,
  "round": 1,
  "winner_seats": [1],
  "outcome": "ko",
  "forfeit": [],
  "rounds_won": [0, 1],
  "pot": 0,
  "payouts": [],
  "showdown": [],
  "action_history": []
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hand_number` | integer | Yes | The round number. |
| `round` | integer | Yes | **New key.** The round number. |
| `winner_seats` | array of integer | Yes | The seat that took the round, or `[]` for a drawn round. |
| `outcome` | string | Yes | **New key.** `"ko"`, `"time_out"` (the higher HP share wins), `"draw"`, or `"forfeit"`. |
| `forfeit` | array of integer | Yes | **New key.** Seats that forfeited this round (§3.9). Usually `[]`. |
| `rounds_won` | array of integer | Yes | **New key.** Round wins so far, **by seat index** (not seat-relative). |
| `pot` | integer | Yes | Always `0`. Carried for shape compatibility. |
| `payouts` | array | Yes | Always `[]`. |
| `showdown` | array | Yes | Always `[]`. Nothing is hidden at round end that the fight did not already show. |
| `action_history` | array | Yes | Always `[]`. The tick-by-tick record is the match replay, not a live payload. |

### 4.8 Match end (`match_end`)

Layer 1 §8.9, unchanged. `reason` is `complete` for a match decided on the floor and `forfeit` for a match decided by §3.9. `results[].score` is the seat's round wins. **Open:** how a no-contest (both seats forfeit the match on the same tick) is encoded is not yet specified; it will be settled with the runner (chipzen-ai/chipzen-sdk#140).

---

## 5. Masking: what a bot can and cannot see

The server masks every view **before** it is serialised, so no payload ever carries what a seat may not see. A seat's own side is always shown in full.

| Phase | Hidden from the bot | Public |
|---|---|---|
| `body_shop` | the opponent's offer (`opp.offer` is `[]`) | both seats' credits; both bodies are still `null` |
| `auction` | the opponent's offer; the opponent's bid on the lot being sold; the lots not yet revealed | the opponent's body, credits and parts won; both bids on every lot already sold (`history`) |
| `fitting` | the opponent's offer and its fitting (`opp.fitted` is one `null` per slot) | everything the auction showed |
| `fight` | while the opponent winds up a move (`opp.phase` is `"startup"`): the move's name and its frames left | the opponent's position, HP, stamina, attributes, strike range, move rate, special id and status; its move once it becomes active, and during recovery |

**Hidden intent in the fight.** While the opponent winds up a move, it shows only as `action: "attack"` in `phase: "startup"`, with `frames_left: null`. Its `stamina`, `special_used`, `last_attack`, `repeats` and `buffs` keep their values from **before** the move started, so none of them gives the move away. The move is revealed on the tick it becomes active. Without this mask every attack could be answered on sight, and the strike/throw/block triangle (§6.2) would stop being a read.

**An interrupted wind-up is revealed too.** If a hit staggers the opponent during its wind-up, the attempted move is never shown as its `action` (the opponent shows `"hitstun"`), but from that tick on `last_attack` and `repeats` name it, `stamina` shows its cost paid, and `special_used` is set if it was the special. The mask covers the wind-up only.

**Never in any payload, to anyone, during play:**

- the opponent's answer for the current step, and anything derived from it before the step resolves;
- the opponent's timing: when it answered, whether it drew on its bank, and whether it missed a tick. Both requests of a step leave at the same moment, and fight results are released on the tick boundary;
- the match seed, and so every draw it decides: body offers, lots not yet revealed, tie-breaks, start positions;
- the opponent's fitting, as a slot-by-slot mapping. Its effect is visible once the fight starts, through `opp.attrs`, `opp.max_hp`, `opp.max_stamina`, `opp.strike_range` and `opp.move_rate`.

After the match, the replay carries everything, both seats' answers included.

---

## 6. Semantic rules

Every number the rules use is in `game_config.rule_set` (§4.1), named by its section and key; this section describes behaviour and names the key.

### 6.1 Gear-up

- **Budget.** Each seat starts with `gearup.budget` credits. One budget pays for the body and the auction. Credits left when the auction ends are lost.
- **Body shop.** Each seat is offered `gearup.body_offer_size` bodies, drawn independently, so the two offers can differ. Both seats choose at once; both choices are revealed before the auction.
- **Auction.** `gearup.auction_lots` distinct parts are drawn for the match and sold **one at a time**. For each lot both seats submit one **sealed bid** at once. The highest bid wins and pays its own bid; a tie goes to a seeded coin flip; if both bid 0 the lot goes unsold. Both bids are revealed before the next lot. A seat keeps every part it wins.
- **Fitting.** After the last lot, both seats fit their parts at once: one part per slot, and the part's slot type must match the slot. Parts left over are unused. Attributes are capped to their range after all parts apply.

### 6.2 The fight

- **The arena** is one-dimensional (`arena`). Fighters start facing each other; both start positions get the same small seeded shift each round. Fighters cannot pass closer than `arena.min_separation`, and nobody leaves the arena.
- **Ticks.** Each tick, both seats submit one action and both resolve together. A round lasts at most `rounds.round_ticks` ticks.
- **Actions.** `idle`, `move_in`, `move_out`, `block`, five attacks (`punch_high`, `uppercut`, `kick_high`, `kick_low`, `throw`) and `special` (the body's special move, once per round). Each attack has a **startup**, **active** and **recovery** phase, a stamina cost and a range (`moves`, `specials`).
- **A busy fighter cannot start a new action.** While it is in startup, active, recovery, hitstun or dazed, its submitted action is ignored until the current one ends. An action it cannot afford, or a special already used, plays as `idle`.
- **The counter triangle.** A strike beats a throw, a throw beats a block, a block beats a strike. Blocked strikes still deal a little chip damage and drain stamina; at 0 stamina the block breaks.
- **Stamina.** Attacks cost stamina and none can be started below its cost; stamina regenerates while idle. A fighter drained to 0 by the opponent's pressure is briefly **dazed**, which both seats can see.
- **Round end.** A round ends on a KO, at the tick limit (the higher share of max HP wins), or by forfeit (§3.9). A double KO, an exact HP tie at the limit, or a double forfeit is a **drawn round**; nobody scores.
- **Match end.** The first seat to `rounds.rounds_to_win` round wins takes the match. Drawn rounds extend the match up to `rounds.max_rounds` rounds; after that the seat with more round wins takes it, and if they are level a seeded coin flip decides. Health, stamina, buffs, the special and the repeated-move counter reset every round; the fitted build does not.

### 6.3 Determinism and replays

Every random draw comes from one match seed, through a generator that belongs to the rule set's engine line, so a match replays exactly from its seed and the recorded answers. Answers are recorded in their JSON form, and the match plays what was recorded, so a match and its replay make the same decisions by construction. Nothing in a payload lets a bot predict a future draw.

---

## 7. Layer 1 behavioural notes

Big Bad Bots changes no Layer 1 message, field, sequencing rule or error code, and no wire shape. Four Layer 1 **behavioural** rules were written for turn-based play with one decision every few seconds. How Big Bad Bots meets each is stated here, and the three marked "review" need an explicit ruling before a Big Bad Bots runner ships. If any is judged a Layer 1 change, it gets its own ADR, a `supported_versions` bump across all three SDKs, and a rules decision ([`LAYER2-COMMON.md`](LAYER2-COMMON.md) §1). None is changed by this document.

| Layer 1 rule | How Big Bad Bots meets it | Status |
|---|---|---|
| §8.12: the auto-action is `check` if legal, otherwise `fold` | Big Bad Bots' default action (§3.5). Same shape of delta as OFC's legal auto-placement. | Delta, with precedent |
| §10.3 / §14.4: 100–500 ms of random jitter before each `turn_result` | A 100–500 ms delay cannot fit a 100 ms tick (provisional). The jitter exists so that result timing never reveals an opponent's thinking time. Big Bad Bots meets that purpose by releasing both requests of a step together and fight results on the fixed tick boundary, which carry no timing information. | Review |
| §11.2 / §12.4: the disconnected seat's turn timer pauses on its first disconnect | Pausing one seat's timer would stall the opponent's fight. Big Bad Bots keeps the tick clock running; the disconnected seat's ticks take the default action and spend none of its bank (§3.9). | Review |
| §13.1: at most 10 messages per second per participant, and 5 invalid actions per round | At 10 ticks per second, a bot that answers every tick already sends 10 messages a second, before any `pong` or retry, and would be rate-limited in normal play. A fight round is up to `rounds.round_ticks` decisions, against a limit of 5 invalid actions. The server-side limits for `bigbadbots` tables must be set for the tick rate. | **Review, blocks the runner** |

---

## 8. Versioning

This document describes **v1** of the Big Bad Bots Game State Protocol. The policy is [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §4: Layer 1 stays at `1.0`, additive changes do not bump this document, removals and retypings do. Big Bad Bots adds one thing to it:

- **Rule sets version separately from this document.** A new rule set (`bigbadbots-1.1`, `bigbadbots-2.0`) changes `game_config.rule_set_id` and the numbers in `game_config.rule_set`, not `variant` and not this document. A rule set whose new mechanics need a new key in a payload is an additive change to this document like any other; one that needs a removal or a retyping is a major version of it. See `docs/RULESETS.md`.

---

## 9. Related documents

- [`LAYER2-COMMON.md`](LAYER2-COMMON.md): the Layer 2 baseline this document inherits (§1, §2).
- [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md): Layer 1. Unchanged by this document; §7 lists the behavioural rules under review.
- [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md): Layer 2 for NLHE, the baseline the compatibility rules are written against.
- [`OFC-GAME-STATE-PROTOCOL.md`](OFC-GAME-STATE-PROTOCOL.md): Layer 2 for Pineapple OFC, the precedent for a dialect-specific default action.
- [`docs/RULESETS.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md): rule-set ids, their lifecycle, and how a match pins one (chipzen-ai/chipzen-sdk#138).

This document and its mirror are drift-guarded: a normalized digest is committed beside each copy and pinned in both repositories, so a one-sided edit turns that side red.
