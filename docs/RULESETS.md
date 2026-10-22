# Rule Sets

**Date:** 2026-09-29
**Status:** Draft (chipzen-ai/chipzen-sdk#138). The first game to use rule sets is **Big Bad Bots** (B3), the robot-fighting game, whose game type id is `bigbadbots`. Three rule sets are released, all in `preview` (§7): `bigbadbots-1.0` (engine `1.0.0`), `bigbadbots-1.1` (engine `1.1.0`) and `bigbadbots-1.2` (engine `1.2.0`), the one chosen for launch. The id was `fighter` until 2026-10-01, when it was renamed before any match was played (chipzen-ai/Chipzen#5291); the one rule set released under the old name, `fighter-1.0`, is retired (§7).

> **Provisional values.** These are placeholders pending a rules decision (D-13), and may change
> before the first rule set is released:
>
> | Value | Placeholder | Decision |
> |---|---|---|
> | Notice period before a new rule set goes live | **14 days** (provisional) | D-13 |
> | Rating continuity | ratings **continue across minor versions**; a **major version starts a new rating track** (provisional) | D-13 |

---

## Overview

A **rule set** is the complete, named set of numbers a game is played under: for Big Bad Bots (`bigbadbots`), the budget, the bodies and parts, the move frame data, the arena, the round length, the per-tick deadline, the forfeit rule, and every other tunable. Every match is played under exactly one rule set, named in its `match_start.game_config`, and a bot never needs any other source for the numbers it plays by.

Three things version independently, and they are easy to confuse:

| What | Example | Versioned by | Changes when |
|---|---|---|---|
| **Layer 2 dialect** | `variant: "bigbadbots"` | its protocol document ([`BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md) §8) | a payload shape changes |
| **Rule set** | `rule_set_id: "bigbadbots-1.0"` | this document | the rules change |
| **Engine** | `1.4.2` | SemVer | the software that runs the rules changes |

A balance patch is a new rule set. It is never a new Layer 2 variant, and never an edit to an existing rule set.

---

## 1. Rule-set ids

```
bigbadbots-MAJOR.MINOR
```

- `MAJOR` and `MINOR` are non-negative integers without leading zeros: `bigbadbots-1.0`, `bigbadbots-1.1`, `bigbadbots-2.0`.
- The prefix is the `game_type` id, not the game's display name: Big Bad Bots rule sets are `bigbadbots-…`. Each game numbers its rule sets on its own.
- An id with a lowercase suffix, `bigbadbots-1.1-dev`, names a **local or test** rule set. It is never offered for rated or competition play.

In full: `^bigbadbots-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[a-z0-9][a-z0-9-]*)?$`.

### 1.1 Minor versus major

| Bump | May change | Example |
|---|---|---|
| **Minor** (`bigbadbots-1.0` to `bigbadbots-1.1`) | **Numbers only.** Any parameter's value, including a **declared behaviour switch**: a parameter whose default keeps the old behaviour, and which a later minor may flip. | A body's cost; a move's startup; the time bank; turning on a switch that fixes a behavioural bug. |
| **Major** (`bigbadbots-1.x` to `bigbadbots-2.0`) | **Mechanics.** A new action, a new rule, a new kind of part, a removed parameter: anything a number cannot express. | A new fight action; a part that changes a rule no parameter covers. |

A minor never adds, removes or retypes a key in a Layer 2 payload. A major may add payload keys; that is an additive change to the Layer 2 document, and a removal or retyping there is a major version of that document ([`LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md) §4).

### 1.2 Released rule sets are immutable

Once a rule set is released, its content never changes. A fix is a new id. Every rule set has a `sha256` of its canonical content, and every match replay records it, so a rule set that was edited instead of versioned is detected.

---

## 2. Rule-set ids versus engine versions

The engine that runs the rules uses **SemVer**, independently of rule-set ids.

- **Every engine release plays every released rule set exactly.** For a given rule set, seed and set of answers, any later engine release produces the same match, bit for bit. `bigbadbots-1.0` can run on engine `1.0.0`, `1.4.2` or `2.1.0` with the same result.
- An engine **minor** release can add a rule set. An engine **patch** release never changes the result of any match.
- A major rule set may need new engine code. The rule sets of the old major keep running on the code they were released on, unchanged.
- A replay records **both** the rule-set id and the exact engine version, so a match can always be re-verified.

