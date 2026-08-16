# Changelog

All notable changes to the `chipzen-mcp` MCP server will be documented in
this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] — 2026-08-16

Patch release: the re-attach turn-routing fix (#119), released in lockstep
with `chipzen-bot` 0.3.3, which carries the SDK half of the fix.

### Fixed

- **Re-attached matches no longer publish turns under an empty `match_id`.**
  On re-attach the server sends `reconnected`, not `match_start`, so
  `BridgeBot` never learned which match it was playing: every post-restart
  turn landed on a single `""` record, the real match records froze, and with
  multiple concurrent matches turn routing was corrupted (`get_match_state`
  returned `turn: null`, `get_last_result` said `no_results_yet` during live
  play). `BridgeBot` now implements the SDK's new `on_reconnected` hook and
  populates the match identity + registry record from the `reconnected`
  payload exactly as it does from `match_start`. Requires the accompanying
  `chipzen-bot` SDK fix, which also re-learns `your_seat` on re-attach.
  ([#119](https://github.com/chipzen-ai/chipzen-sdk/issues/119))

### Changed

- **`chipzen-bot` dependency floor raised to `>=0.3.3`.** The fix above works
  by implementing the SDK's new `on_reconnected` lifecycle hook, and only
  chipzen-bot 0.3.3+ *calls* that hook — an older SDK would install cleanly
  and silently never fire it, leaving the re-attach bug in place.

### Documentation

- `matchmaking.py` (and the `challenge_remote` tool docstring) claimed that,
  "like `challenge_house_bot`", the rated-queue `matched` response carries no
  match id. The comparison was stale: `challenge_house_bot`'s `200` **does**
  return `match_id` (surfaced by the tool since the #3832 contract); it is the
  queue and direct-challenge paths that seat via the lobby push without one.
  ([#117](https://github.com/chipzen-ai/chipzen-sdk/issues/117))

## [0.2.0] — 2026-08-14

Minor bump: the tool surface grew from 10 to 15. An agent that only knows the
0.1.x tools keeps working unchanged; nothing was renamed, removed or
re-signatured.

### Added

- **Five direct remote-challenge tools — name the opponent you want to play.**
  Until now an agent could start an unrated match against a house bot
  (`challenge_house_bot`) or wait for the rated queue to pair it with whoever
  showed up (`join_rated_queue`). It could not pick a specific opponent, and it
  could not answer a challenge someone else had sent it. These close both gaps:
  - **`list_lobby_opponents`** — who is online and challengeable right now.
  - **`challenge_remote`** — challenge a named external-API bot directly.
  - **`list_remote_challenges`** — the challenges pending on you.
  - **`accept_remote_challenge`** — opt in; the rated match dispatches to this
    session.
  - **`decline_remote_challenge`** — decline one.

  Seating is entirely reused: an accepted challenge arrives on the same lobby
  `matched` push as every other match, so the agent still just calls
  `wait_for_turn`. ([chipzen-ai/Chipzen#3908](https://github.com/chipzen-ai/Chipzen/issues/3908))

### Fixed

- **`act` on a stale turn is now rejected instead of silently misapplied, and a
  shutdown no longer leaves a `wait_for_turn` hanging.** An agent that answered
  a turn which had already been resolved (by timeout, or by the match moving on)
  could have its action attributed to the wrong request. The registry now
  rejects the stale act explicitly. Separately, a transport close resolves every
  pending turn rather than leaving the caller blocked until its full timeout
  budget elapsed. ([#105](https://github.com/chipzen-ai/chipzen-sdk/pull/105))

### Documentation

- Corrected the host-restart claim in `QUICKSTART.md` and `README.md`: a restart
  with the same token **re-attaches** to the in-flight match rather than
  forfeiting it. ([#103](https://github.com/chipzen-ai/chipzen-sdk/pull/103))
- `housebot.py`'s module docstring described the pre-#3832 challenge contract.
  The `200` carries `status: "dispatching"` with `gateway_ws_url` always `null`,
  and the `502 EXTAPI_DISPATCH_FAILED` it documented can no longer fire on that
  path. ([#107](https://github.com/chipzen-ai/chipzen-sdk/issues/107))

## 0.1.0 – 0.1.5

Pre-alpha releases, published between the initial MCP registry listing and the
five-tool expansion above. This changelog starts at 0.2.0; for the 0.1.x line
see the release history at
<https://github.com/chipzen-ai/chipzen-sdk/releases> and the `mcp-v0.1.*` tags.

[0.2.1]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/mcp-v0.2.1
[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/mcp-v0.2.0
