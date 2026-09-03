# Changelog

All notable changes to the `chipzen-bot` Python SDK will be documented
in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-09-03

Minor release: the poker-variants surface (2-7 Triple Draw + Pineapple OFC)
and `supported_games`. Fully additive — an NLHE bot built on 0.3.x runs
unchanged, and its client `hello` is byte-identical.

### Added

- **`supported_games` — declare the games your bot can actually play.**
  `run_bot(..., supported_games=[...])` and
  `run_external_bot(..., supported_games=[...])` put the list on the Layer 1
  client `hello` under `supported_games`, the field the platform reads to
  decide whether a client may be seated at a non-poker table
  (`docs/protocol/LAYER2-COMMON.md` section 2). **Default `None` omits the
  field entirely**, and the platform reads an absent declaration as "poker
  only" — the client `hello` a bot sends today is byte-identical, frozen by
  `tests/test_supported_games.py`. Declaring a `game_type` asserts you
  implement everything in that game's Layer 2 document; a client that declares
  a game it cannot act in is seated and then timeout-substituted every turn.

- **Variant scaffolding for 2-7 Triple Draw and Pineapple OFC.** Additive and
  unpublished: both games are registered and **dark**, no dispatch path creates
  a match at either table today, and none of this changes what an NLHE bot
  sees. See `docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md` and
  `docs/protocol/OFC-GAME-STATE-PROTOCOL.md` for the wire contract.
  - `GameState` gained the variant keys from the two Layer 2 specs —
    `is_draw_phase`, `draw_number`, `draws_remaining`, `max_discard`,
    `your_draw_counts`, `opponent_draw_counts` (27TD); `your_rows`,
    `opponent_rows`, `cards_to_place`, `place`, `must_discard`,
    `row_capacity`, `royalties`, `opponent_royalties`, `point_value`,
    `in_fantasy_land`, `phase_sequence` (OFC). **Every one is optional with a
    default**, so a bot that never reads them behaves exactly as before.
  - `Action.discard()` / `Action.stand_pat()` (27TD `draw`) and
    `Action.place()` (OFC `place`). Their parameters travel under `params` —
    the only top-level key the server's field allowlist accepts for them.
  - `Bot.decide_draw()` / `Bot.decide_placement()` — **optional, defaulted**
    convenience hooks the SDK never calls on its own. `decide()` is still the
    single required entry point.
  - Conformance fixtures for both variants (fixtures only; deliberately not
    wired into `run_conformance_checks`, which grades NLHE bots).
- **`tests/test_variant_backcompat.py`**, sibling of
  `test_multiway_backcompat.py`. Freezes that a variant `turn_request` parses
  in an NLHE-shaped bot without raising and without displacing any existing
  field's default — and proves the hard constraint the whole design rests on:
  an invalid card in `board` or `your_hole_cards` raises **before `decide()`
  is called**, driven through the real session loop.

### Changed

- **`Action` carries a `params` dict.** Appended after `amount` with a default,
  so positional construction and every NLHE `to_wire()` payload are unchanged;
  `Action` stays hashable.
- **`GameState.from_turn_request` reads `your_seat` / `dealer_seat` from
  `state` when present**, falling back to the keyword arguments. OFC carries
  both in `turn_request.state`; NLHE and 27TD do not.
- **The Python starter notices an action vocabulary it does not implement**
  rather than guessing at it — it reports the unfamiliar names once and picks
  the safest action actually on offer. It remains an NLHE bot.

## [0.3.3] — 2026-08-16

