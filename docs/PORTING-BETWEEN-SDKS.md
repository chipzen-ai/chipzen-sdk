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
| 2-7 Triple Draw draw turn *(optional, never called by the SDK; call it from `decide`)* | `decide_draw` | `decideDraw` |
| Pineapple OFC placement turn *(optional, never called by the SDK; call it from `decide`)* | `decide_placement` | `decidePlacement` |

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

2-7 Triple Draw's `draw` action
([`protocol/DRAW27-GAME-STATE-PROTOCOL.md`](protocol/DRAW27-GAME-STATE-PROTOCOL.md)
§3.5) puts the discard list under `params`:

| | discard (card strings or 0-based positions) | stand pat |
|---|---|---|
| Python | `Action.discard(["Ah", "Kd"])` | `Action.stand_pat()` |
| JavaScript | `Action.discard(["Ah", "Kd"])` | `Action.standPat()` |
| Rust | `Action::discard(["Ah", "Kd"])`, or `Action::discard_positions([0, 3])` | `Action::stand_pat()` |

Pineapple OFC's `place` action
([`protocol/OFC-GAME-STATE-PROTOCOL.md`](protocol/OFC-GAME-STATE-PROTOCOL.md)
§3.5) puts the placements and the discard under `params`:

| | place (card-to-row pairs, then the discard) |
|---|---|
| Python | `Action.place([("Ks", "bottom"), ("Js", "middle")], discard="9s")` |
| JavaScript | `Action.place([["Ks", "bottom"], ["Js", "middle"]], "9s")` |
| Rust | `Action::place([("Ks", "bottom"), ("Js", "middle")], ["9s"])` |

Python and JavaScript also accept `{card, row}` objects for the pairs and a
list for the discard. Rust takes any `(card, row)` pair of printable values
(a `Card` works) or a `Placement`, and the discard as anything iterable:
`["9s"]`, `Some("9s")`, or `None::<&str>` on the opening set. Rust always
sends the discard as a list, which the server accepts for any count. An OFC
turn offers neither `check` nor `fold`, so there the safe fallback is a
legal default placement rather than a fold.

Rust's `Action` and `ActionKind` are `#[non_exhaustive]`: a `match` on
either needs a `_` arm, so a future game's action is not a breaking change.

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
| `is_draw_phase` (2-7 Triple Draw) | `isDrawPhase` |
| `draw_number` | `drawNumber` |
| `draws_remaining` | `drawsRemaining` |
| `max_discard` | `maxDiscard` |
| `your_draw_counts` | `yourDrawCounts` |
| `opponent_draw_counts` | `opponentDrawCounts` |
| `your_rows` (Pineapple OFC) | `yourRows` |
| `opponent_rows` | `opponentRows` |
| `cards_to_place` | `cardsToPlace` |
| `place` | `place` |
| `must_discard` | `mustDiscard` |
| `row_capacity` | `rowCapacity` |
| `royalties` | `royalties` |
| `opponent_royalties` | `opponentRoyalties` |
| `point_value` | `pointValue` |
| `in_fantasy_land` | `inFantasyLand` |
| `phase_sequence` | `phaseSequence` |

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

## 7. Running a bot: containerized vs external-API remote-play

There are two ways to run a bot, and all three SDKs expose both.

**Containerized / direct-match** — the platform's executor runs your
compiled bot in a container and hands it the match URL. This is the upload
path.

| | Entry point |
|---|---|
| Python | `run_bot(url, bot, ...)` |
| JavaScript | `runBot(url, bot, ...)` |
| Rust | `run_bot(url, bot, options)` |

**External-API remote-play** — you run the bot on your own machine with a
long-lived `cz_extbot_` token; the platform matches and dispatches you
exactly like any other competitor (lobby → `matched` → per-match gateway
WS). One persistent lobby connection serves a whole tournament.

| | Entry point | Config / env helper | CLI |
|---|---|---|---|
| Python | `run_external_bot(bot, bot_id=, env=, token=, ...)` | `connect_to_chipzen(bot_id, env)` | `chipzen run-external my_bot.py` |
| Rust | `run_external_bot(factory, RunExternalOptions { .. })` | `connect_to_chipzen(bot_id, env, ..)` | `chipzen_bot::run_external_cli(factory, args)` — a library helper; the scaffolded starter's `bot run-external` mode calls it |

Shared semantics across both languages: a `chipzen.toml` (`[external_api]`
`token` / `url` / `bot_id`) discovered at cwd → `~/.chipzen/` →
`/etc/chipzen/` (explicit args win); a `RetryPolicy` (defaults 5 attempts /
500 ms initial / 30 s cap / 2.0 multiplier) pacing both lobby and mid-match
gateway reconnects; `safe_mode` (default on) folding a `decide()` failure,
off making it terminal; the token carried in the `Sec-WebSocket-Protocol`
header (sentinel `chipzen-bot-token`), never the query string; and a
non-default `User-Agent` (`chipzen-sdk-<lang>/<version>`).

**The one real difference is the CLI.** Python's `chipzen run-external
my_bot.py` dynamically imports a bot from a `.py` file. Rust can't — a Rust
bot is compiled into its own binary, and there's no safe equivalent of
`dlopen` for Rust types (the same reason `chipzen-sdk validate` has no
`--check-connectivity` flag and the conformance harness ships as a library
function). So the Rust equivalent is `chipzen_bot::run_external_cli(factory,
args)`, which you wire into your own bot binary's `main` the same way you
wire `run_bot` — the scaffolded starter ships a `run-external` mode that
does this for you. The standalone `chipzen-sdk run-external` subcommand is a
config doctor: it resolves + prints the connection (token redacted) so you
can verify your `chipzen.toml` / env setup before running.

In Rust, `run_external_bot` takes a **bot factory** (`Fn() -> impl Bot`)
rather than a single bot instance, because each match runs in its own task
and may need its own per-match state — pass `|| MyBot::default()`. Python
accepts either an instance (reused) or a callable / the class (fresh per
match).

## 8. Declaring the games a bot plays

A bot that plays more than No-Limit Hold'em declares its games on the
connection: a list of `game_type` strings that goes on the client `hello` as
`supported_games` ([`protocol/LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md)
section 2). Omitting it declares nothing, which the platform reads as
"poker only", and leaves the `hello` unchanged. See
[`DEV-MANUAL.md` §2.8](DEV-MANUAL.md#28-playing-other-games).

| | Containerized | External-API |
|---|---|---|
| Python | `run_bot(url, bot, supported_games=["poker", "draw27"])` | `run_external_bot(bot, ..., supported_games=[...])` |
| JavaScript | `runBot(url, bot, { supportedGames: [...] })` | `runExternalBot(bot, { ..., supportedGames: [...] })` |
| Rust | `run_bot(url, bot, RunBotOptions { supported_games: Some(vec!["poker".into(), "draw27".into()]), .. })` | `RunExternalOptions { supported_games: Some(..), .. }`, or the same field on `RunExternalArgs` for `run_external_cli` |

In Python and JavaScript a Big Bad Bots base class (`BigBadBotsBot`)
declares `["bigbadbots"]` when you pass nothing, and an explicit list
replaces it. The Rust SDK has no such base class yet. It plays hold'em,
2-7 Triple Draw and Pineapple OFC tables (see the support table in
DEV-MANUAL §2.8).

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
