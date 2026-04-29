# Changelog

All notable changes to the `chipzen-bot` Python SDK will be documented
in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- **`chipzen.Bot`** — abstract base class (alias for
  `chipzen.bot.ChipzenBot`). Override `decide(state) -> action`.
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