---

## 3. Lifecycle

Each rule set is in one of four states. The state is looked up **when a match is created**; a match already created keeps its rule set whatever happens afterwards (§5).

| State | New rated or competition matches | Practice, and explicit selection | Replays and verification |
|---|---|---|---|
| `preview` | no | yes | yes |
| `live` | **yes** (the default for new matches) | yes | yes |
| `deprecated` | no, except competitions already pinned to it | yes, until its retirement date | yes |
| `retired` | no | no | **yes, forever** |

- **`preview`** is a rule set that has been announced and is inside its notice period. Bots can practise against it before it counts.
- **`live`** is the default for new matches. Normally one rule set per game is live.
- **`deprecated`** is a rule set that has been superseded. Its retirement date is published when it is deprecated.
- **`retired`** rule sets never start a match again, but every match played on one stays replayable and verifiable.

Transitions happen at published UTC timestamps. A rule set is never announced before builders can play it on the platform (§6).

### 3.1 Notice period (provisional)

**A new rule set is announced at least 14 days before it goes live (provisional, D-13).** It is in `preview` for the whole notice period: from the announcement until its live date. The live date and the notice are published together with the rule set's changelog entry (§7).

---

## 4. The parameter table

`match_start.game_config` carries the rule set in two keys (see [`BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md) §4.1):

| Key | Content |
|---|---|
| `rule_set_id` | The id, e.g. `"bigbadbots-1.0"`. |
| `rule_set` | The rule set's **full parameter table**, by section: the whole rule set except its human-readable notes. |

The Big Bad Bots (`bigbadbots`) sections:

| Section | What it holds |
|---|---|
| `attributes` | The attribute names, in order. |
| `slot_types` | The part slot types. |
| `gearup` | The budget, the size of each body offer, and how many auction lots are sold. |
| `bodies` | Every body: cost, base attributes, slot layout, special move. |
| `parts` | Every part: its slot type, attribute bonuses, and rule effects. |
| `moves` | Every attack's frame data (startup, active, recovery), stamina cost, damage and range. |
| `specials` | Every special move's frame data, stamina cost, damage and range. |
| `arena` | Arena width, start positions and their per-round shift, and the minimum separation. |
| `rounds` | Ticks per second, ticks per round, round wins needed, and the maximum number of rounds. |
| `fighter` | How a robot's attributes turn into hit points, stamina, stamina regeneration, movement rate and strike range. |
| `combat` | Damage scaling, guard, blocking, stamina drain, stagger, pushback, the repeated-move penalty, and the dazed rule. |
| `special_effects` | The numbers inside the specials' effects (durations, distances, multipliers). |
| `forfeit` | The disconnect grace and how many forfeited rounds forfeit the match. |
| `deadline` | The per-tick deadline, the time bank, the default action on a miss, and what happens to a late answer. On the platform the deadline and the bank are overridden by the response clock (§6). |

The table for each released rule set is generated from the rule set itself and published in its changelog entry (§7), so the published numbers cannot drift from the played ones.

### 4.1 In the Python SDK

A `chipzen.BigBadBotsBot` reads the rule set from `match_start` and hands it to every call, in every phase, as `state.rules` (`chipzen.bigbadbots_rules.RuleSet`): the id, the full table read-only, and typed `gearup`, `arena`, `rounds`, `forfeit` and `deadline` sections. A bot that reads its numbers from there runs unchanged on a new rule set.

The SDK also bundles the published numbers of every released rule set, so a bot can look one up offline with `chipzen.bigbadbots_rules.published_rule_set("bigbadbots-1.2")`. They are a reference only: the bot always plays the numbers `match_start` sent. A `rule_set_id` the installed SDK does not know, or a published value that differs from the one sent, logs a warning, and the bot plays on.

---

## 5. How a match pins a rule set

- **At creation.** The rule set is chosen when the match is created, and never changes during it. `game_config.rule_set_id` and `game_config.rule_set` state it to both bots at `match_start`.
- **In the record.** The replay records the rule-set id, the rule set's `sha256`, and the exact engine version.
- **Competitions pin once.** A tournament, fixture set or season pins its rule set when it is created, and every match it spawns inherits that pin. A cutover to a new live rule set therefore never splits a bracket across two rule sets.
- **A cutover does not touch matches in progress.** A match created before a rule set's live date finishes on the rule set it was created with.

---

## 6. Playing and testing against a rule set

There is no local Big Bad Bots simulator: the game engine runs on the platform only. Builders test a bot against a rule set by playing it there, against the house bots (see [`DEV-MANUAL.md` §10.9](DEV-MANUAL.md#109-iterating-on-the-platform)).

- **Every match names its rule set.** A match is played under the rule set it was created with (§5), and `match_start.game_config` states it to both bots. A challenge is played under the game's default rule set unless it names another.
- **`preview`** rule sets can be played in practice and by explicit selection (§3), so a bot can be tuned against one on the platform during its notice period, before it counts.
- **`deprecated`** rule sets can still be played, until their published retirement date.
- **`retired`** rule sets never start a match again, but every match played on one stays replayable.
- **Never a silent fallback.** The bot always plays the numbers `match_start` sent. The SDK's bundled copies of the published rule sets (§4.1) are a reference only; a `rule_set_id` the installed SDK does not know, or a published value that differs from the one sent, logs a warning, and the bot plays on the server's numbers.

The platform plays every rule set as published (§1.2, §2), with one exception: the response clock. The released rule sets (`bigbadbots-1.0` to `bigbadbots-1.2`) are immutable and still carry decision D-01's numbers in their `deadline` section: a 100 ms per-tick deadline and a 3 s time bank reset every round. Decision D-20 replaced those two numbers, and the platform applies its response clock over any rule set: **250 ms per fight tick, plus a 3 s time bank per seat per match that is never refilled**. `match_start` announces it in `game_config.response_clock` (`deadline_ms`, `time_bank_ms`, `bank_scope`; [protocol §3.4 and §4.1](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md#34-the-response-window-and-the-time-bank-d-20)). The rest of the `deadline` section still applies: `default_action_policy`, `late_answer` and `miss_counts_toward_forfeit`. So read the window and the bank from `game_config.response_clock`, not from `state.rules.deadline`. Every other number a bot reads from `state.rules` is the same in every match played under that rule set.

---

## 7. Changelog

One entry per rule set, newest first. Each entry gives the id, its lifecycle dates, a summary of what changed from its predecessor, and the full parameter table (§4).

### `bigbadbots-1.2`

- **State:** `preview`. Frozen with engine `1.2.0` (chipzen-ai/bigbadbots-engine tag `v1.2.0`, engine line `v1`) on 2026-10-08. This is the rule set chosen for launch.
- **sha256** (canonical JSON, as in every log header): `5e1f453c8f02dc3196cdf6a262e30a6147a1b5d4f472d4ec117e87bab42ac3ca`.
- **Live date:** not set. Its `deadline` and `forfeit` sections are the same as 1.0's, so it stays in `preview` until D-01, D-03 and D-16 are confirmed (chipzen-ai/Chipzen#5089). The notice period (§3.1) starts when a live date is announced.
- **Parameter table:** to be generated from the released file and published here (#138).
- **Changes from `bigbadbots-1.1`** (6 changes, 7 values):
  - **No closing walls.** The three closing-wall keys (`rounds.stall_close_after`, `stall_close_rate`, `stall_min_width`) are left out, so the arena never narrows. `rounds.timeout_tiebreak` stays `"damage"` and a round is still 600 ticks (60 s): a round that reaches the time limit goes to the side that dealt more damage.
  - `combat.damage_scale` 2.0 to 2.3, so rounds end on damage without walls forcing contact.
  - `special_effects.counter_return` 1.25 to 1.0: Warden's Counter returns a cancelled strike at its own damage.
  - Warden health 45 to 40.
  - Mantis reach 58 to 52.
  - Everything else is as in 1.1, including `combat.intent_reveal` `"after_hit"` and `combat.block_chip` 0.40.

### `bigbadbots-1.1`

- **State:** `preview`. Frozen with engine `1.1.0` (chipzen-ai/bigbadbots-engine tag `v1.1.0`, engine line `v1`) on 2026-10-07. Superseded for launch by `bigbadbots-1.2`.
- **sha256** (canonical JSON, as in every log header): `436e579ffbbff971c53957d6dcf8d0c1c2cf1da9096d9dbabf255c4e24e17789`.
- **Live date:** not set (same reasons as 1.2).
- **Parameter table:** to be generated from the released file and published here (#138).
- **Changes from `bigbadbots-1.0`** (29 changes):
  - **Mechanics** (optional rule-set keys, set for the first time):
    - `combat.intent_reveal` `"after_hit"`: what happens on a tick is not visible to the opponent until the next tick, so a strike can no longer be blocked on sight on the tick it lands.
    - **Closing walls:** `rounds.stall_close_after` 50, `stall_close_rate` 0.06, `stall_min_width` 2.0. After 5 s with no damage the walls close in at 0.06 units a tick, down to 2.0 units apart. `FightState` carries the wall positions.
    - **Damage tiebreak:** `rounds.timeout_tiebreak` `"damage"`. A round that reaches the time limit goes to the side that dealt more damage in it; equal damage is a drawn round.
  - **Blocking and the fight:** `blocked_extra_recovery` 3 to 0; `block_chip` 0.25 to 0.40; `damage_scale` 1.8 to 2.0; `strike_range_per_reach` 0.009 to 0.007; `counter_return` 1.5 to 1.25.
  - **Bodies:** slot counts now scale with frame size (2 to 4, Scrapper 5), and several costs changed: Golem 550 to 580, Mantis 450 to 530, Warden 450 to 560, Scrapper 370 to 330, Viper 360 to 340, Brawler 410 to 360.
  - **Parts:** Whip reach +25 to +20; Long arm reach +20 to +16; Targeting reach +15 to +12; Anchor speed -5 to 0; Juggernaut speed -15 to -6; Regenerator `regen_mult` 0.5 to 0.35.

### `bigbadbots-1.0`

- **State:** `preview`. Frozen with engine `1.0.0` (chipzen-ai/bigbadbots-engine tag `v1.0.0`, engine line `v1`) on 2026-10-03.
- **sha256** (canonical JSON, as in every log header): `edc31048adeac8ed315a31a923e92f889c95ffde473d478f492ec0107de916a2`.
- **Content:** the same numbers as the retired `fighter-1.0`, under the new id: the code as played (**provisional, D-04**). Every table is identical; only `rule_set_id` and the notes differ, so the sha256 and the golden replays are new.
- **Live date:** not set. It stays in `preview` until the provisional decisions D-01, D-03, D-04 and D-16 are confirmed (chipzen-ai/Chipzen#5089). The notice period (§3.1) starts when a live date is announced.
- **Parameter table:** to be generated from the released file and published here (#138).
- **Changes:** the first Big Bad Bots rule set under the `bigbadbots` id. No rule changes from `fighter-1.0`.

### `fighter-1.0` (retired)

- **State:** `retired` on 2026-10-01, without ever being played. It was frozen with engine `1.0.0` (chipzen-ai/fighter-mvp tag `v1.0.0`) on 2026-09-29 and stayed in `preview`; the game id was then renamed from `fighter` to `bigbadbots` (chipzen-ai/Chipzen#5291), and `bigbadbots-1.0` replaces it. No engine ships it, not even as an alias, and the id is never reused.
- **sha256** (canonical JSON, as in every log header): `906705ea2c539721250da502de45b902b54dd7f0e77fab6550b0ecc0e29df334`.

---

## 8. Rating continuity (provisional)

**Ratings continue across minor versions of a rule set, and a major version starts a new rating track (provisional, D-13).** A minor changes numbers only, so a bot's strength carries over. A major changes the mechanics, so a rating earned under the old ones says little about the new game.

Every rating event records the rule-set id it was earned under, so either policy can be applied to existing results.

---

## 9. Related documents

- [`protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md): the Big Bad Bots (`bigbadbots`) Layer 2 dialect, and where `game_config` carries the rule set (§4.1).
- [`protocol/LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md): how a Layer 2 dialect itself versions (§4).
- [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md): Layer 1, unchanged by rule sets.
- [`DEV-MANUAL.md` §10.7](DEV-MANUAL.md#107-reading-the-rule-set-staterules): reading the rule set from a bot (`state.rules`).