Patch release: the re-attach fix (#119). A session that (re)joins an in-flight
match now learns its match context from the `reconnected` frame instead of
silently keeping stale defaults.

### Added

- **`Bot.on_reconnected(message)` lifecycle hook.** Fired when the server
  resumes an in-flight match with a `reconnected` frame (which is what a
  re-attach delivers instead of `match_start`), so a bot can (re)learn its
  match context — `match_id`, `seats`, `game_config` — before the replayed
  pending turn is decided. Default is a no-op; existing bots are unaffected.
  ([#119](https://github.com/chipzen-ai/chipzen-sdk/issues/119))

### Fixed

- **`your_seat` is re-learned from `reconnected.seats`.** Previously the seat
  was only captured from `match_start`, so a session that (re)attached to an
  in-flight match — or resumed after a mid-match gateway drop — built every
  subsequent `GameState` with `your_seat=0` regardless of the bot's actual
  seat. ([#119](https://github.com/chipzen-ai/chipzen-sdk/issues/119))

### Changed

- **Starter template tracks SDK releases automatically.** The Python starter's
  `requirements.txt` pinned `chipzen-bot==0.2.0` (four releases behind), so
  every image built from it shipped a stale SDK. It now uses the
  compatible-range pin `chipzen-bot~=0.3`, which follows minor/patch releases
  and only needs touching on a major bump.
  ([#116](https://github.com/chipzen-ai/chipzen-sdk/issues/116))

## [0.3.2] — 2026-07-19

### Fixed

- **External-API: a slow `decide()` no longer starves the session keepalive /
  drops the lobby (and no longer cascades to sibling matches under
  concurrency).** `_run_session` previously invoked `bot.decide()` synchronously
  on the single shared session event loop, so any decision outstanding past the
  ~20s WebSocket keepalive interval (well inside the 30s casual clock) blocked
  the loop, starved the lobby heartbeat, and dropped the lobby server-side; under
  concurrency one slow decision blocked every co-scheduled match. `decide()` now
  runs off-loop via `asyncio.to_thread`, so the loop keeps servicing keepalives
  and other matches while a decision is outstanding, up to the real decision
  clock. Push→pull bridge semantics and the fallback margin are unchanged. This
  chiefly affects LLM-backed agents (slow think times) playing via the MCP
  server. ([#3904](https://github.com/chipzen-ai/Chipzen/issues/3904))

### Changed

- **Starter is now seat-count-aware.** The Python starter
  (`packages/python/starters/python/bot.py`) exposes a `table_position()`
  helper, derives the table size from `len(opponent_stacks) + 1`, and reads
  `your_seat` / `dealer_seat` off the parsed `GameState` instead of assuming a
  single opponent. Its heads-up behavior is unchanged; this is a reference for
  authors extending a bot to 3-6 player tables.

### Notes (multi-player forward-compat)

- **No breaking change and no protocol-version bump for multi-player tables.**
  The two-layer protocol was multiway-shaped from the start: `opponent_stacks`
  is a `list[int]`, and the seat fields (`your_seat`, `dealer_seat`,
  `winner_seats`) are already seat-indexed. An existing heads-up bot keeps
  running unchanged when seated at a 3-6 player table; the only `game_config`
  addition is `num_players` (the seat count N), which old bots can ignore.
- **Migration note / silent-failure risk.** A bot that hardcodes
  `opponent_stacks[0]` does NOT crash at a larger table, but reads a single
  neighbor's stack rather than the whole field. If you meant "the opponents",
  iterate or aggregate the list, or gate heads-up-only logic on
  `len(opponent_stacks) == 1`. See `docs/DEV-MANUAL.md` Section 2.3 and
  `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` Section 5.9 for position
  derivation, and `tests/test_multiway_backcompat.py` for the proof that
  heads-up bots keep working.

## [0.3.1] — 2026-07-14

### Fixed

- **Lifecycle hooks are now safe-mode wrapped — a user exception in a stats
  callback no longer forfeits the match.** All bot lifecycle hook invocations
  (`on_match_start`, `on_round_start`, `on_turn_result`, `on_phase_change`,
  `on_round_result`, `on_match_end`, `on_decision_latency`) now use the same
  guard as `decide()`: the full traceback is logged at ERROR and the WS
  session continues. Previously a raise tore down the session, the reconnect
  loop logged only "Connection lost, retrying" (hiding the real traceback),
  and the bot zombied into an auto-substitute forfeit. Under
  `safe_mode=False` the exception propagates as `BotDecisionError`,
  mirroring `decide()`.
  ([#80](https://github.com/chipzen-ai/chipzen-sdk/issues/80))
- **External-API: a mid-match gateway disconnect no longer silently forfeits
  the match.** `run_external_bot()` now reconnects a dropped per-match gateway
  socket (bounded by the `RetryPolicy`) and resumes via the platform's
  reconnect-resume — `_run_session` already consumes the server `reconnected`
  frame and replays the pending turn, and the bot instance keeps its state.
  Previously the gateway was opened once with no retry, so a transient drop
  ended the match with `end=None` while the lobby stayed up (the bot looked
  healthy but had forfeited).
- **External-API: in-flight matches survive a lobby reconnect, and no match
  task is orphaned on teardown.** Match-task ownership moved out of the
  per-lobby-session into `run_external_bot`, so a lobby blip no longer abandons
  a match playing on its own gateway socket; on teardown, still-running matches
  get a short grace window then are cancelled and awaited (previously they were
  left running after a 30s `asyncio.wait` timeout).
- **Packaging: ship the `py.typed` marker.** The `Typing :: Typed` classifier
  promised an inline-typed package, but the marker file was missing, so
  downstream `mypy` ignored the SDK's type hints. The marker now ships in the
  wheel (verified by a new CI wheel smoke-test).

### Security

- **External-API: refuse a cross-origin or `wss`→`ws` gateway URL.**
  `run_external_bot()` now rejects a server-supplied absolute `gateway_ws_url`
  whose origin differs from the lobby's, or that downgrades the connection to
  cleartext `ws://` — so the long-lived bot token can never be sent to a
  different host or unencrypted. The offending match is skipped rather than
  connected. A relative `gateway_ws_url` is always re-anchored to the lobby
  origin, so the normal path is unaffected.
  ([#58](https://github.com/chipzen-ai/chipzen-sdk/issues/58))

## [0.3.0] — 2026-06-13

### Added

- **External-API remote-play surface.** `run_external_bot()` connects a bot to
  the platform over the public token-authed external-API path — lobby
  (`/ws/external/bot/{bot_id}`) → `matched` → per-match gateway WS
  (`/ws/external/match/{mid}/{pid}`, token in the `Sec-WebSocket-Protocol`
  header) — and plays every match dispatched to it (a single challenge, or each
  round of a tournament) on one persistent lobby connection. The match data
  plane reuses the same `Bot.decide(GameState) -> Action` loop as the
  containerized path, so one bot class works on both. Promotes the previously
  copy-paste `examples/external-api-bot` reference into the published package.
  ([#43](https://github.com/chipzen-ai/chipzen-sdk/issues/43))
- `connect_to_chipzen(bot_id, env=...)` — env-aware lobby-URL helper
  (`prod` / `staging` / `local`, honoring `$CHIPZEN_ENV`).
  ([#43](https://github.com/chipzen-ai/chipzen-sdk/issues/43))
- `chipzen.toml` config-file convention: drop your `cz_extbot_` token (and
  optional `url` / `bot_id`) into `[external_api]` once; discovered from cwd →
  `~/.chipzen/` → `/etc/chipzen/`. Explicit kwargs always win.
  ([#42](https://github.com/chipzen-ai/chipzen-sdk/issues/42))
- `chipzen run-external <bot.py>` CLI (also `chipzen-sdk run-external`): loads
  config, resolves the env URL, finds your `Bot` subclass, and runs it.
  Flags: `--env`, `--token`, `--bot-id`, `--bot-class`, `--max-matches`,
  `--no-safe-mode`. ([#44](https://github.com/chipzen-ai/chipzen-sdk/issues/44))
- `RetryPolicy` reconnect/backoff knobs (`max_reconnect_attempts`,
  `initial_backoff_ms`, `max_backoff_ms`, `backoff_multiplier`), accepted by
  both `run_bot()` and `run_external_bot()`. Default: 5 attempts, 500 ms
  initial backoff doubling to a 30 s cap.
  ([#45](https://github.com/chipzen-ai/chipzen-sdk/issues/45))
- `Bot.on_decision_latency(latency_ms)` hook — called after each `turn_action`
  is sent, with the wall-clock time your `decide()` took. Default no-op.
  ([#46](https://github.com/chipzen-ai/chipzen-sdk/issues/46))
- `safe_mode` parameter on `run_bot()` / `run_external_bot()` (default `True`,
  preserving the existing fold-on-error behavior). Set `False` for dev/eval so
  an exception in `decide()` raises `BotDecisionError` and exits non-zero
  instead of being silently folded.
  ([#52](https://github.com/chipzen-ai/chipzen-sdk/issues/52))
- A non-default `User-Agent` (`chipzen-sdk-python/<version>`) is now sent on the
  WebSocket handshake (defense-in-depth against the platform's Cloudflare
  bot-fight rule). Override with `user_agent=`.
  ([#46](https://github.com/chipzen-ai/chipzen-sdk/issues/46))

### Changed

- `run_bot()` now returns the `match_end` payload (`dict | None`) instead of
  `None`. Backward-compatible — callers that ignore the return value are
  unaffected.
- `run_bot()`'s default reconnect cap is now 5 attempts (from the default
  `RetryPolicy`) instead of the previous hardcoded `max_retries=3`. Pass
  `max_retries=` or a `retry_policy=` to override.

### Fixed

- The default `client_version` sent in the `hello` handshake now tracks the
  installed package version instead of a hardcoded string that drifted (it
  reported `0.2.0` in the `0.2.1` wheel).
  ([#41](https://github.com/chipzen-ai/chipzen-sdk/issues/41))

## [0.2.1] — 2026-05-05

### Fixed

- `action_rejected` retry now uses the `valid_actions` field from the
  rejection payload (Chipzen v0.3.53+) when present, instead of always
  guessing `["check", "fold"]`. The legacy blind retry caused a
  consecutive-rejection loop in matches where neither `check` nor
  `fold` was legal at the rejected decision point: bot sends `call`
  (rejected because legal=`[check, raise]`), client retries blindly
  with `check` (also rejected because legal=`[fold, call, raise]` next
  street), each rejection counts toward the server's
  `BOT_UNRESPONSIVE_AUTO_SUBSTITUTE_LIMIT` streak, eventually killing
  the match. Pre-v0.3.53 servers omit `valid_actions` and the client
  falls back to the legacy behavior — older bots remain compatible.

### Added

- Three new conformance scenarios in `validate --check-connectivity`,
  bringing the total from 1 to 4. The previously-shipped scenario only
  covered a clean handshake + 1 hand + match_end; bots could pass it
  and still crash in production. The new scenarios are:
  - `multi_turn_request_id_echo` — drives 3 `turn_request`s across
    preflop/flop/turn and verifies the SDK echoes each `request_id`
    correctly (the previous harness only checked the first action).
  - `action_rejected_recovery` — verifies the SDK retries with a
    safe-fallback `check`/`fold` and the original `request_id` when the
    server sends `action_rejected` (a routine production code path
    that had no harness coverage).
  - `retry_storm_bounded` — verifies the SDK responds reactively to 3
    back-to-back `action_rejected` messages without hanging or entering
    an unbounded send loop.
  - Closes part of
    [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28).
- A hard wall-clock watchdog on each conformance scenario. A bot whose
  `decide()` busy-loops or blocks the asyncio event loop synchronously
  used to hang the harness silently (the inner `asyncio.wait_for` could
  not fire because the event loop was starved). The new watchdog runs
  each scenario in a daemon thread and returns a `Fail` `ConformanceCheck`
  if the wall clock exceeds `timeout_s + 5s`.

### Documentation

- Clarified that `chipzen.Bot` is the canonical public name for the bot
  base class. `chipzen.bot.ChipzenBot` continues to refer to the same
  class object and is kept for backward compatibility with 0.2.0
  imports, but new code should always use `chipzen.Bot`. Added a
  pointer in `DEV-MANUAL.md` and a clarifying docstring on the
  `ChipzenBot` class itself.
- Added [`docs/PORTING-BETWEEN-SDKS.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/PORTING-BETWEEN-SDKS.md):
  cross-language cheat-sheet for Python ↔ JavaScript ↔ Rust covering
  base class, lifecycle hook names, action construction idiom,
  `GameState` field naming, card construction, and async/threading
  model. Closes [#27](https://github.com/chipzen-ai/chipzen-sdk/issues/27).
- Added [`SECURITY.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/SECURITY.md)
  section "Strategy leakage via crash output" — clarifies that
  exception tracebacks (Python), panic locations (Rust), and stack
  traces (JavaScript) include user function names which may be
  captured in platform match logs. Sets the expectation that function
  names should be treated as observable for accidental-disclosure
  purposes. Closes part of
  [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28).
- Updated `chipzen-sdk validate --help` to enumerate all 4 conformance
  scenarios and to note that the validator is a courtesy linter — the
  authoritative gate is the platform's seccomp + cap-drop sandbox.
  Closes part of
  [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28).

## [0.2.0] — Initial public release

First release of `chipzen-bot` to PyPI. The package was previously
developed inside the Chipzen platform repo and is now extracted to
[chipzen-ai/chipzen-sdk](https://github.com/chipzen-ai/chipzen-sdk)
as the canonical home.

### Scope

The published SDK is intentionally narrow:

1. A **protocol adapter** (`chipzen.Bot` base class plus the WebSocket
   client) so your bot doesn't hand-roll the wire protocol.
2. A **`chipzen-sdk validate`** CLI that runs the same pre-upload
   checks the platform performs (size, imports, sandbox-blocked
   modules, `decide()` timeout sniff).
3. (Forthcoming, in a follow-up release) An **IP-protected Dockerfile
   recipe** — Cython multi-stage build that ships compiled `.so`
   artifacts only, not your `.py` source.

Local match simulation, hand evaluation, opponent pools, and
bot-vs-bot strength testing are explicitly out of scope; the platform
runs that evaluation post-upload. See the
[README](README.md#what-the-sdk-is-for-and-what-it-isnt).

### CLI surface

Two commands. Both have detailed `--help` output.

- `chipzen-sdk init <name>` — scaffold a new bot project from a
  starter template.
- `chipzen-sdk validate <path>` — pre-upload go/no-go.

### Public API

- **`chipzen.Bot`** — abstract base class (also exported as
  `chipzen.bot.ChipzenBot` — same class object, prefer `Bot`). Override
  `decide(state) -> action`.
  Optional lifecycle hooks: `on_match_start`, `on_round_start`,
  `on_hand_start`, `on_phase_change`, `on_turn_result`,
  `on_round_result`, `on_hand_result`, `on_match_end`.
- **`chipzen.GameState`** — dataclass built from the server's
  `turn_request` payload. Fields documented in the
  [DEV-MANUAL §2.3](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#23-gamestate).
- **`chipzen.Action`** — factory: `Action.fold()`, `Action.check()`,
  `Action.call()`, `Action.raise_to(amount)`, `Action.all_in()`.
  `Action.to_wire()` produces the two-layer `turn_action` params
  schema.
- **`chipzen.Card`** — `(rank, suit)` frozen dataclass.
  `Card.from_str("Ah")` parses wire format; `str(card)` renders it.
- **`chipzen.client.run_bot(...)`** — async runner that drives the
  full WebSocket lifecycle (handshake, envelope sequence check,
  ping/pong, `action_rejected` retry, reconnect, clean exit on
  `match_end`).

### Built-in example bots

Importable as canonical `Bot` subclass examples (not as competitive
opponents — there is no local match runner):

- `chipzen.examples.call_bot.CallBot` — always calls.
- `chipzen.examples.random_bot.RandomBot` — picks a uniform random
  valid action.
- `chipzen.examples.tight_aggressive.TightAggressiveBot` — simplified
  TAG strategy.

### Two-layer wire protocol

The client speaks the Chipzen two-layer protocol (Layer 1 Transport +
Layer 2 Poker) defined in
[`docs/protocol/TRANSPORT-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/TRANSPORT-PROTOCOL.md)
and
[`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/POKER-GAME-STATE-PROTOCOL.md).

Highlights for clients written in other languages or for anyone
debugging at the wire level:

- The `run_bot` handshake sends `authenticate` first, waits for the
  server `hello`, then sends the client `hello` with
  `supported_versions=["1.0"]`.
- Heartbeat: client replies to `ping` with `pong`.
- `action_rejected`: SDK falls back to `check` (or `fold` if check is
  not legal) using the original `request_id`.
- `reconnected` messages with embedded `pending_request` are
  dispatched as if they were fresh `turn_requests`.

### License

Apache-2.0 (changed from MIT in earlier internal builds — aligns with
the chipzen-sdk repo's root LICENSE).

[0.4.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/python-v0.4.0
[0.3.3]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/python-v0.3.3
[0.3.2]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/python-v0.3.2
[0.3.1]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/python-v0.3.1
[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/python-v0.2.0
