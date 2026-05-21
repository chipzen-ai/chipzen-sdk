# Changelog

All notable changes to the `chipzen-bot` Python SDK will be documented
in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `chipzen.connect_to_chipzen(bot_id, env=...)` — env-aware lobby URL
  helper. Returns a `ConnectionConfig` carrying the resolved WebSocket
  URL, optional `chipzen.toml` token, and a default `RetryPolicy`,
  ready to hand off to `run_bot()`. Maps `env="prod"` → `wss://chipzen.ai/...`,
  `env="staging"` → `wss://staging.chipzen.ai/...`, `env="local"` →
  `ws://localhost:8001/...`, all under the external-API lobby path
  `/ws/external/bot/{bot_id}`. Honors the `CHIPZEN_ENV` environment
  variable when no explicit `env=` is passed; an explicit argument
  always wins over the env var. A `[external_api].url` from a
  discovered `chipzen.toml` takes precedence over both — matching the
  precedence pattern established by the `chipzen.toml` token in #42.
  Public surface: `chipzen.connect_to_chipzen`, `chipzen.ConnectionConfig`.
  Closes [#43](https://github.com/chipzen-ai/chipzen-sdk/issues/43).
- `chipzen.toml` config-file token convention. Long-lived external-API
  tokens (and an optional URL override) can now be loaded from
  `./chipzen.toml`, `~/.chipzen/chipzen.toml`, or
  `/etc/chipzen/chipzen.toml` (POSIX only) instead of being hard-coded
  into source. `run_bot()` picks up the discovered values when no
  explicit `token=` / `url=` kwarg is passed; explicit values always
  win. Public surface: `chipzen.ChipzenConfig`, `chipzen.ChipzenConfigError`,
  `chipzen.load_chipzen_config()`. On Python 3.10 the optional `tomli`
  dependency is pulled in automatically. Closes
  [#42](https://github.com/chipzen-ai/chipzen-sdk/issues/42).
- `chipzen.RetryPolicy` — configurable backoff knobs for the WebSocket
  client's reconnect loop. `run_bot()` now accepts a `retry_policy=`
  keyword argument; defaults (5 attempts, 500ms initial backoff, ×2
  multiplier, 30s cap) match the External-API Issue 26 spec and apply
  to both connection-drop and heartbeat-miss reconnect attempts within
  the server's 30s grace window. The legacy `max_retries=` keyword
  still works as a convenience override for the attempt cap; new code
  should pass a full `RetryPolicy` instead. Closes
  [#45](https://github.com/chipzen-ai/chipzen-sdk/issues/45).
- `Bot.on_decision_latency(latency_ms)` lifecycle hook. Fires after
  every `turn_action` is sent with the wall-clock milliseconds elapsed
  since the corresponding `turn_request` arrived. Override to log
  per-turn decision times locally and catch tail latency before it
  produces server-side `auto_action` substitutes. Default is a no-op,
  so existing bots are unaffected. (#46)
- Non-default `User-Agent` header on the WebSocket handshake:
  `chipzen-sdk-python/<version>`. Defense-in-depth alongside the
  platform-side Cloudflare path allowlist so the default
  `websockets` library UA doesn't trip the bot-fight rule on the
  `chipzen.ai` zone. (#46)

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

[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/python-v0.2.0
