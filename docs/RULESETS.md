# Rule Sets

**Date:** 2026-09-29
**Status:** Draft (chipzen-ai/chipzen-sdk#138). The first game to use rule sets is `fighter`, and no fighter rule set has been released yet.

> **Provisional values.** These are placeholders pending a rules decision (D-13), and may change
> before the first rule set is released:
>
> | Value | Placeholder | Decision |
> |---|---|---|
> | Notice period before a new rule set goes live | **14 days** (provisional) | D-13 |
> | Rating continuity | ratings **continue across minor versions**; a **major version starts a new rating track** (provisional) | D-13 |

---

## Overview

A **rule set** is the complete, named set of numbers a game is played under: for `fighter`, the budget, the bodies and parts, the move frame data, the arena, the round length, the per-tick deadline, the forfeit rule, and every other tunable. Every match is played under exactly one rule set, named in its `match_start.game_config`, and a bot never needs any other source for the numbers it plays by.

Three things version independently, and they are easy to confuse:

| What | Example | Versioned by | Changes when |
|---|---|---|---|
| **Layer 2 dialect** | `variant: "fighter"` | its protocol document ([`FIGHTER-GAME-STATE-PROTOCOL.md`](protocol/FIGHTER-GAME-STATE-PROTOCOL.md) §8) | a payload shape changes |
| **Rule set** | `rule_set_id: "fighter-1.0"` | this document | the rules change |
| **Engine** | `1.4.2` | SemVer | the software that runs the rules changes |

A balance patch is a new rule set. It is never a new Layer 2 variant, and never an edit to an existing rule set.

---

## 1. Rule-set ids

```
fighter-MAJOR.MINOR
```

- `MAJOR` and `MINOR` are non-negative integers without leading zeros: `fighter-1.0`, `fighter-1.1`, `fighter-2.0`.
- The prefix is the `game_type`. Each game numbers its rule sets on its own.
- An id with a lowercase suffix, `fighter-1.1-dev`, names a **local or test** rule set. It is never offered for rated or competition play.

In full: `^fighter-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[a-z0-9][a-z0-9-]*)?$`.

### 1.1 Minor versus major

| Bump | May change | Example |
|---|---|---|
| **Minor** (`fighter-1.0` to `fighter-1.1`) | **Numbers only.** Any parameter's value, including a **declared behaviour switch**: a parameter whose default keeps the old behaviour, and which a later minor may flip. | A body's cost; a move's startup; the time bank; turning on a switch that fixes a behavioural bug. |
| **Major** (`fighter-1.x` to `fighter-2.0`) | **Mechanics.** A new action, a new rule, a new kind of part, a removed parameter: anything a number cannot express. | A new fight action; a part that changes a rule no parameter covers. |

A minor never adds, removes or retypes a key in a Layer 2 payload. A major may add payload keys; that is an additive change to the Layer 2 document, and a removal or retyping there is a major version of that document ([`LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md) §4).

### 1.2 Released rule sets are immutable

Once a rule set is released, its content never changes. A fix is a new id. Every rule set has a `sha256` of its canonical content, and every match replay records it, so a rule set that was edited instead of versioned is detected.

---

## 2. Rule-set ids versus engine versions

The engine that runs the rules uses **SemVer**, independently of rule-set ids.

- **Every engine release plays every released rule set exactly.** For a given rule set, seed and set of answers, any later engine release produces the same match, bit for bit. `fighter-1.0` can run on engine `1.0.0`, `1.4.2` or `2.1.0` with the same result.
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

Transitions happen at published UTC timestamps. A rule set is never announced before builders can run it locally (§6).

### 3.1 Notice period (provisional)

**A new rule set is announced at least 14 days before it goes live (provisional, D-13).** It is in `preview` for the whole notice period: from the announcement until its live date. The live date and the notice are published together with the rule set's changelog entry (§7).

---

## 4. The parameter table

`match_start.game_config` carries the rule set in two keys (see [`FIGHTER-GAME-STATE-PROTOCOL.md`](protocol/FIGHTER-GAME-STATE-PROTOCOL.md) §4.1):

| Key | Content |
|---|---|
| `rule_set_id` | The id, e.g. `"fighter-1.0"`. |
| `rule_set` | The rule set's **full parameter table**, by section: the whole rule set except its human-readable notes. |

The `fighter` sections:

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
| `fighter` | How attributes turn into hit points, stamina, stamina regeneration, movement rate and strike range. |
| `combat` | Damage scaling, guard, blocking, stamina drain, stagger, pushback, the repeated-move penalty, and the dazed rule. |
| `special_effects` | The numbers inside the specials' effects (durations, distances, multipliers). |
| `forfeit` | The disconnect grace and how many forfeited rounds forfeit the match. |
| `deadline` | The per-tick deadline, the time bank, the default action on a miss, and what happens to a late answer. |

The table for each released rule set is generated from the rule set itself and published in its changelog entry (§7), so the published numbers cannot drift from the played ones.

---

## 5. How a match pins a rule set

- **At creation.** The rule set is chosen when the match is created, and never changes during it. `game_config.rule_set_id` and `game_config.rule_set` state it to both bots at `match_start`.
- **In the record.** The replay records the rule-set id, the rule set's `sha256`, and the exact engine version.
- **Competitions pin once.** A tournament, fixture set or season pins its rule set when it is created, and every match it spawns inherits that pin. A cutover to a new live rule set therefore never splits a bracket across two rule sets.
- **A cutover does not touch matches in progress.** A match created before a rule set's live date finishes on the rule set it was created with.

---

## 6. How the local simulator selects a rule set

When the SDK's local fighter simulator ships (chipzen-ai/chipzen-sdk#141), it selects a rule set like this:

| How | Selects |
|---|---|
| `--rule-set fighter-1.0` | exactly that rule set |
| `--from-match-start <file.json>` | the `rule_set_id` of a recorded `match_start`, so a real match can be replicated locally |
| `--rule-set live` | the rule set currently `live` on the platform, asked from a public read-only endpoint |
| no flag | the **newest released** rule set the installed simulator knows. The simulator prints the id it chose. |

- **Never a silent fallback.** If the requested rule set is not in the installed simulator, it stops with an error naming the minimum version that has it. It never runs a different rule set instead.
- **`preview`** rule sets are selectable explicitly, so a bot can be tuned during the notice period.
- **`deprecated`** rule sets still run, with a warning naming the replacement and the retirement date.
- **`retired`** rule sets are refused for a new simulated match, but can still replay and verify a recorded one.

The simulator plays a rule set exactly as the platform does, so a bot tuned locally meets the same numbers after upload.

---

## 7. Changelog

One entry per rule set, newest first. Each entry gives the id, its lifecycle dates, a summary of what changed from its predecessor, and the full parameter table (§4).

### `fighter-1.0`

- **State:** not yet released. Its numbers are provisional until it is frozen, and its parameter table will be published here when it is.
- **Changes:** the first fighter rule set.

---

## 8. Rating continuity (provisional)

**Ratings continue across minor versions of a rule set, and a major version starts a new rating track (provisional, D-13).** A minor changes numbers only, so a bot's strength carries over. A major changes the mechanics, so a rating earned under the old ones says little about the new game.

Every rating event records the rule-set id it was earned under, so either policy can be applied to existing results.

---

## 9. Related documents

- [`protocol/FIGHTER-GAME-STATE-PROTOCOL.md`](protocol/FIGHTER-GAME-STATE-PROTOCOL.md): the fighter Layer 2 dialect, and where `game_config` carries the rule set (§4.1).
- [`protocol/LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md): how a Layer 2 dialect itself versions (§4).
- [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md): Layer 1, unchanged by rule sets.
