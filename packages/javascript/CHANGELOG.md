# Changelog

All notable changes to the `@chipzen-ai/bot` JavaScript / TypeScript
SDK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/javascript-v0.2.0
