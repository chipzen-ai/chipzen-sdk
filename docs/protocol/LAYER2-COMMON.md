# Layer 2 Common Baseline

**Date:** 2026-08-24
**Status:** Normative
**Version:** 1.0 (Layer 1 protocol version is **unchanged at `1.0`**)

---

## Overview

Chipzen speaks a two-layer protocol. Layer 1 — [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md) — handles connection, authentication, turn-taking, timing and error delivery, and carries a game-specific payload opaquely. Layer 2 defines what is inside that payload for one game.

Each game gets its own Layer 2 document: [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) for NLHE, [`DRAW27-GAME-STATE-PROTOCOL.md`](DRAW27-GAME-STATE-PROTOCOL.md) for 2-7 Triple Draw, [`OFC-GAME-STATE-PROTOCOL.md`](OFC-GAME-STATE-PROTOCOL.md) for Pineapple OFC, and one per variant added after them.

**This document states the part that is the same in all of them**, so a variant document carries only what is true of *that* variant. It is normative: a variant document inherits everything here unless it says otherwise, and where it restates a value the value is a delta, never a re-derivation.

Why it exists: §1, the handshake prose, the five backward-compatibility rules and the versioning policy were copy-pasted verbatim into the first two variant documents, and one of the copies had already lost a paragraph by the time anyone diffed them (`chipzen-ai/Chipzen#4242`). Every future variant document would have copied the block again. One statement cannot drift from itself.

**Scope.** [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) predates this document and is not reworded by it. It is the NLHE baseline the rules below are written *against*: "no existing NLHE key changes type or meaning" means the keys that document defines.

---

## 1. Layer 1 is untouched

**Normative.** A Layer 2 dialect introduces **no** Layer 1 change and **no** protocol version bump.

- The handshake still negotiates `supported_versions: ["1.0"]`, and `"1.0"` is still the only supported version. A client that speaks Layer 1 v1.0 today speaks it at any table.
- Every Layer 1 message type, envelope field, sequencing rule, timeout rule and error code is exactly as specified in [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md).
- Layer 1 carries a dialect's payloads opaquely, in the same fields it carries NLHE payloads: `match_start.game_config`, `round_start.state`, `turn_request.state`, `turn_action.params`, `turn_result.details`, `phase_change.state`, `round_result.result`.

