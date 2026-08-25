# 2-7 Triple Draw Game State Protocol (Layer 2)

**Date:** 2026-08-24
**Status:** Draft — the game is registered but **dark**
**Version:** 1.0 (Layer 1 protocol version is **unchanged at `1.0`**)

---

## Overview

This document defines the **2-7 Triple Draw (27TD) Game State Protocol** — the 27TD-specific content that travels inside the game-agnostic Transport Protocol (Layer 1). It is a sibling of [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md), not a revision of it: NLHE payloads are byte-unchanged by everything below.

27TD is **fixed-limit lowball**. Five cards per player, no community cards, four betting rounds interleaved with three draw rounds, and the lowest hand wins (deuce-to-seven — straights and flushes count against the hand, aces are always high).

The Layer 2 dialect is announced as `game_config.variant = "27tripledraw"`; the platform `game_type` is `draw27`.

**Status.** The 27TD plugin is registered with `enabled = true` (`chipzen-ai/Chipzen#4490`). No dispatch path creates a 27TD match today: `CHIPZEN_VARIANT_DISPATCH_GAMES` (#4255) is `"none"` in every environment, every scheduled surface carries `game_type="poker"` (#4246), and the declared-capability gate (#4256) refuses a poker-only house bot at a lowball table — so `enabled = true` on its own creates no 27TD match anywhere. This document describes the wire shape a 27TD match will have when a gated slice creates one, and the shape the engine tests already exercise end to end. It is an internal contract; nothing here is a public commitment.

---

## 1. What this document inherits

[`LAYER2-COMMON.md`](LAYER2-COMMON.md) states the part of every Layer 2 dialect that is not variant-specific, and it is **normative here**: *its* §1 (Layer 1 is untouched), *its* §2 (how a client learns which game it is at), *its* §3 (the five backward-compatibility rules) and *its* §4 (the versioning policy). Read it once; this document does not restate it.

What follows in this section and in §2 is only 27TD's **deltas** against that baseline.

### 1.1 The 27TD `game` descriptor

The server `hello` at a 27TD table carries this additive `game` descriptor (mechanism: [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §2):

```json
{
  "game_type": "draw27",
  "variant": "27tripledraw",
  "actions": ["call", "check", "draw", "fold", "raise"],
  "phases": ["predraw", "draw1", "bet1", "draw2", "bet2", "draw3", "bet3", "showdown", "complete"],
  "state_shape": "27tripledraw"
}
```

Declaring `"draw27"` in `supported_games` is an assertion that the client implements everything in this document.

---

## 2. Backward-compatibility deltas

The five rules and the reasoning behind them are in [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §3, and they bind every future revision of this document. 27TD's values under each:

| Rule | 27TD delta |
|---|---|
| **1** — valid cards only, never a placeholder | 27TD has no community cards, so **`board` is permanently `[]`** — an empty array, never omitted, never a placeholder. `your_hole_cards` is 5 cards, or fewer between declaring a draw and receiving the replacements (§5.4). |
| **2** — the six numeric fields stay present and numeric | In a 27TD **draw** phase `to_call`, `min_raise` and `max_raise` are `0`. `min_raise` / `max_raise` are also `0` whenever `raise` is unavailable for a rules reason — they are `0` in exactly the spots `valid_actions` omits `raise`, see §5.2. |
| **3** — `phase` stays a free string | 27TD uses nine phase strings, none of them NLHE's. They are listed in §4.2. |
| **4** — new action parameters nest under `params` | 27TD's one new parameter is `discard` (§3.5). **27TD adds no top-level field.** |
| **5** — new keys only | 27TD's new keys are `draws_remaining`, `is_draw_phase`, `draw_number`, `max_discard`, `your_draw_counts`, `opponent_draw_counts` and `draw_counts`. A parser that ignores them degrades to "a hand where nothing is ever raiseable", which is wrong but survivable. |

---

## 3. Payload Schemas

### 3.1 Game Config (`match_start.game_config`)

Sent once at the start of a match. It is rebuilt per hand internally as the limit ladder escalates, so a client MUST read the sizes it is given rather than deriving them.

```json
{
  "variant": "27tripledraw",
  "starting_stack": 10000,
  "betting_structure": "fixed_limit",
  "small_blind": 50,
  "big_blind": 100,
  "small_bet": 100,
  "big_bet": 200,
  "ante": 0,
  "bet_cap": 5,
  "num_betting_rounds": 4,
  "num_draws": 3,
  "cards_per_player": 5,
  "escalation_unit": "small_bet",
  "escalation_interval": 10,
  "num_players": 2
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `variant` | string | Yes | Always `"27tripledraw"` for this document. |
| `starting_stack` | integer | Yes | Chips each seat starts with. |
| `betting_structure` | string | Yes | Always `"fixed_limit"`. **Read this first.** A no-limit bot that sees `"fixed_limit"` and does not implement capped, fixed-size wagering should refuse the seat rather than min-raise its way through a capped round. |
| `small_blind` | integer | Yes | Small blind for the hand. **Not derivable from `big_blind`.** See §5.1. |
| `big_blind` | integer | Yes | Big blind for the hand. |
| `small_bet` | integer | Yes | The wager unit in betting rounds 0 and 1 (pre-draw, and after draw 1). |
| `big_bet` | integer | Yes | The wager unit in betting rounds 2 and 3 (after draws 2 and 3). Conventionally `2 * small_bet`, but a real field rather than a derivation — read it. |
| `ante` | integer | Yes | Per-seat ante posted each hand. `0` in v1. |
| `bet_cap` | integer | Yes | Total wagers allowed per betting round. `5`. See §5.2 for the counting convention — this number is meaningless without it. |
| `num_betting_rounds` | integer | Yes | `4`. |
| `num_draws` | integer | Yes | `3`. |
| `cards_per_player` | integer | Yes | `5`. |
| `escalation_unit` | string | Yes | The quantity that steps as the match progresses: `"small_bet"`, never a blind. |
| `escalation_interval` | integer | Yes | Hands per ladder rung. `10` for 27TD — the platform default of 20 does not apply. |
| `num_players` | integer | Yes | Seat count N. 27TD offers **2-5 seats** in v1 (see §5.6). |

There is **no `total_hands` key**, exactly as for NLHE: matches are elimination-only.

### 3.2 Round Start State (`round_start.state`)

Sent to each participant at the start of each hand.

```json
{
  "hand_number": 1,
  "dealer_seat": 0,
  "your_hole_cards": ["Ah", "Kd", "9s", "7c", "3h"],
  "pot": 150,
  "post_blind_stacks": [9950, 9900],
  "draws_remaining": 3,
  "stacks": [10000, 10000],
  "deck_commitment": ""
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hand_number` | integer | Yes | 1-indexed hand number within the match. |
| `dealer_seat` | integer | Yes | Button seat index, 0-indexed. |
| `your_hole_cards` | array of string | Yes | Exactly 5 valid card strings — the receiving seat's **opening five**, before any draw. |
| `pot` | integer | Yes | Chips in the pot after the forced postings. |
| `post_blind_stacks` | array of integer | Yes | Per-seat stacks after blinds and antes are posted. |
| `draws_remaining` | integer | Yes | **New key.** Draw rounds still to come. `3` at hand start. |
| `stacks` | array of integer | Yes | Per-seat stacks before blinds are posted. Merged by the transport layer, not by the rule set. |
| `deck_commitment` | string | Yes | `SHA-256(deck_seed \|\| deck_order)`, or `""` when RNG verification is not enabled. **See §5.6 — a 27TD hand may reshuffle, and the commitment does not cover a reshuffle.** |

The spectator variant of `round_start.state` carries no seat's cards and adds the level: `hand_number`, `dealer_seat`, `small_blind`, `big_blind`, `small_bet`, `big_bet`, `pot`, `post_blind_stacks`, `draws_remaining`, plus the transport layer's `stacks` and `deck_commitment`. The bet sizes are included because in a limit game the sizes *are* the level, and the blinds do not convey it (§5.1).

### 3.3 Turn Request State (`turn_request.state`)

The core decision payload. **One shape covers both a betting turn and a draw turn**; `is_draw_phase` says which one this is.

The example below is the **first** `draw1` turn of a heads-up hand with `dealer_seat: 0`. Draw order follows the betting round after the draw, so the big blind draws first (§5.4): this payload goes to **seat 1**, and the only other seat, seat 0, has not drawn yet.

```json
{
  "hand_number": 1,
  "phase": "draw1",
  "board": [],
  "your_hole_cards": ["Ah", "Kd", "9s", "7c", "3h"],
  "pot": 200,
  "your_stack": 9900,
  "opponent_stacks": [9900],
  "to_call": 0,
  "min_raise": 0,
  "max_raise": 0,
  "is_draw_phase": true,
  "draw_number": 1,
  "draws_remaining": 3,
  "max_discard": 5,
  "your_draw_counts": [],
  "opponent_draw_counts": {"0": []},
  "action_history": [
    {"seat": 0, "action": "post_small_blind", "amount": 50, "phase": "predraw", "is_timeout": false},
    {"seat": 1, "action": "post_big_blind", "amount": 100, "phase": "predraw", "is_timeout": false},
    {"seat": 0, "action": "call", "amount": 50, "phase": "predraw", "is_timeout": false},
    {"seat": 1, "action": "check", "amount": 0, "phase": "predraw", "is_timeout": false}
  ]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hand_number` | integer | Yes | Current hand number. |
| `phase` | string | Yes | One of the nine phase strings in §4.2. |
| `board` | array of string | Yes | **Always `[]`.** 27TD has no community cards. Carried for Rule 1. |
| `your_hole_cards` | array of string | Yes | The acting seat's **current** holding — 5 cards, or fewer between declaring a draw and receiving the replacements (§5.4). Never another seat's cards. |
| `pot` | integer | Yes | Total chips in the pot, including this round's bets. |
| `your_stack` | integer | Yes | Acting seat's remaining stack. |
| `opponent_stacks` | array of integer | Yes | Other seats' stacks in seat order, excluding the acting seat. Length N-1. |
| `to_call` | integer | Yes | Chips to add to call. `0` in a draw phase, and when checking is free. |
| `min_raise` | integer | Yes | The single legal raise-**to** total — the next full bet level, capped at the acting seat's all-in figure. **In fixed limit `min_raise == max_raise` always.** `0` **exactly when `raise` is absent from `valid_actions`**: a draw phase, a terminal phase, a settled round, the bet cap (§5.2), a reopening closed by a short all-in, no opponent left who can respond, or a stack too short to reach past the current bet. `min_raise > 0` and "`raise` is offered" are therefore the same test. |
| `max_raise` | integer | Yes | Identical to `min_raise`. See §5.3. |
| `is_draw_phase` | boolean | Yes | **New key.** `true` iff the acting seat owes a draw rather than a betting decision. The single dial a client branches on. |
| `draw_number` | integer | Yes | **New key.** `1`-`3` inside a draw round, `0` in every other phase. |
| `draws_remaining` | integer | Yes | **New key.** Draw rounds still to come, **counting one in progress**. `3` during `draw1`; `0` from `bet3` onward. |
| `max_discard` | integer | Yes | **New key.** The largest discard the acting seat may legally submit right now. `5` in a draw phase at every seat count v1 offers; `0` outside a draw phase. |
| `your_draw_counts` | array of integer | Yes | **New key.** The acting seat's own per-round draw counts so far, one entry per draw round it has completed (a stand pat contributes `0`). |
| `opponent_draw_counts` | object | Yes | **New key.** Map of **seat index as a decimal string** to that seat's per-round draw counts. Excludes the acting seat. Draw counts are **public information** — this is not a leak. Opponents' *cards* and *discards* never appear in any payload. |
| `action_history` | array of ActionEntry | Yes | Every action this hand, chronological, including the synthetic postings and every `draw`. |

**Not present:** 27TD's `turn_request.state` carries no `your_seat` and no `dealer_seat`. The button arrives in `round_start.state`; a client that needs it mid-hand must retain it. Layer 1's `turn_request.seat` names the acting seat.

`opponent_draw_counts` is keyed by **string** because JSON object keys are strings. `{"1": [2, 0]}` means seat 1 drew two cards in draw round 1 and stood pat in draw round 2.

### 3.4 Action Entry Schema

Identical in shape to NLHE's, with one 27TD-specific reading of `amount`.

```json
{"seat": 0, "action": "draw", "amount": 2, "phase": "draw1", "is_timeout": false}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `seat` | integer | Yes | 0-indexed seat that acted. |
| `action` | string | Yes | One of the action strings in §4.1, or a synthetic posting. |
| `amount` | integer | Yes | **Chips committed by this action** — the increment, not the seat's running total — for a wager or a posting. (Heads-up, a small blind of 50 that calls a big blind of 100 records `50`.) For `action: "draw"` it is the **number of cards drawn** — not a bitmask, not chips; the mask is a `turn_result` reading only (§3.6). `0` for `fold`, `check` and a stand pat. |
| `phase` | string | Yes | The phase the action occurred in. |
| `is_timeout` | boolean | Yes | `true` if the server substituted this action after a timeout. |

Draw counts are public, so publishing them in the shared trail is the same decision the game makes everywhere else.

### 3.5 Turn Action Params (`turn_action.params`)

| Action | `params` value | Constraint |
|---|---|---|
| `fold` | `{}` or omitted | None. |
| `check` | `{}` or omitted | None. |
| `call` | `{}` or omitted | None. |
| `raise` | `{"amount": <integer>}` | Fixed limit: `amount` is clamped to the single legal raise-to total. See §5.3. |
| `draw` | `{"discard": [<card or index>, ...]}` | At most `max_discard` entries. **An empty list, or an omitted `discard`, is a stand pat.** |

**Example — draw two:**

```json
{
  "action": "draw",
  "params": {"discard": ["Ah", "Kd"]}
}
```

**Example — stand pat:**

```json
{
  "action": "draw",
  "params": {"discard": []}
}
```

The discard list accepts either **card strings**, matched against the acting seat's own holding, or **0-based hand positions** into `your_hole_cards`. The keys `discard`, `discards` and `cards` are all accepted, most specific first; `discard` is canonical. Unknown cards, out-of-range positions and duplicates are **dropped**, and an over-long selection is **truncated** to `max_discard` — sloppy input costs a card, not the hand. This is the same forgiving-clamp policy NLHE applies to a raise amount.

### 3.6 Turn Result Details (`turn_result.details`)

Unchanged from NLHE in shape. `pot` and `stacks` are the post-action values, which a draw does not move. No card — discarded or received — ever appears in a `turn_result`.

**For a `draw`, `amount` is the discard MASK, not the count.** `turn_result.details.amount` carries the server-normalized form of the submission: a five-bit field over the acting seat's hand positions, bit *i* set when position *i* was replaced, `0` for a stand pat. Discarding positions 0 and 1 puts `3` on the wire; position 4 alone puts `16`. This is the one place the two 27TD readings of `amount` diverge — `action_history` (§3.4) carries the public **count**, and that is what a client should read to learn how many cards a seat took.

### 3.7 Phase Change State (`phase_change.state`)

Broadcast when a phase completes.

```json
{
  "phase": "bet1",
  "board": [],
  "draw_number": 0,
  "draws_remaining": 2,
  "draw_counts": {"0": [2], "1": [0]}
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `phase` | string | Yes | The new phase (§4.2). |
| `board` | array of string | Yes | **Always `[]`.** |
| `draw_number` | integer | Yes | `1`-`3` inside a draw round, `0` otherwise. |
| `draws_remaining` | integer | Yes | Draw rounds still to come, counting one in progress. |
| `draw_counts` | object | Yes | Map of seat index (decimal string) to that seat's per-round draw counts. **Every** seat, including the recipient's own. Public information. |

Unlike NLHE, a 27TD `phase_change` never deals a card into a shared `board`. What changes between phases is who owes what, and the draw counts everybody may see.

### 3.8 Round Result (`round_result.result`)

The round-result envelope is **game-agnostic and unchanged**: `hand_number`, `winner_seats`, `pot`, `payouts`, `showdown`, `action_history`, `stacks`, `deck_commitment`, `deck_reveal`. Only the `showdown` entries are 27TD-shaped.

### 3.9 Showdown Entry Schema

```json
{
  "seat": 0,
  "hole_cards": ["8s", "6h", "4d", "3c", "2h"],
  "hand_rank": "8-6-4-3-2",
  "best_hand": ["8s", "6h", "4d", "3c", "2h"]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `seat` | integer | Yes | Seat at showdown. |
| `hole_cards` | array of string | Yes | The seat's five cards **as held at showdown**, in engine order. |
| `hand_rank` | string | Yes | **Not an NLHE hand-rank enum value.** A 2-7 hand is named by its ranks, high to low, dash-joined: `"8-6-4-3-2"`. `""` for an incomplete hand. Suits never appear — they do not break ties. |
| `best_hand` | array of string | Yes | The same five cards sorted high to low, which is how a 2-7 hand is read. `[]` for an incomplete hand. |

**A client MUST NOT feed 27TD's `hand_rank` into an NLHE hand-rank lookup.** It is a display string, and the *lowest* hand wins. Entries appear only for seats still contesting the pot; a folded seat's cards are never revealed.

---

## 4. Phases and the action vocabulary

### 4.1 Action vocabulary

| Action | Available | Notes |
|---|---|---|
| `fold` | Betting phases, when `to_call > 0` | Same semantics as NLHE. |
| `check` | Betting phases, when `to_call == 0` | Same semantics as NLHE. |
| `call` | Betting phases, when `to_call > 0` | Same semantics as NLHE. |
| `raise` | Betting phases, when not capped and an opponent can still respond | Fixed size: `min_raise == max_raise`. |
| `draw` | Draw phases only | **New.** The only action a draw phase offers. |

Four of the five are NLHE's, deliberately: the WebSocket allowlist and every published SDK parser already accept them. Only `draw` is new. There is no `bet` action — an opening wager is a `raise` from `0`, exactly as in NLHE.

Layer 1's `turn_request.valid_actions` is a plain array of these strings. In a draw phase it is exactly `["draw"]`. The *bounds* live in the state payload (`max_discard`), not in `valid_actions`.

**Submitting a betting action in a draw phase, or `draw` in a betting phase, is an error.** Branch on `is_draw_phase`.

### 4.2 Phase strings

Nine phases, in order:

| # | `phase` | Kind | Meaning |
|---|---|---|---|
| 1 | `predraw` | Betting | Betting round 0, after the deal. The big blind is wager 1 (§5.2). Wager unit `small_bet`. |
| 2 | `draw1` | Draw | First draw. |
| 3 | `bet1` | Betting | Betting round 1. Wager unit `small_bet`. |
| 4 | `draw2` | Draw | Second draw. |
| 5 | `bet2` | Betting | Betting round 2. Wager unit `big_bet`. |
| 6 | `draw3` | Draw | Third draw. |
| 7 | `bet3` | Betting | Betting round 3. Wager unit `big_bet`. |
| 8 | `showdown` | Terminal | Hands compared. |
| 9 | `complete` | Terminal | Hand settled. |

A 27TD hand asks a seat for roughly twice as many decisions as an NLHE hand: four betting rounds and three draws.

The wager unit steps at `bet2`, **not** at `bet1`: rounds 0 and 1 are small-bet rounds, rounds 2 and 3 are big-bet rounds.

---

## 5. Semantic Rules

### 5.1 The blinds are not derivable from each other

**Normative.** `small_blind` is **not** `big_blind / 2` and MUST NOT be computed. Real 27TD structures run other ratios (a published WSOP event opened at 300/500 blinds with 500/1,000 limits, a ratio of 0.6). Read `small_blind` and `big_blind` from `game_config`.

Equally, `small_bet` is not derivable from `big_blind` in general, and `big_bet` is not derivable from `small_bet`. Read all four.

### 5.2 The bet cap: five total wagers per round

**Normative, and this is the counting convention the number means.**

`bet_cap` is expressed as **total wagers in a betting round** — not "bets", not "raises". Exhaustively:

- **Pre-draw, the big blind is wager 1.** The first voluntary raise is wager 2. Five wagers means the big blind plus four raises.
- **After each draw (rounds 1-3), the opening bet is wager 1.** Five wagers means the opening bet plus four raises.
- A short all-in large enough to be treated as a full bet under the half-bet rule counts as a wager. A short all-in too small to qualify does **not**.
- A `call`, a `check` and a `fold` are never wagers.

Why the convention has to be pinned: "4 bets", "a bet and four raises" and "4 raises" are three different numbers for the same sentence, and mixing them is the classic fixed-limit off-by-one. The published rule is *"one bet and four raises"*, which under this convention is **5**.

**The cap applies in every round, at every table size, heads-up included.** This is a deliberate, documented divergence from the tournament exemption that uncaps an event's final two players: a Chipzen heads-up match would otherwise be permanently uncapped. A client MUST NOT assume heads-up is uncapped.

**Once the cap is reached, `raise` disappears from `valid_actions`, and `min_raise` / `max_raise` go to `0` with it.** The action list and the numbers beside it are one answer, not two: `min_raise > 0` holds in exactly the spots `raise` is on offer, so a client may branch on either. **`valid_actions` remains the authority on which actions are legal** — it is the list a submission is checked against, and a size is not an offer — but it can no longer be contradicted by the state block. The same zeroing holds wherever else §5.3 removes `raise`.

> **Changed in `chipzen-ai/Chipzen#4487`.** Until that fix the payload kept reporting the next bet level at the cap, so a client branching on `min_raise > 0` submitted a raise the server rejected and burned its retry window into a substituted action. No dispatch path has ever created a 27TD match, so no client ever received the old shape; see §7.

### 5.3 Raise sizing is a point, not a range

**Normative.** In fixed limit there is exactly one legal raise size in any given spot, so `min_raise == max_raise` in every `turn_request.state`. Both are the **total bet size** (raise-to), consistent with NLHE.

A submitted `raise` amount is **clamped** to that single legal figure whatever the client sends, so a bot cannot mis-size a fixed-limit raise. The one case where the figure is not the full wager unit is a seat too short to post it: the offer collapses to that seat's all-in total.

`raise` is absent from `valid_actions` when the cap is reached, when no opponent can still respond, or when a short all-in has closed the action to a seat that already acted. In each of those cases `min_raise` / `max_raise` are `0` (§5.2): the size is read out of the same offer that produces the action entry, so there is no state in which one exists without the other.

### 5.4 The draw: dealing is deal-as-you-go

**Normative.** A seat's replacement cards are dealt **before the next seat draws**. Declare-then-deal batching — the live-dealer artefact where every seat declares and only then receives — is **not** used. This matches cash and online play.

Consequences a client must handle:

- Between declaring a draw and receiving the replacements, a seat's holding is **fewer than five cards** — the kept cards only. A seat never observes this on its own turn (it draws once per round), but it is observable in a hand record (§6).
- The seat drawing after you has already seen how many cards you took, and you have seen how many the seats before you took. That is `opponent_draw_counts`, and it is correct: draw counts are public.

**Draw order** is the order of the betting round that *follows* the draw: first active seat left of the button, clockwise. Heads-up the button is the small blind, so the big blind draws first — the same positional rule, not a special case.

**Who draws:** every seat still in the hand, **including all-in seats**. All-in seats are skipped for *betting* only.

**How many:** zero to five. **Five consecutive is legal.** `max_discard` states the bound before the seat acts.

**A draw is a turn.** Every seat that owes a draw gets its own `turn_request` with `is_draw_phase: true`.

### 5.5 Stand pat

**Normative.** `draw` with an empty discard list is a **stand pat** — the seat keeps all five cards. It is a real action, not a no-op: it appears in `action_history` with `amount: 0`, it contributes a `0` to that seat's draw counts, and it passes the turn.

All of these are stand pats and are treated identically:

```json
{"action": "draw", "params": {"discard": []}}
{"action": "draw", "params": {}}
{"action": "draw"}
{"action": "draw", "params": {"discard": null}}
```

Stand pat is also the **server's substituted action** for a seat that times out in a draw phase. NLHE's check-else-fold default names two actions a draw phase does not offer; 27TD substitutes the free, hand-preserving option instead.

### 5.6 The burn, the reshuffle, and the deck commitment

One card is burned per draw round, **always** — even when every seat stands pat.

A 27TD hand can outrun the 52-card stub. When it does, the stub is rebuilt from the previous rounds' discards, the muck and the burn pile — **never** the discards of the round in progress. Reshuffles are dealer procedure and are **public**: the hand record names the rounds in which one happened (§6).

**Integrity consequence, stated plainly.** `deck_commitment` commits to one 52-card order. A mid-hand reshuffle deals cards that order cannot explain, so `SHA-256(seed || join(deck_order)) == deck_commitment` verification of a hand that reshuffled does not close.

**Only heads-up is exhaust-proof, and v1 offers seat counts that are not.** Over a whole hand two seats need at most `2*5 + 3*(2*5 + 1) = 43` cards, which fits in 52, so `N = 2` never reaches the reshuffle. No other offered seat count has that property. At `N` seats the stub after the deal is `52 - 5N`, and one draw round can demand `5N + 1` — five replacements each plus the burn. Three-handed that is 16 against 37 and the stub is dry inside the third draw; five-handed it is 26 against 27, so the *first* draw round clears by exactly one card and the rounds after it do not.

**What a fairness verifier must therefore do.** A 27TD hand at `N >= 3` may legitimately fail deck-order verification, and that failure is not evidence of tampering. Read the hand record's `reshuffles` (§6) **before** concluding anything: `reshuffles.occurred == false` means the commitment covers the whole hand and a mismatch is real; `reshuffles.occurred == true` means the hand went outside what the commitment can explain and the check does not apply. A verifier that reads every mismatch as tampering will raise false alarms on ordinary multi-way hands.

**The 5-seat cap is a separate constraint**, and it is not a claim that the stub holds. Six-handed cannot pay a maximal final draw even *with* the reshuffle: the last drawer's five cards would have to come from outside the five other live hands (25), the round in progress' discards (30, less the burns swapped out of it) and the burn pile — 55 cards' worth of demand against a 52-card deck. The cap moves only when the fairness layer commits to a **seed** rather than a card order and the verifier can replay every reshuffle.

### 5.7 Showdown, ties and the odd chip

The **lowest** deuce-to-seven hand wins: straights and flushes count against the hand and aces are always high, so the best possible hand is `7-5-4-3-2` in mixed suits. Ties split. Suits never break a tie. The odd chip goes to the first seat left of the button.

### 5.8 What is never in a payload

- **Another seat's cards**, in any live payload, ever.
- **Any seat's discards**, including the recipient's own. A seat's discards are as private as its hand; only the *count* is public.
- **The burn cards.** Only how many were burned, and only in the hand record.

### 5.9 Timeouts

Unchanged from Layer 1. The substituted action is **stand pat** in a draw phase and **check-else-fold** in a betting phase. Substituted actions carry `is_timeout: true` in `action_history`, as in NLHE.

---

## 6. Hand-record payload

**Frozen, schema version 1.** A completed 27TD hand leaves a structured record behind. It rides in the game-neutral variant envelope under a `variant` key, discriminated by `game_type`. A poker reader, and a reader that predates the envelope, see nothing new.

```jsonc
{
  "variant": {
    "game_type": "draw27",
    "schema_version": 1,

    "seats": [
      {
        "seat": 0,
        "dealt": ["Ah", "Kh", "Qh", "Jh", "Th"],
        "draws": [
          {
            "seat": 0,
            "draw_round": 1,
            "stand_pat": false,
            "discard_indices": [0, 1],
            "discarded": ["Ah", "Kh"],
            "received": ["8s", "6c"],
            "holding_after": ["Qh", "Jh", "Th", "8s", "6c"]
          }
        ],
        "final_holding": ["Qh", "Jh", "Th", "8s", "6c"],
        "folded": true,
        "was_dealt_in": true
      }
    ],

    "draw_counts": [[2], [0]],
    "num_draws": 3,
    "reshuffles": { "occurred": false, "rounds": [] },
    "burns": 1
  }
}
```

| Field | Type | Description |
|---|---|---|
| `game_type` | string | `"draw27"`. **Discriminate on this; never sniff keys.** |
| `schema_version` | integer | `1`. A version you do not know means degrade, never guess. |
| `seats` | array of object | One entry per seat, each carrying an integer `seat`. Per-seat card data lives **here and nowhere else** — human-seat redaction filters on exactly this shape, so a payload hiding cards elsewhere would leak them. |
| `seats[].dealt` | array of string | The seat's opening five, frozen at the deal. |
| `seats[].draws` | array of object | One entry per draw round the seat acted in, in order. |
| `seats[].draws[].draw_round` | integer | `1`-`3`. |
| `seats[].draws[].stand_pat` | boolean | `true` iff `discarded` is empty. |
| `seats[].draws[].discard_indices` | array of integer | Positions in the holding **before** this draw. |
| `seats[].draws[].discarded` | array of string | The cards thrown. |
| `seats[].draws[].received` | array of string | Replacements, in the order dealt. `len(received) < len(discarded)` marks a draw still awaiting its cards; a completed hand never has one. |
| `seats[].draws[].holding_after` | array of string | The seat's cards once the replacements land. |
| `seats[].final_holding` | array of string | The cards the seat actually held when the hand ended **for it** — at showdown, or at the moment it folded. Equal to the last `draws` entry's `holding_after`, or to `dealt` for a seat that never drew. |
| `seats[].folded` | boolean | Whether the seat folded. |
| `seats[].was_dealt_in` | boolean | `false` for a seat sitting out with no chips. |
| `draw_counts` | array of array of integer | Per seat, per round — the **public** counts as the table saw them, including the zeroes a stand pat contributes. |
| `num_draws` | integer | `3`. Present so "drew twice then folded" is distinguishable from a format change. |
| `reshuffles` | object | `{"occurred": bool, "rounds": [int]}` — the draw rounds in which the stub was rebuilt. Recorded because a reshuffle is the moment the hand stops being explained by its deck commitment (§5.6); silence would read as "the commitment covers everything". |
| `burns` | integer | **A count, not the cards.** The reshuffle swaps current-round discards into the burn pile one-for-one, so the pile at hand end is not the cards that were burned; publishing it would be a confident lie. |

**Reconstruction.** `dealt` folded over `draws` in order reproduces every intermediate holding with no reference to live state: `holding_after == (holding_before - discarded) + received`.

**Why the record exists.** The runtime writes a hand's hole cards **once**, right after the deal. For NLHE that is the whole truth; for 27TD it is the pre-draw five. Without this payload every folded 27TD hand in the match history, the replay viewer and the export would show cards the player did not hold. The record is built off an append-only journal, never off live state, because the reshuffle drains live state: by showdown it has genuinely forgotten what a seat threw in draw 1.

The record is inside the per-hand audit chain: mutating a recorded discard or replacement changes that hand's hash, every subsequent hash, and the match checksum.

---

## 7. Versioning

This document describes **v1** of the 2-7 Triple Draw Game State Protocol. The policy is [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §4 — Layer 1 stays at `1.0`, additive changes do not bump this document, removals and retypings do. 27TD adds one thing to it:

- The hand-record payload (§6) versions separately, via its own `schema_version`, because it is persisted: a shape change on already-written rows is a migration, not an edit.

The §5.2 / §5.3 zeroing change (`chipzen-ai/Chipzen#4487`) is a semantic change to an existing field, which LAYER2-COMMON §4 would normally make a major bump. It is **not** bumped here: no 27TD match has ever been dispatched, so v1 has no client to break — the dialect is still unreleased and v1 is what it will first ship as. A change of this kind after 27TD goes live would bump.

---

## 8. Related documents

- [`LAYER2-COMMON.md`](LAYER2-COMMON.md) — the Layer 2 baseline this document inherits (§1, §2).
- [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md) — Layer 1. Unchanged by this document.
- [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) — Layer 2 for NLHE. Card notation (§1) and the action-entry shape are shared verbatim.
- [`OFC-GAME-STATE-PROTOCOL.md`](OFC-GAME-STATE-PROTOCOL.md) — Layer 2 for Pineapple OFC, the other variant landing under epic `chipzen-ai/Chipzen#4200`.

This document and its mirror are drift-guarded: a normalized digest is committed beside each copy and pinned in both repositories, so a one-sided edit turns that side red.
