# Pineapple OFC Game State Protocol (Layer 2)

**Date:** 2026-08-24
**Status:** Draft — the game is registered but **dark**
**Version:** 1.0 (Layer 1 protocol version is **unchanged at `1.0`**)

---

## Overview

This document defines the **Pineapple Open-Face Chinese (OFC) Game State Protocol** — the OFC-specific content that travels inside the game-agnostic Transport Protocol (Layer 1). It is a sibling of [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md), not a revision of it: NLHE payloads are byte-unchanged by everything below.

OFC is the variant that breaks the most NLHE assumptions at once:

- **There is no betting.** No pot, no bet, no raise, no fold. Seats place cards and settle points.
- **The board is public and the hand is private** — the inverse of poker.
- **Placement is irrevocable.** A card placed in a row stays there for the hand.
- **There is exactly one action**, `place`, and one decision shape covers every turn.

The Layer 2 dialect is announced as `game_config.variant = "pineapple"`; the platform `game_type` is `ofc`. Classic OFC, progressive OFC and 2-7 OFC are dialects of the same engine and are explicitly out of v1 scope.

**Status.** The OFC plugin is registered with `enabled = true` (`chipzen-ai/Chipzen#4200`). No dispatch path creates an OFC match on its own: every scheduled surface carries `game_type="poker"` (#4246), the declared-capability gate (#4256) refuses a poker-only house bot in front of a thirteen-card board, and the challenge picker only offers OFC once a bot with a *verified* OFC capability exists (#4609) — so `enabled = true` on its own creates no OFC match anywhere. This document describes the wire shape an OFC match will have when a gated slice enables one, and the shape the engine tests already exercise end to end. It is an internal contract; nothing here is a public commitment.

---

## 1. What this document inherits

[`LAYER2-COMMON.md`](LAYER2-COMMON.md) states the part of every Layer 2 dialect that is not variant-specific, and it is **normative here**: *its* §1 (Layer 1 is untouched), *its* §2 (how a client learns which game it is at), *its* §3 (the five backward-compatibility rules) and *its* §4 (the versioning policy). Read it once; this document does not restate it.

What follows in this section and in §2 is only OFC's **deltas** against that baseline.

### 1.1 The OFC `game` descriptor

The server `hello` at an OFC table carries this additive `game` descriptor (mechanism: [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §2):

```json
{
  "game_type": "ofc",
  "variant": "pineapple",
  "actions": ["place"],
  "phases": ["deal1", "street2", "street3", "street4", "street5", "complete"],
  "state_shape": "pineapple"
}
```

Note `actions: ["place"]` — the OFC vocabulary does **not** include `fold`, `check`, `call` or `raise`. A client whose vocabulary is a strict subset of the table's cannot play there.

Declaring `"ofc"` in `supported_games` is an assertion that the client implements everything in this document.

---

## 2. Backward-compatibility deltas

The five rules and the reasoning behind them are in [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §3, and they bind every future revision of this document. OFC's values under each:

| Rule | OFC delta |
|---|---|
| **1** — valid cards only, never a placeholder | OFC has no community cards, so **`board` is permanently `[]`**. `your_hole_cards` carries the seat's own **pending cards** — dealt and not yet placed — as real cards, or `[]` between streets. **This is why a redacted Fantasy Land row is `[]` and never a placeholder** (§5.6). |
| **2** — the six numeric fields stay present and numeric | OFC has no wagering. `to_call`, `min_raise` and `max_raise` are **always `0`**; `pot` is `0` for the whole hand (§5.8). |
| **3** — `phase` stays a free string | OFC uses six phase strings, none of them NLHE's. They are listed in §4.2. |
| **4** — new action parameters nest under `params` | OFC's new parameters are `placements` and `discard` (§3.5). **OFC adds no top-level field.** |
| **5** — new keys only | OFC's new keys are `your_rows`, `opponent_rows`, `cards_to_place`, `must_discard`, `royalties` and the rest of §3. |

---

## 3. Payload Schemas

### 3.1 Game Config (`match_start.game_config`)

Sent once at the start of a match. Rebuilt per hand internally as the point value escalates, so a client MUST read the value it is given rather than assuming the opening one.

```json
{
  "variant": "pineapple",
  "starting_stack": 10000,
  "scoring": "points",
  "point_value": 50,
  "bank_points": 200,
  "rows": {"top": 3, "middle": 5, "bottom": 5},
  "cards_per_seat": 17,
  "cards_placed": 13,
  "cards_per_street": [5, 3, 3, 3, 3],
  "fantasy_land": {
    "cards": 14,
    "progressive": false,
    "middle_stay": null,
    "entry_min_pair_rank": 12
  },
  "min_seats": 2,
  "max_seats": 3,
  "rated_max_seats": 2,
  "escalation_unit": "point_value",
  "escalation_interval": 20,
  "num_players": 2
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `variant` | string | Yes | Always `"pineapple"` for this document. |
| `starting_stack` | integer | Yes | Chips each seat starts with. |
| `scoring` | string | Yes | Always `"points"`. **Read this first.** A chips-per-wager bot has nothing to do at this table; there is no `betting_structure` key because there is no betting. |
| `point_value` | integer | Yes | Chips one point is worth this hand. `50` at the opening level. See §5.7. |
| `bank_points` | integer | Yes | The stack expressed in the unit the game settles in: `starting_stack / point_value`. `200` at the opening level. Derived, never a second source of truth. |
| `rows` | object | Yes | Row name to capacity: `{"top": 3, "middle": 5, "bottom": 5}`. The row names in this object are the row names the `place` action uses. |
| `cards_per_seat` | integer | Yes | `17` — cards a non-Fantasy-Land seat is dealt across the hand. Discards included: 17 = 5 + 4x3. |
| `cards_placed` | integer | Yes | `13` — the board size. |
| `cards_per_street` | array of integer | Yes | Cards **dealt** on each street, in phase order: `[5, 3, 3, 3, 3]`. Discards included. |
| `fantasy_land` | object | Yes | The Fantasy Land knobs. See §5.6 and the table below. |
| `min_seats` | integer | Yes | `2`. |
| `max_seats` | integer | Yes | `3` — what the cards allow (`3 x 17 = 51 <= 52`). The deck is never reshuffled. |
| `rated_max_seats` | integer | Yes | `2`. What the *variance* allows for a rated match: three-handed OFC puts a large fraction of matches on a single hand, because pairwise settlement stacks. Advisory. |
| `escalation_unit` | string | Yes | `"point_value"` — the chip value of a point is what steps. Never a blind; OFC has none. |
| `escalation_interval` | integer | Yes | Hands per rung. `20`. |
| `num_players` | integer | Yes | Seat count N. |

`fantasy_land` keys:

| Field | Type | Description |
|---|---|---|
| `cards` | integer | `14`. Flat — a Fantasy Land seat is dealt 14 at once and sets 13. |
| `progressive` | boolean | `false`. Progressive Fantasy Land (14/15/16/17 by entry hand) is config-only and **not wired** in v1. |
| `middle_stay` | string or null | `null`. The contested middle-row repeat rung is off by default; see §5.6. |
| `entry_min_pair_rank` | integer | `12` — the minimum top-row **pair rank** that earns Fantasy Land, i.e. queens. Spelled as a rank so a bot can compare it against a rank it already holds, rather than parsing the prose "QQ+". |

There is **no `points_per_chip` key.** The ruled quantity is `point_value` — chips per point — and `points_per_chip` would be its reciprocal (1/50) and would state the ruling backwards.

### 3.2 Round Start State (`round_start.state`)

Sent to each participant at the start of each hand.

```json
{
  "hand_number": 1,
  "dealer_seat": 0,
  "your_seat": 1,
  "your_hole_cards": ["2c", "3d", "4h", "5s", "6s"],
  "cards_to_place": ["2c", "3d", "4h", "5s", "6s"],
  "your_rows": {"top": [], "middle": [], "bottom": []},
  "pot": 0,
  "post_blind_stacks": [10000, 10000],
  "point_value": 50,
  "in_fantasy_land": false,
  "phase_sequence": ["deal1", "street2", "street3", "street4", "street5", "complete"],
  "seats": [
    {
      "seat": 0,
      "rows": {"top": [], "middle": [], "bottom": []},
      "placed": 0,
      "royalties": {"top": 0, "middle": 0, "bottom": 0},
      "fouled": false,
      "stack": 10000,
      "in_fantasy_land": false,
      "hidden": false
    }
  ],
  "stacks": [10000, 10000],
  "deck_commitment": ""
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hand_number` | integer | Yes | 1-indexed hand number within the match. |
| `dealer_seat` | integer | Yes | Button seat index, 0-indexed. **Can repeat across hands** — see the button freeze in §5.6. |
| `your_seat` | integer | Yes | **New key.** The receiving seat's own index. |
| `your_hole_cards` | array of string | Yes | The receiving seat's pending cards — its opening set (5, or 14 in Fantasy Land). |
| `cards_to_place` | array of string | Yes | **New key.** The same cards, under the name the placement action uses. Carried under both names so Rule 1 holds and OFC-aware clients read a name that means what it says. |
| `your_rows` | object | Yes | **New key.** The receiving seat's own three rows. Empty arrays at hand start. Never redacted from its owner. |
| `pot` | integer | Yes | `0`. See §5.8. |
| `post_blind_stacks` | array of integer | Yes | Per-seat stacks. Identical to `stacks` — OFC posts nothing. Carried for shape compatibility. |
| `point_value` | integer | Yes | **New key.** Chips per point this hand. |
| `in_fantasy_land` | boolean | Yes | **New key.** Whether the receiving seat plays **this** hand in Fantasy Land. |
| `phase_sequence` | array of string | Yes | **New key.** The phases **this seat** will walk. `["deal1", "complete"]` for a Fantasy Land seat. See §5.6. |
| `seats` | array of PublicSeat | Yes | **New key.** One entry per seat — see §3.10. Redacted per seat for Fantasy Land. |
| `stacks` | array of integer | Yes | Per-seat stacks. Merged by the transport layer, not by the rule set. |
| `deck_commitment` | string | Yes | `SHA-256(deck_seed \|\| deck_order)`, or `""` when RNG verification is not enabled. OFC never reshuffles, so the commitment covers the whole hand. |

The spectator variant carries `hand_number`, `dealer_seat`, `pot`, `post_blind_stacks`, `point_value` and `seats` (redacted with no viewer), plus the transport layer's `stacks` and `deck_commitment`. In OFC that subtraction is small: everything already placed is public, so a spectator sees the whole game except which cards are on their way to a board and which were thrown away.

### 3.3 Turn Request State (`turn_request.state`)

The core decision payload.

```json
{
  "hand_number": 1,
  "phase": "street2",
  "board": [],
  "your_hole_cards": ["9s", "Js", "Ks"],
  "pot": 0,
  "your_stack": 10000,
  "opponent_stacks": [10000],
  "to_call": 0,
  "min_raise": 0,
  "max_raise": 0,
  "dealer_seat": 0,
  "your_seat": 1,
  "your_rows": {"top": ["2c"], "middle": ["3d", "4h"], "bottom": ["5s", "6s"]},
  "opponent_rows": {"0": {"top": ["7c"], "middle": ["8d", "9h"], "bottom": ["Ts", "Jc"]}},
  "cards_to_place": ["9s", "Js", "Ks"],
  "place": 2,
  "must_discard": 1,
  "row_capacity": {"top": 2, "middle": 3, "bottom": 3},
  "royalties": {"top": 0, "middle": 0, "bottom": 0},
  "opponent_royalties": {"0": {"top": 0, "middle": 0, "bottom": 0}},
  "point_value": 50,
  "in_fantasy_land": false,
  "phase_sequence": ["deal1", "street2", "street3", "street4", "street5", "complete"],
  "action_history": [
    {"seat": 1, "action": "place", "amount": 1001, "phase": "deal1", "is_timeout": false},
    {"seat": 0, "action": "place", "amount": 1001, "phase": "deal1", "is_timeout": false}
  ]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hand_number` | integer | Yes | Current hand number. |
| `phase` | string | Yes | One of the six phase strings in §4.2. |
| `board` | array of string | Yes | **Always `[]`.** Carried for Rule 1. |
| `your_hole_cards` | array of string | Yes | The acting seat's pending cards — identical to `cards_to_place`. Carried under the NLHE name so an existing parser survives. |
| `pot` | integer | Yes | `0`. See §5.8. |
| `your_stack` | integer | Yes | Acting seat's chips. |
| `opponent_stacks` | array of integer | Yes | Other seats' chips in seat order, excluding the acting seat. Length N-1. |
| `to_call` | integer | Yes | **Always `0`.** OFC has no wagering. |
| `min_raise` | integer | Yes | **Always `0`.** |
| `max_raise` | integer | Yes | **Always `0`.** |
| `dealer_seat` | integer | Yes | **New key.** Button seat index. |
| `your_seat` | integer | Yes | **New key.** The acting seat's own index. |
| `your_rows` | object | Yes | **New key.** The acting seat's three rows, by row name, **unredacted**. A seat always sees its own board. |
| `opponent_rows` | object | Yes | **New key.** Map of **seat index as a decimal string** to that seat's rows. **Public information** — a placed card is visible to everyone the moment it is placed; that is the whole game. The exception is a hidden Fantasy Land board, whose rows read as empty arrays (§5.6). |
| `cards_to_place` | array of string | Yes | **New key.** The cards this street brought the acting seat: 5 on the opening set, 3 on a pineapple street, 14 in Fantasy Land. |
| `place` | integer | Yes | **New key.** How many of `cards_to_place` must be placed: 5 / 2 / 13. **Per seat**, not per phase — a Fantasy Land seat places 13 in the same phase where the opponent places 5. |
| `must_discard` | integer | Yes | **New key.** How many must be discarded: 0 on the opening set, 1 on a pineapple street, 1 in Fantasy Land. `place + must_discard == len(cards_to_place)` always. |
| `row_capacity` | object | Yes | **New key.** Free slots per row for the acting seat, by row name. |
| `royalties` | object | Yes | **New key.** The acting seat's own royalties per row, live. Only a **complete** row can pay; an incomplete row reports `0`. |
| `opponent_royalties` | object | Yes | **New key.** Map of seat index (decimal string) to that seat's live royalties per row. Zeroed for a hidden Fantasy Land board (§5.6). |
| `point_value` | integer | Yes | **New key.** Chips per point this hand. |
| `in_fantasy_land` | boolean | Yes | **New key.** Whether the acting seat is playing this hand in Fantasy Land. |
| `phase_sequence` | array of string | Yes | **New key.** The phases **this seat** walks. A progress indicator MUST read this, not the match-level phase list. |
| `action_history` | array of ActionEntry | Yes | Every placement this hand, chronological. **Masks only — no card ever appears in the trail** (§3.4). |

`royalties` is reported live rather than only at showdown because it is the number a player is optimising, and it leaks nothing: the rows it is computed from are public. A fouled board's raw royalties are still shown — they are what was forfeited — and the `fouled` flag next to them in the `seats` entries is what says they will not be collected.

The royalty schedule itself is a rules matter, not a protocol matter: read the values the server sends rather than recomputing them.

### 3.4 Action Entry Schema

Identical in shape to NLHE's, with an OFC-specific reading of `amount`.

```json
{"seat": 1, "action": "place", "amount": 1001, "phase": "deal1", "is_timeout": false}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `seat` | integer | Yes | 0-indexed seat that acted. |
| `action` | string | Yes | Always `"place"`. |
| `amount` | integer | Yes | The **placement mask**, not chips. Two bits per dealt card, in the order the cards were dealt: `0` discard, `1` top, `2` middle, `3` bottom. `mask = sum(code_i << (2 * i))`. |
| `phase` | string | Yes | The phase the placement occurred in. |
| `is_timeout` | boolean | Yes | `true` if the server substituted this placement after a timeout. |

**The trail carries masks, never cards.** A mask is only interpretable against the pending cards of the seat that acted, which the trail does not carry — so the action history leaks nothing about a hidden Fantasy Land board. The readable placement sequence lives in the hand record (§6), which exists only after the hand is over.

### 3.5 Turn Action Params (`turn_action.params`)

There is one action.

| Action | `params` value | Constraint |
|---|---|---|
| `place` | `{"placements": [{"card": <string>, "row": <string>}, ...], "discard": <string or array>}` | Exactly `place` placements and exactly `must_discard` discards, drawn from `cards_to_place`, each card used once, and every named row must have free capacity **counting the other placements in the same action**. |

**Example — a pineapple street (place 2, discard 1):**

```json
{
  "action": "place",
  "params": {
    "placements": [
      {"card": "Ks", "row": "bottom"},
      {"card": "Js", "row": "middle"}
    ],
    "discard": "9s"
  }
}
```

**Example — the opening set (place 5, discard 0):**

```json
{
  "action": "place",
  "params": {
    "placements": [
      {"card": "2c", "row": "top"},
      {"card": "3d", "row": "middle"},
      {"card": "4h", "row": "middle"},
      {"card": "5s", "row": "bottom"},
      {"card": "6s", "row": "bottom"}
    ],
    "discard": []
  }
}
```

Row names are `"top"`, `"middle"`, `"bottom"` — the keys of `game_config.rows` and of `row_capacity`. The placement list may be hung off `placements`, `placement`, `place` or `cards`, most specific first; `placements` is canonical. The discard may be hung off `discard` or `discards`, and may be a single card string or an array.

**An illegal assignment is REJECTED, not clamped.** This is the sharpest behavioural difference from NLHE and from 27TD, both of which quietly coerce a bad submission into the nearest legal one. Placement is irrevocable, so there is no defensible "nearest legal placement" — silently moving a card to a different row would corrupt a board the player can never re-set. A rejected placement is returned as an `action_rejected` error carrying `row_capacity`, and the seat gets its normal retry window. A second bad submission falls through to the server's default placement (§5.9).

### 3.6 Turn Result Details (`turn_result.details`)

Unchanged from NLHE in shape. For a `place`, `amount` is the placement **mask** (§3.4), and `pot` and `stacks` do not move — nothing is transferred until the hand settles.

### 3.7 Phase Change State (`phase_change.state`)

Broadcast when a street completes.

```json
{
  "phase": "street2",
  "board": [],
  "place": 2,
  "must_discard": 1,
  "rows": {
    "0": {"top": ["7c"], "middle": ["8d", "9h"], "bottom": ["Ts", "Jc"]},
    "1": {"top": ["2c"], "middle": ["3d", "4h"], "bottom": ["5s", "6s"]}
  },
  "placed": {"0": 5, "1": 5},
  "hidden": []
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `phase` | string | Yes | The new phase (§4.2). |
| `board` | array of string | Yes | **Always `[]`.** |
| `place` | integer | Yes | The **phase-level** placement count — what a non-Fantasy-Land seat owes this street. A Fantasy Land seat's own 14/13/1 reaches it through its per-seat `turn_request.state`. |
| `must_discard` | integer | Yes | The phase-level discard count. |
| `rows` | object | Yes | Map of seat index (decimal string) to that seat's rows, spectator-redacted: a hidden Fantasy Land board reads as empty arrays. |
| `placed` | object | Yes | Map of seat index (decimal string) to how many cards that seat has down. **Stays public even for a hidden board** — the count is public at a live table. |
| `hidden` | array of integer | Yes | Seat indices whose rows are currently hidden. An empty row is never to be mistaken for an unplayed one; this array is what disambiguates. |

### 3.8 Round Result (`round_result.result`)

The round-result envelope is **game-agnostic and unchanged**: `hand_number`, `winner_seats`, `pot`, `payouts`, `showdown`, `action_history`, `stacks`, `deck_commitment`, `deck_reveal`. Only the `showdown` entries are OFC-shaped.

`pot` is the **virtual** pot — the total chips transferred at settlement (§5.8). `payouts` entries carry `refund: 0` always: OFC has no uncontested committed chips to hand back.

### 3.9 Showdown Entry Schema

The contract's four NLHE keys keep their meaning as closely as OFC allows, and the OFC breakdown is folded in **additively**.

```json
{
  "seat": 0,
  "hole_cards": ["7c", "7d", "7h", "9c", "9d", "9h", "2c", "Ac", "Ad", "Ah", "Kc", "Kd", "2d"],
  "hand_rank": "Full House",
  "best_hand": ["Ac", "Ad", "Ah", "Kc", "Kd"],
  "rows": {
    "top": ["7c", "7d", "7h"],
    "middle": ["9c", "9d", "9h", "2c", "2d"],
    "bottom": ["Ac", "Ad", "Ah", "Kc", "Kd"]
  },
  "row_royalties": {"top": 15, "middle": 12, "bottom": 6},
  "royalties": 33,
  "fouled": false,
  "complete": true,
  "points": 6,
  "net_chips": 300,
  "fantasy_land": true,
  "in_fantasy_land": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `seat` | integer | Yes | Seat index. |
| `hole_cards` | array of string | Yes | The seat's **thirteen placed cards** — public by showdown. Discards are absent (§5.5). |
| `hand_rank` | string | Yes | The **bottom row's** hand category as a display string (`"Two Pair"`, `"Full House"`, …), or `"Foul"` for a mis-set board, or `""` for an incomplete one. **Not an NLHE hand-rank enum value** — the casing and spacing differ, and a client MUST NOT feed it into an NLHE lookup. |
| `best_hand` | array of string | Yes | The bottom row — the strongest row a legal board can have. `[]` for an incomplete board. |
| `rows` | object | Yes | **New key.** The seat's three rows, by row name, unredacted. |
| `row_royalties` | object | Yes | **New key.** Royalties per row, before the foul rule. |
| `royalties` | integer | Yes | **New key.** Total royalties collected. **`0` for a fouled board**, even though `row_royalties` still shows what was forfeited. |
| `fouled` | boolean | Yes | **New key.** Whether the completed board is mis-set (§5.4). |
| `complete` | boolean | Yes | **New key.** Whether all 13 cards were placed. |
| `points` | integer | Yes | **New key.** The seat's net points for the hand. Sums to zero across seats. |
| `net_chips` | integer | Yes | **New key.** The seat's net chips: points x `point_value`, table-stakes capped. Sums to zero across seats. |
| `fantasy_land` | boolean | Yes | **New key.** Whether the seat plays the **next** hand in Fantasy Land. |
| `in_fantasy_land` | boolean | Yes | **New key.** Whether the seat played **this** hand in Fantasy Land. |

Showdown entries appear for **every** seat: OFC has no fold, so nobody is out of the hand.

### 3.10 Public Seat Schema

Used in the `seats` array of `round_start.state`.

```json
{
  "seat": 0,
  "rows": {"top": [], "middle": [], "bottom": []},
  "placed": 0,
  "royalties": {"top": 0, "middle": 0, "bottom": 0},
  "fouled": false,
  "stack": 10000,
  "in_fantasy_land": false,
  "hidden": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `seat` | integer | Yes | Seat index. |
| `rows` | object | Yes | The seat's rows **as this viewer may see them**. Empty arrays for a hidden Fantasy Land board. |
| `placed` | integer | Yes | Cards down. **Public even for a hidden board.** |
| `royalties` | object | Yes | Live royalties per row, or all-zero for a hidden board. |
| `fouled` | boolean | Yes | Whether the completed board is mis-set. **Always `false` for a hidden board** — "this secret board is mis-set" is precisely what an opponent with cards left to place must not know. |
| `stack` | integer | Yes | The seat's chips. |
| `in_fantasy_land` | boolean | Yes | Whether the seat is playing this hand in Fantasy Land. **Public.** |
| `hidden` | boolean | Yes | Whether this entry is redacted. Says which of the two readings the consumer is looking at, so an empty row is never mistaken for an unplayed one. |

---

## 4. Phases and the action vocabulary

### 4.1 Action vocabulary

| Action | Available | Notes |
|---|---|---|
| `place` | Every non-terminal phase, for a seat that owes a placement | **The only action.** |

There is **no `fold`**, no `check`, no `call`, no `raise` and no `bet`. Layer 1's `turn_request.valid_actions` is exactly `["place"]` on every OFC turn. The *shape* of a legal placement lives in the state payload (`cards_to_place`, `place`, `must_discard`, `row_capacity`), not in `valid_actions`: enumerating every legal card-to-row assignment would mean 3^5 = 243 entries for the opening set alone, and orders of magnitude more for a Fantasy Land turn.

**A seat that stops responding cannot fold its way out.** It is asked for a placement every street, forever, and the server places for it (§5.9).

### 4.2 Phase strings

Six phases, in order:

| # | `phase` | Dealt | Place | Discard | Meaning |
|---|---|---|---|---|---|
| 1 | `deal1` | 5 | 5 | 0 | The opening set. |
| 2 | `street2` | 3 | 2 | 1 | Pineapple street. |
| 3 | `street3` | 3 | 2 | 1 | Pineapple street. |
| 4 | `street4` | 3 | 2 | 1 | Pineapple street. |
| 5 | `street5` | 3 | 2 | 1 | Pineapple street. |
| 6 | `complete` | — | — | — | Terminal. Boards compared, points settled. |

`5 + 4x3 = 17` cards dealt, `13` placed, `4` discarded.

**A Fantasy Land seat walks a different sequence:** `["deal1", "complete"]` — 14 cards, 13 placed and 1 discarded in a single turn during `deal1`, and nothing owed afterward while the opponent plays a normal 17-card hand. The match-level phase list is the display superset; **`turn_request.state.phase_sequence` is the per-seat truth** and is what a progress indicator must read.

### 4.3 Turn order

**In-turn placement**, not simultaneous: the first seat left of the button places first every street, and the button places last with full information. Heads-up that means the non-button seat leads every street, which is exactly why the button alternates.

Each seat's placement is a separate `turn_request` / `turn_action` exchange.

---

## 5. Semantic Rules

### 5.1 There is no betting

**Normative.** `to_call`, `min_raise` and `max_raise` are `0` in every payload. `pot` is `0` for the whole hand and becomes the settled transfer only at hand end (§5.8). `game_config` carries **no** `betting_structure`, `small_blind`, `big_blind` or `bet_cap` key — there is nothing for them to describe. `post_blind_stacks` equals `stacks` because nothing is posted.

A client that computes a bet size from these fields computes zero, which is correct.

### 5.2 The board is public; the hand is private

**Normative — this is the inverse of poker, and getting it backwards is a class of bug.**

- **Public to everyone, including spectators:** every placed card, every seat's `placed` count, every seat's live royalties, every seat's Fantasy Land status.
- **Private to its owner:** the cards **awaiting placement** (`cards_to_place` / `your_hole_cards`). A seat sees its own pull and nobody else's.
- **Private to nobody — that is, never in any payload at all:** the **discards**. They are face-down and dead, and they do not appear even in the owner's own showdown entry.

`build_spectator_view` is therefore roughly the participant view *minus the hand*, where in poker it is the participant view minus the board's hidden half.

### 5.3 Placement is irrevocable

**Normative.** A card placed in a row stays there for the hand. There is no re-setting, no moving between rows, and no taking a card back. This is why an illegal placement is rejected rather than coerced (§3.5), and why the hand record can be reconstructed by replaying the placement masks (§6).

A row is full when it holds its capacity from `game_config.rows` — top 3, middle 5, bottom 5. `row_capacity` in `turn_request.state` reports the free slots.

### 5.4 Fouling

A completed board is **fouled** when the rows are mis-set: the ranking must be non-decreasing from top to bottom (bottom at least as strong as middle, middle at least as strong as top).

A fouled board collects **no royalties** and pays a flat settlement to every non-fouling opponent. A fouled Fantasy Land board is punished exactly like any other — 14 cards means more options, not more forgiveness — and it also **loses** Fantasy Land.

`fouled` is reported in the showdown entry and in the public seat entries. It is **suppressed to `false`** for a hidden Fantasy Land board (§5.6): the flag is as derived from the rows as the royalties are.

### 5.5 Discards

Each pineapple street discards exactly one card; the opening set discards none; a Fantasy Land turn discards one of fourteen. Four dead cards per non-Fantasy-Land seat, one per Fantasy Land seat.

Discards are face-down and dead **in play** and appear in **no live payload**. They do appear in the hand record (§6), because a hand history that cannot account for 4 of a seat's 17 cards is not a hand history — but by then the hand is over.

### 5.6 Fantasy Land

**Fantasy Land is supported from day one.** It is not a later slice, and a client that declares `"ofc"` must handle it.

**What it is.** A seat that earns Fantasy Land plays the **next** hand differently: it is dealt **14 cards at once, before the first street**, sets all 13 in **one** `place` action during `deal1`, discards 1, and then owes nothing for the rest of the hand while the opponent plays a normal 17-card Pineapple hand.

**Entry (a seat not currently in Fantasy Land):** queens or better **in the top row**, on a board that did **not** foul. `game_config.fantasy_land.entry_min_pair_rank` states the rank numerically.

**Repeat (a seat already in Fantasy Land):** the stricter test — **trips or better in top, OR quads or better in bottom**, on a board that did not foul. Queens in front keeps nobody in Fantasy Land. The contested middle-row rung is **off by default** (`middle_stay: null`); the two other published readings are a config value away.

**Both tests are evaluated for every seat every hand**, regardless of anyone's current status. So a seat can enter Fantasy Land during the opponent's Fantasy Land hand, and **both seats can be in Fantasy Land in the same hand** — in which case the whole hand is two placements.

**Card count is flat 14**, on entry and on repeat. Progressive Fantasy Land is not wired.

**Scoring is normal.** Royalties as usual, no multiplier.

**Redaction — the inversion, twice over.** A Fantasy Land seat sets thirteen cards in secret and commits before the opponent's board develops, so **its rows are private to it until showdown** while every non-Fantasy-Land seat's rows stay public **in the same payload**. Redaction is therefore a **per-seat** question, not a per-payload one. For a hidden seat, as seen by anyone but its owner:

| Field | Value while hidden |
|---|---|
| `opponent_rows[<seat>]` / `seats[].rows` / `phase_change.rows[<seat>]` | `{"top": [], "middle": [], "bottom": []}` — **empty arrays, never a placeholder card** (Rule 1). |
| `opponent_royalties[<seat>]` / `seats[].royalties` | all-zero. Royalties are a pure function of the rows; publishing them would leak the board a number at a time. |
| `seats[].fouled` | `false`, suppressed. |
| `seats[].placed` / `phase_change.placed[<seat>]` | **the real count — public.** |
| `seats[].in_fantasy_land` | **`true` — public.** |
| `seats[].hidden` / `phase_change.hidden` | `true` / contains the seat. |

The owner's own `your_rows` is **never** redacted.

**The button freezes into a Fantasy Land hand.** A Fantasy Land hand is an extension of the hand that earned it, so the button does **not** advance into it: `dealer_seat` repeats across the two hands. Rotation resumes the moment nobody is in Fantasy Land. A client tracking position MUST read `dealer_seat` each hand rather than assuming alternation.

**Cross-hand chain.** Hand N's showdown `fantasy_land` (and the hand record's `fantasy_land_next`) is hand N+1's `in_fantasy_land` (and the hand record's `fantasy_land_in`), for the same seat. A consumer replaying a match can and should check that.

### 5.7 Points, chips, and settlement

**Normative. OFC ships as a chip game.** The game scores points; the platform is chip-denominated end to end; the bridge is one number.

- **1 point = 50 chips** at the opening level (`point_value`), against the standard 10,000 stack — a **200-point bank** (`bank_points`).
- What escalates is `point_value`, on a 20-hand cadence. Nothing overloads a blind field, because OFC has no blinds.
- Scoring is the **1-6 system**: one point per row won, plus a 3-point scoop bonus for winning all three rows, plus royalties.
- **Multi-way is pairwise.** Every unordered pair of seats is scored independently and the results summed. There is no three-way comparison rule. Net points therefore sum to zero by construction.
- **Chips are table-stakes capped, per pair.** A seat's total loss across all its pairings is capped at what it brought to the table. `net_chips` sums to zero and no seat can go negative.
- Both `points` and `net_chips` appear in the showdown entry, and `point_value` appears in every state payload, so the two can always be reconciled.

### 5.8 The pot is virtual

**Normative.** OFC has no pot. `pot` is `0` for the entire hand and becomes, at settlement, the **total chips transferred** — the sum of the positive `net_chips`.

It exists so the runtime's chip accounting balances without a special case: losing seats are debited when the hand completes, the total is parked in `pot`, and winners are credited from `payouts`. `round_result.result.pot` is that transferred total, and `payouts[].refund` is `0` always.

Do not read `pot` as "chips at stake". Read `bank_points`, `point_value` and the boards.

### 5.9 Timeouts and substitution

A seat that does not act gets a **legal auto-placement**: fill the row with the most remaining capacity, tied toward the back. NLHE's check-else-fold default names two actions OFC does not have.

Substituted placements carry `is_timeout: true` in `action_history`.

**Consecutive-substitution budgets are scaled for OFC.** The platform counts consecutive substituted *decisions*, so the same number buys a different amount of unresponsiveness per game. Measured with each rule set's own substituted action, a silent seat is asked for **5.00** decisions per hand in OFC against **1.36** in NLHE and **1.10** in 27TD — OFC has no fold, so a silent seat is asked every street forever instead of being out of the hand after one. Un-scaled, a dead OFC seat would forfeit about **four times** sooner than a dead NLHE one, so OFC's budget is **four times** the configured value. It is a multiplier rather than a constant, so the platform setting still governs, including its documented "`<= 0` disables the escalation" meaning.

---

## 6. Hand-record payload

**Frozen, schema version 1.** A completed OFC hand leaves a structured record behind. It rides in the game-neutral variant envelope under a `variant` key, discriminated by `game_type`.

```jsonc
{
  "variant": {
    "game_type": "ofc",
    "schema_version": 1,

    "point_value": 50,

    "seats": [
      {
        "seat": 0,
        "rows": {
          "top": ["2c", "3d", "4h"],
          "middle": ["5s", "6s", "7c", "8d", "9h"],
          "bottom": ["Ts", "Js", "Qs", "Ks", "As"]
        },
        "streets": [
          {
            "phase": "deal1",
            "pull": ["2c", "3d", "4h", "5s", "6s"],
            "placements": [
              {"card": "2c", "row": "top"},
              {"card": "3d", "row": "top"},
              {"card": "4h", "row": "top"},
              {"card": "5s", "row": "middle"},
              {"card": "6s", "row": "middle"}
            ],
            "discarded": []
          }
        ],
        "discarded": ["9s", "Jd", "Kd", "2d"],
        "placed": 13,
        "row_royalties": {"top": 0, "middle": 0, "bottom": 0},
        "royalties": 0,
        "fouled": false,
        "complete": true,
        "points": 6,
        "net_chips": 300,
        "fantasy_land_in": false,
        "fantasy_land_next": true
      }
    ],

    "settlement": {
      "net_points": [6, -6],
      "chip_deltas": [300, -300],
      "transferred": 300
    }
  }
}
```

| Field | Type | Description |
|---|---|---|
| `game_type` | string | `"ofc"`. **Discriminate on this; never sniff keys.** |
| `schema_version` | integer | `1`. A version you do not know means degrade, never guess. |
| `point_value` | integer | Chips per point for this hand, so `points` and `net_chips` reconcile from the record alone. |
| `seats` | array of object | One entry per seat, each carrying an integer `seat`. Per-seat card data lives **here and nowhere else** — human-seat redaction filters on exactly this shape. |
| `seats[].rows` | object | The final board by row name. |
| `seats[].streets` | array of object | One entry per street the seat played. |
| `seats[].streets[].phase` | string | `deal1` / `street2` / `street3` / `street4` / `street5`. |
| `seats[].streets[].pull` | array of string | The cards this street brought, in a **canonical** order: placements in mask order, then the discard. The physical arrival order is not recoverable from an append-only board and no rule depends on it, so the record states a canonical order rather than inventing one. |
| `seats[].streets[].placements` | array of object | `{"card": <string>, "row": <string>}`, in mask order. |
| `seats[].streets[].discarded` | array of string | `[]` on the opening set, 1 card per pineapple street. |
| `seats[].discarded` | array of string | All the seat's dead cards for the hand — 4 normally, 1 in Fantasy Land. |
| `seats[].placed` | integer | `13` for a completed board. |
| `seats[].row_royalties` | object | Royalties per row, before the foul rule. |
| `seats[].royalties` | integer | Total collected. `0` for a fouled board. |
| `seats[].fouled` | boolean | Whether the board is mis-set. |
| `seats[].complete` | boolean | Whether all 13 cards were placed. |
| `seats[].points` | integer | Net points. |
| `seats[].net_chips` | integer | Net chips. |
| `seats[].fantasy_land_in` | boolean | Carried **into** the hand. |
| `seats[].fantasy_land_next` | boolean | Earned **by** the hand. |
| `settlement.net_points` | array of integer | Per seat. Sums to zero. |
| `settlement.chip_deltas` | array of integer | Per seat. Sums to zero, table-stakes capped. |
| `settlement.transferred` | integer | The **virtual** pot (§5.8). Named `transferred` rather than `pot` to keep that honest. |

The scored half (`row_royalties`, `royalties`, `fouled`, `complete`, `points`, `net_chips`, `fantasy_land_next`) is read from the showdown payload rather than recomputed, so the record and the showdown broadcast can never disagree.

### 6.1 A Fantasy Land hand in the record

Fantasy Land adds **no key**. It changes the **shape of one seat's `streets` array**, and the schema version does not move because every field keeps its meaning:

| Field | Normal seat | Fantasy Land seat |
|---|---|---|
| `streets` | **5** entries (`deal1` plus four pineapple streets) | **1** entry, `phase: "deal1"` |
| `streets[0].pull` | 5 cards | **14** cards |
| `streets[0].placements` | 5 | **13** — the whole board, one action |
| `streets[0].discarded` | `[]` | **1** card |
| `discarded` | 4 dead cards | **1** dead card |
| `placed` | 13 | 13 (unchanged) |
| `fantasy_land_in` | `false` | **`true`** |
| `fantasy_land_next` | entry test: queens or better in front, non-fouled | repeat test: trips+ top or quads+ bottom, non-fouled |

Further notes:

- **The card count is not a separate field**, because it is a constant — `len(streets[0].pull)` states it anyway.
- **There is no `hidden` flag in the record.** The Fantasy Land board is secret in the *live* view builders, and the hand is over by the time a record exists, so the record carries the full board exactly like any other. The redaction that matters is live (§5.6).
- **The button is not in this block** and does not need to be: a frozen button shows up as two consecutive hands with the same dealer, which the platform's own hand columns already carry.
- **Both seats can be in Fantasy Land in the same hand**, in which case both `streets` arrays have one entry and the hand has exactly two placements in total.

### 6.2 Reader rules

- Discriminate on `game_type`; never sniff keys.
- A `schema_version` you do not know means degrade, never guess.
- The live `hole_cards` snapshot the platform records for a hand is **not** a complete record for a variant. Read the variant block.
- The record is inside the per-hand audit chain: mutating a recorded placement or royalty changes that hand's hash, every subsequent hash, and the match checksum.

---

## 7. Versioning

This document describes **v1** of the Pineapple OFC Game State Protocol. The policy is [`LAYER2-COMMON.md`](LAYER2-COMMON.md) §4 — Layer 1 stays at `1.0`, additive changes do not bump this document, removals and retypings do. OFC adds two things to it:

- Classic OFC, progressive OFC and 2-7 OFC would each be a **new `variant` value** with their own document, not a revision of this one. Wiring progressive Fantasy Land would add a per-seat card count to the carryover and to the hand record, and would move the hand-record `schema_version`.
- The hand-record payload (§6) versions separately, via its own `schema_version`, because it is persisted: a shape change on already-written rows is a migration, not an edit.

---

## 8. Related documents

- [`LAYER2-COMMON.md`](LAYER2-COMMON.md) — the Layer 2 baseline this document inherits (§1, §2).
- [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md) — Layer 1. Unchanged by this document.
- [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) — Layer 2 for NLHE. Card notation (§1) and the action-entry shape are shared verbatim.
- [`DRAW27-GAME-STATE-PROTOCOL.md`](DRAW27-GAME-STATE-PROTOCOL.md) — Layer 2 for 2-7 Triple Draw, the other variant landing under epic `chipzen-ai/Chipzen#4200`.

This document and its mirror are drift-guarded: a normalized digest is committed beside each copy and pinned in both repositories, so a one-sided edit turns that side red.
