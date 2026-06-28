# Changelog

All notable changes to the `@chipzen-ai/bot` JavaScript / TypeScript
SDK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Starter is now seat-count-aware.** The JavaScript starter
  (`packages/javascript/starters/javascript/bot.js`) exposes a
  `tablePosition()` helper and reads `yourSeat` / `dealerSeat` /
  `opponentStacks` off the parsed `GameState` instead of assuming a single
  opponent, deriving the table size as `opponentStacks.length + 1`. Heads-up
  behavior is unchanged. No protocol-version bump is needed for multi-player
  tables — `opponentStacks` has always been a list; see
  `docs/protocol/POKER-GAME-STATE-PROTOCOL.md` Section 5.9.

### Fixed

- **Lifecycle hooks are now safe-mode wrapped — a user exception in a stats
  callback no longer forfeits the match.** All bot lifecycle hook invocations
  (`onMatchStart`, `onRoundStart`, `onPhaseChange`, `onTurnResult`,
  `onRoundResult`, `onMatchEnd`, `onDecisionLatency`) now use the same guard
  as `decide()`: the error (with stack) is logged loudly to stderr and the
  WS session continues. Previously a throw tore down the session and the
  bot zombied into an auto-substitute forfeit. Under `safeMode: false` the
  error propagates as `BotDecisionError`, mirroring `decide()`.
  ([#80](https://github.com/chipzen-ai/chipzen-sdk/issues/80))

### Security

- **External-API: refuse a cross-origin or `wss`→`ws` gateway URL.**
  `runExternalBot()` now rejects a server-supplied absolute `gateway_ws_url`
  whose origin differs from the lobby's, or that downgrades to cleartext
  `ws://` — so the bot token can never be sent to a different host or
  unencrypted; the offending match is skipped. A relative URL is always
  re-anchored to the lobby origin.
  ([#58](https://github.com/chipzen-ai/chipzen-sdk/issues/58))

## [0.3.0] — 2026-06-13

Reaches feature parity with the Python SDK's external-API remote-play
path (chipzen-ai/chipzen-sdk#56), and ships the just-merged reconnect
fix from the Python side.

### Added

- **External-API remote-play surface.** `runExternalBot()` connects a bot
  to the platform over the public token-authed external-API path — lobby
  (`/ws/external/bot/{botId}`) → `matched` → per-match gateway WS
  (`/ws/external/match/{mid}/{pid}`, token in the `Sec-WebSocket-Protocol`
  header) — and plays every match dispatched to it (a single challenge, or
  each round of a tournament) on one persistent lobby connection. The match
  data plane reuses the same `Bot.decide(GameState) -> Action` loop
  (`_runSession`) as the containerized `runBot()` path, so one bot class
  works on both. ([#56](https://github.com/chipzen-ai/chipzen-sdk/issues/56))
- `connectToChipzen(botId, env)` — env-aware lobby-URL helper
  (`prod` / `staging` / `local`, honoring `$CHIPZEN_ENV`), returning a
  `ConnectionConfig`.
- `chipzen.toml` config-file convention: drop your `cz_extbot_` token (and
  optional `url` / `bot_id`) into `[external_api]` once; discovered from
  cwd → `~/.chipzen/` → `/etc/chipzen/`. Explicit kwargs always win. Parsed
  with a minimal inline reader so the package keeps its single runtime
  dependency (`ws`) — no TOML library added.
- `chipzen-sdk run-external <bot.js>` CLI: loads config, resolves the env
  URL, finds your exported `Bot` subclass, and runs it. Flags mirror the
  Python CLI: `--env`, `--token`, `--bot-id`, `--bot-class`,
  `--max-matches`, `--no-safe-mode`.
- `RetryPolicy` reconnect/backoff knobs (`maxReconnectAttempts`,
  `initialBackoffMs`, `maxBackoffMs`, `backoffMultiplier`) + the shared
  `DEFAULT_RETRY_POLICY`, accepted by both `runBot()` and
  `runExternalBot()`. Default: 5 attempts, 500 ms initial backoff doubling
  to a 30 s cap.
- `Bot.onDecisionLatency(latencyMs)` hook — called after each `turn_action`
  is sent, with the wall-clock time your `decide()` took. Default no-op.
  ([#46](https://github.com/chipzen-ai/chipzen-sdk/issues/46))
- `safeMode` option on `runBot()` / `runExternalBot()` (default `true`,
  preserving the existing fold-on-error behavior). Set `false` for
  dev/eval so an exception in `decide()` raises `BotDecisionError` and
  exits non-zero instead of being silently folded. `BotDecisionError` is
  now exported. ([#52](https://github.com/chipzen-ai/chipzen-sdk/issues/52))
- A non-default `User-Agent` (`chipzen-sdk-js/<version>`) is now sent on
  the WebSocket handshake (defense-in-depth against the platform's
  Cloudflare bot-fight rule). Override with `userAgent`.
  ([#46](https://github.com/chipzen-ai/chipzen-sdk/issues/46))
- `VERSION` export — the SDK version string, sourced from `package.json`.

### Changed

- `runBot()` now returns the `match_end` payload
  (`Record<string, unknown> | null`) instead of `void`.
  Backward-compatible — callers that ignore the return value are
  unaffected. (`_runSession` likewise returns the payload so the lobby
  loop can distinguish a clean end from a drop.)
- `runBot()`'s default reconnect cap is now 5 attempts (from the default
  `RetryPolicy`) with the policy's exponential backoff, instead of the
  previous hardcoded `maxRetries=3` with `min(2**n, 8)s`. Pass
  `maxRetries` or a `retryPolicy` to override.

### Fixed

- The default `clientVersion` sent in the `hello` handshake now tracks the
  installed package version instead of the hardcoded `"0.2.0"` literal
  that would drift on a release bump.
  ([#41](https://github.com/chipzen-ai/chipzen-sdk/issues/41))
- **External-API: a mid-match gateway disconnect no longer silently
  forfeits the match.** `runExternalBot()` reconnects a dropped per-match
  gateway socket (bounded by the `RetryPolicy`) and resumes via the
  platform's reconnect-resume — `_runSession` consumes the server
  `reconnected` frame and replays the pending turn, and the bot instance
  keeps its state across the gap. The reconnect budget is bounded, so an
  unrecoverable match is abandoned (`end: null`) rather than hanging.
- **External-API: in-flight matches survive a lobby reconnect, and no
  match task is orphaned on teardown.** Match-task ownership is hoisted to
  the top-level `runExternalBot` (not the per-lobby-session), so a lobby
  blip no longer abandons a match playing on its own gateway socket; on
  teardown, still-running matches get a short grace window then are
  drained and awaited.

### Added (already shipped pre-0.3.0, in conformance)

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
    [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28) for the
    JavaScript SDK.
- Public `SCENARIOS` export from `conformance.ts` listing each
  scenario name and runner function. Lets tests and downstream tooling
  enumerate the registered scenarios without parsing CLI output.

### Documentation

- `chipzen-sdk validate --help` now enumerates all 4 conformance
  scenarios and notes that the validator is a courtesy linter — the
  authoritative gate is server-side seccomp + cap-drop. Closes part of
  [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28).
- Documented a known limitation in `runConformanceChecks`: the
  JavaScript harness does not yet include a hard wall-clock watchdog
  against bots that synchronously block the event loop (busy-loop,
  `Atomics.wait`). The Python SDK has a daemon-thread watchdog; the
  JS equivalent (a Worker) is heavier-weight and deferred.

## [0.2.0] — Initial public release

First release of `@chipzen-ai/bot` to npm. Mirrors the Python SDK's
shape (see [`packages/python/CHANGELOG.md`](../python/CHANGELOG.md))
so a developer using either language sees the same command surface
and protocol behavior.

### Scope

The published SDK is intentionally narrow:

1. A **protocol adapter** (`Bot` base class plus the WebSocket client)
   so your bot doesn't hand-roll the wire protocol.
2. A **`chipzen-sdk validate`** CLI that runs the same pre-upload
   checks the platform performs (size, imports, sandbox-blocked
   modules, `decide()` timeout sniff, optional protocol-conformance
   harness via `--check-connectivity`).
3. An **IP-protected Dockerfile recipe** at
   [`starters/javascript/`](starters/javascript/) — multi-stage Bun
   build (`bun build --compile`) that ships a single statically-
   linked binary, not your `.js` source. See
   [`IP-PROTECTION.md`](IP-PROTECTION.md).

Local match simulation, hand evaluation, opponent pools, and
bot-vs-bot strength testing are explicitly out of scope; the platform
runs that evaluation post-upload.

### CLI surface

Two commands. Both have detailed `--help` output.

- `chipzen-sdk init <name>` — scaffold a new bot project from the
  IP-protected starter template. Emits `bot.js`, `package.json`
  (depends on `@chipzen-ai/bot`), `Dockerfile` (real
  `bun build --compile` recipe, byte-identical to the canonical
  starter), `.dockerignore`, `.gitignore`, `README.md`.
- `chipzen-sdk validate <path>` — pre-upload go/no-go. Add
  `--check-connectivity` to also drive the bot through one canned
  full-match exchange.

### Public API (re-exported from `@chipzen-ai/bot`)

- **`Bot`** — abstract base class. Override `decide(state) -> Action`.
  Optional lifecycle hooks: `onMatchStart`, `onRoundStart`,
  `onPhaseChange`, `onTurnResult`, `onRoundResult`, `onMatchEnd`.
- **`Action`** — class with `private constructor` + static factories:
  `Action.fold()`, `Action.check()`, `Action.call()`,
  `Action.raiseTo(amount)`, `Action.allIn()`. `action.toWire()`
  produces the two-layer `turn_action` params schema.
- **`Card`**, **`GameState`**, **`ActionHistoryEntry`**, **`ActionKind`**
  — types mirroring the wire schema. `parseGameState(message)` and
  `cardFromString("Ah")` bridge the snake_case wire format.
- **`runBot(url, bot, options)`** — async runner driving the full
  WebSocket lifecycle (handshake, envelope sequence check,
  ping/pong, `action_rejected` retry, reconnect with bounded
  exponential backoff, clean exit on `match_end`).
- **`runConformanceChecks(bot, options)`** — drives a bot through
  the canned full-match exchange against an in-process mock socket;
  same severity model as the Python harness. Surfaced via the CLI's
  `--check-connectivity` flag.
- **`SUPPORTED_PROTOCOL_VERSIONS`** — `["1.0"]` baseline.

### Two-layer wire protocol

The client speaks the same Chipzen two-layer protocol the Python
SDK does, defined in
[`docs/protocol/TRANSPORT-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/TRANSPORT-PROTOCOL.md)
and
[`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/POKER-GAME-STATE-PROTOCOL.md).

### Packaging

- Dual ESM + CJS via [tsup](https://tsup.egoist.dev/), with `.d.ts`
  for both module systems.
- CLI binary `chipzen-sdk` ships as `dist/bin.js` with a shebang
  prepended by the tsup banner.
- Published with **npm Trusted Publishing (sigstore-attested
  provenance)** — see [`RELEASING.md`](RELEASING.md).

### License

Apache-2.0.

[0.3.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/javascript-v0.3.0
[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/javascript-v0.2.0