[`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) §7 already legislates exactly this: *"New variants … new `variant` value in `game_config`, separate Layer 2 specification document."* There is nothing for a Layer 1 implementation to change.

---

## 2. How a client learns which game it is at

A non-poker table advertises itself. The server `hello` carries an additive `game` descriptor — **absent means poker**, so the NLHE `hello` envelope is byte-identical to what ships today. Each variant document states the descriptor its tables send; the keys are always `game_type`, `variant`, `actions`, `phases` and `state_shape`.

A client declares what it can play with the optional handshake field `supported_games` — a list of `game_type` strings. **Absent means "poker only."** At a non-poker table a client that has not declared the seat's game is rejected **before it is seated**, rather than being seated and auto-substituted to zero. The rejection arrives as two frames: an `error` envelope carrying `code: "EXTAPI_CLIENT_GAME_UNSUPPORTED"` and a message naming the game, then a WebSocket close with code **`4002`** and reason `game_unsupported_by_client`. (`409` is that code's HTTP status in the platform error table — it is not a handshake status, and nothing on this socket ever carries it.) Declaring a `game_type` is an assertion that the client implements everything in that game's Layer 2 document.

**Where this contract is owned.** The mechanism belongs to the platform's `docs/API-REFERENCE.md`, section "Declaring client support", and is implemented in `services/extapi_game_capability.py` and `bot_ws.py`. That file is platform-repo-only, so this section — which ships in both repositories — is the public statement of it; the paragraph above is the whole contract, not a summary of a longer one. **A variant document states its `game` descriptor and nothing else about the handshake.** Two variant documents restating this paragraph is what `chipzen-ai/Chipzen#4242` had to correct in both copies at once.

`state_shape` is the marker a client MUST branch on before parsing state payloads. A client that hardcodes the NLHE field list MUST treat an unfamiliar `state_shape` as "I cannot parse this table" — never as "drop the keys I do not recognise".

---

## 3. Backward-compatibility rules

**Normative, and binding on every Layer 2 dialect and every future revision of one.** These rules exist because the deployed SDK parsers are strict, and they fail *before* a bot's `decide()` ever runs.

A variant document restates each rule as a one-line **delta** — the value that rule takes for that game — and nothing else. The rationale lives here.

### Rule 1 — `board` and `your_hole_cards` are arrays of strictly-valid cards

`board` and `your_hole_cards` MUST always be arrays whose every element is a valid two-character card string (`"Ah"`, `"Td"`, `"2c"` — see [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) §1 for the notation, which every dialect shares verbatim).

The Python SDK's `Card.from_str` and the JavaScript SDK's `cardFromString` **throw inside `parseGameState`**, before `decide()` is called and outside any per-decision safe mode. A `"??"`, `"XX"` or `null` placeholder in either array is therefore a hard session kill for every deployed Python and JavaScript bot. The Rust SDK does not throw — it silently `filter_map`s unparseable entries, which is worse in a different way: a hand quietly shrinks instead of failing loudly.

**Hiding information is done by emptying an array, never by putting a fake card in it.**

### Rule 2 — the six numeric fields stay present and numeric

`pot`, `to_call`, `min_raise`, `max_raise`, `your_stack` and `opponent_stacks` MUST be present with numeric values in every `turn_request.state`, even where they are meaningless for the game or for the phase in progress. They are never omitted and never `null`. A dialect states which of them are pinned to `0`, and when.

### Rule 3 — `phase` stays a free string

`phase` is a free-form string. The JavaScript SDK types it as a compile-time-only union and casts at runtime, so an unfamiliar phase string does not throw — but a client MUST NOT assume the value is one of the five NLHE phases. The union is widened in the next SDK release; the wire contract is "any string".

### Rule 4 — all new action parameters nest under `params`

Every dialect-specific action parameter travels under `turn_action.params`. **A dialect adds no top-level field**, and a bespoke one is not part of the `turn_action` envelope: what happens to it is Layer 1's business, specified by [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md) §9.2 (the `turn_action` schema, `additionalProperties: false`) and §16.2 (unknown fields in bot messages). Neither this document nor a variant document restates that list — a spec that copies it drifts from it.

### Rule 5 — new keys only

Everything a dialect adds is carried in **new keys**. No existing NLHE key changes type or meaning. A parser that ignores unknown keys degrades to a diminished but survivable reading of the table; a parser that trips over a changed key does not survive at all. **Additions to a variant document are new keys; removals and retypings are a major version of that document.**

---

## 4. Versioning a Layer 2 dialect

- **Layer 1 stays at `1.0`.** Nothing in a variant document is a reason to bump it, now or on a future revision of that document. Layer 2 dialects version independently of Layer 1.
- **Additive changes** (new keys in a state payload, new optional `params` keys): no version bump. Rule 5 of §3 requires them to be additive.
- **Breaking changes** (a key removed or retyped, a phase string renamed, a semantic change to an existing field): a major bump of *that* document, and a new `variant` value if the two dialects must coexist.
- **A new variant** is a new `variant` value with its own document, not a revision of an existing one.
- A dialect's **hand-record payload versions separately**, via its own `schema_version`, because it is persisted: a shape change on already-written rows is a migration, not an edit.

---

## 5. Related documents

- [`TRANSPORT-PROTOCOL.md`](TRANSPORT-PROTOCOL.md) — Layer 1. Unchanged by any Layer 2 dialect.
- [`POKER-GAME-STATE-PROTOCOL.md`](POKER-GAME-STATE-PROTOCOL.md) — Layer 2 for NLHE, and the baseline §3 is written against. Card notation lives in its §1.
- [`DRAW27-GAME-STATE-PROTOCOL.md`](DRAW27-GAME-STATE-PROTOCOL.md) — Layer 2 for 2-7 Triple Draw.
- [`OFC-GAME-STATE-PROTOCOL.md`](OFC-GAME-STATE-PROTOCOL.md) — Layer 2 for Pineapple OFC.
- `docs/API-REFERENCE.md` (platform repository) — owns `supported_games` and the unsupported-game rejection restated in §2. Not a link: that file has no public copy.

This document and its mirror are drift-guarded: a normalized digest is committed beside each copy and pinned in both repositories, so a one-sided edit turns that side red.
