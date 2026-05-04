# Porting a bot between Chipzen SDKs

The Python, JavaScript, and Rust SDKs are intentionally **shape-equivalent**:
the same hooks fire at the same protocol points, the same `decide()` contract
returns the same five action kinds, and the same `GameState` fields drive
your decisions. What differs is naming idiom — each SDK follows its host
language's convention rather than mechanically copying another's.

If you have a working bot in one language and want to port it to another,
this page is the cheat-sheet. Logic translation is the easy part; the only
mechanical work is renaming.

This doc was filed from the [cross-language consistency cleanup
audit](https://github.com/chipzen-ai/chipzen-sdk/issues/27). The audit's
recommendation was *not* to homogenize names across SDKs (each "feels right"
inside its own ecosystem), but to document the mappings so a port doesn't
require diffing source.

---

## 1. Base class / trait

| | Public name | Where it lives |
|---|---|---|
| Python | `chipzen.Bot` | also exported as `chipzen.bot.ChipzenBot` for back-compat with 0.2.0 — same class object, prefer `Bot` |
| JavaScript | `Bot` | `import { Bot } from "@chipzen-ai/bot"` |
| Rust | `Bot` (trait) | `use chipzen_bot::Bot;` |

**Implementation shape.** Python and JavaScript subclass an abstract base
class; Rust implements a trait. The user-facing surface is the same: one
required method (`decide`) and six optional lifecycle hooks.

```python
# Python
class MyBot(Bot):
    def decide(self, state: GameState) -> Action: ...
```

```typescript
// JavaScript
class MyBot extends Bot {
  decide(state: GameState): Action { ... }
}
```

```rust
// Rust
struct MyBot;
impl Bot for MyBot {
    fn decide(&mut self, state: &GameState) -> Action { ... }
}
```

---

## 2. Lifecycle hook names

Each language follows its native casing convention. The hooks fire in the
exact same order driven by the same protocol messages.

| Hook | Python / Rust | JavaScript |
|---|---|---|
| Match start | `on_match_start` | `onMatchStart` |
| Round (hand) start | `on_round_start` | `onRoundStart` |
| Round start, simplified | `on_hand_start` *(Python only — convenience wrapper)* | — |
| Phase change (flop/turn/river) | `on_phase_change` | `onPhaseChange` |
| Per-action broadcast | `on_turn_result` | `onTurnResult` |
| Round (hand) result | `on_round_result` | `onRoundResult` |
| Round result, simplified | `on_hand_result` *(Python only)* | — |
| Match end | `on_match_end` | `onMatchEnd` |

Python additionally exposes the `on_hand_start` / `on_hand_result`
convenience wrappers that do the common parsing work
(`hole_cards = [Card.from_str(c) for c in state.your_hole_cards]`) so most
bots only need to override the simpler signature. The JavaScript and Rust
SDKs do not — override the `onRoundStart` / `on_round_start` hook directly
and parse the message yourself.

---

## 3. Action construction

This is the largest idiom drift. Rust uses an enum, which makes invalid
states (e.g. `fold` with an amount) impossible at compile time. Python and
JavaScript use static factories on a class, which validate the same
invariants at construction time.

| | fold | check | call | raise to N | all-in |
|---|---|---|---|---|---|
| Python | `Action.fold()` | `Action.check()` | `Action.call()` | `Action.raise_to(N)` | `Action.all_in()` |
| JavaScript | `Action.fold()` | `Action.check()` | `Action.call()` | `Action.raiseTo(N)` | `Action.allIn()` |
| Rust | `Action::Fold` | `Action::Check` | `Action::Call` | `Action::Raise(N)` | `Action::AllIn` |

The `N` in raises is the **target total bet** (matching the wire field
name `amount`), not the raise increment over the current bet. All three
SDKs validate that `state.min_raise <= N <= state.max_raise` before sending
to the server; if the bot returns an out-of-range raise, the SDK
substitutes a safe-fallback `check` (or `fold` if check is illegal).

---

## 4. GameState field naming

The wire protocol is **always snake_case**. Python and Rust expose the
fields verbatim. JavaScript bridges to camelCase in the parser
(`parseGameState` in `models.ts`), so user code reads camelCase.

| Wire / Python / Rust | JavaScript |
|---|---|
| `hand_number` | `handNumber` |
| `to_call` | `toCall` |
| `min_raise` | `minRaise` |
| `max_raise` | `maxRaise` |
| `valid_actions` | `validActions` |
| `your_hole_cards` | `holeCards` |
| `pot` | `pot` |
| `phase` | `phase` |
| `board` | `board` |

If you log raw wire payloads (e.g. from `on_round_start(message)` in any
language), you'll always see snake_case keys regardless of which SDK you're
on — the JavaScript bridge happens at the typed-object boundary, not at
the raw `message` boundary.

---

## 5. Card construction

| | From wire string | To wire string |
|---|---|---|
| Python | `Card.from_str("Ah")` | `str(card)` |
| JavaScript | `cardFromString("Ah")` | `cardToString(card)` |
| Rust | `"Ah".parse::<Card>()?` or `parse_card("Ah")?` | `format!("{}", card)` |

Cards are always two characters: rank (`2`-`9`, `T`, `J`, `Q`, `K`, `A`)
followed by suit (`h`, `d`, `c`, `s`).

---

## 6. Async / threading model

| | Sync or async | Reentrancy |
|---|---|---|
| Python | `decide` is sync; SDK runs it on the asyncio task | Never concurrent for the same `Bot` instance |
| JavaScript | `decide` is sync; SDK awaits between turns | Never concurrent for the same `Bot` instance |
| Rust | `decide` is sync (`&mut self`); SDK owns the bot inside an async session loop | The trait is `Send + 'static` but **not** `Sync` — the session loop is the only caller, by design |

In all three: per-bot state mutations from `decide` and any lifecycle hook
are safe — the SDK guarantees serial execution. You do not need locks,
mutexes, or `asyncio.Lock` for per-bot state.

The `decide` timeout (default 5000 ms, announced in
`match_start.turn_timeout_ms`) is enforced server-side. If your bot misses
the deadline the server folds on your behalf and emits a `bot_error` event
to the human's UI. Spending nontrivial work in lifecycle hooks instead of
`decide` (e.g. precomputing equity in `on_phase_change`) is encouraged —
that work runs *between* turns and doesn't eat into your decide budget.

---

## See also

- [`DEV-MANUAL.md`](DEV-MANUAL.md) — the full developer manual for the
  Python SDK. The hook contracts described there apply 1:1 to JavaScript
  and Rust under the name mappings above.
- [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) —
  Layer 1 wire envelope; this is the same across all SDKs and is the
  authoritative source if a name in this doc ever drifts from reality.
- [`protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)
  — Layer 2 game-state payload; field names here are snake_case and match
  the Python / Rust SDK surfaces directly.
