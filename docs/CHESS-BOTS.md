# Chess bots (preview)

> **PREVIEW: chess is not live on Chipzen yet.** Nothing on this page can be
> played on the platform today, and the chess support it describes is not in
> a released `chipzen-bot`. The rule set it follows, `chess-1.0`, is frozen
> but in `preview`. The chess Layer 2 dialect is a draft on the platform side,
> so message field names may still change before chess goes live; this page
> and the SDK follow it.

This page is for building a chess bot with the Python SDK: what your bot
receives, what it answers, how the clock works, what happens to a bad answer,
and the two starters.

## 1. The game

Standard chess under the FIDE Laws, rule set `chess-1.0`:

- Two seats. Seat 0 plays White in a match and moves first. **A seat number
  is not a colour:** read `state.side`.
- Moves are UCI strings: `e2e4`; promotions with a lower-case piece letter,
  `e7e8q`; castling as the king's two-square move, `e1g1` / `e1c1`.
- Draws are automatic: stalemate, insufficient material, threefold repetition,
  the 50-move rule (and FIDE's fivefold and 75-move rules behind them), and a
  draw at 300 plies. Bots cannot claim or offer draws.
- A bot that misses a move (silence, a disconnect, a late answer, or no legal
  answer before its time runs out) loses on time, except that running out of
  time when the opponent cannot mate is a draw (FIDE 6.9). There is no default
  move: the first miss ends the game.
- Every number here comes from the rule set, which every match sends in
  `match_start.game_config.rule_set` (section 2.1). Read it from there; never
  hard-code it.

A second rule set, `chess-armageddon-1.0`, is a bracket's tie-break game: the
same game, but every draw counts as a win for Black, and Black's bank is
smaller (48 s against White's 60 s). A match plays it only when its
`game_config.rule_set_id` names it.

**No chess rules in the SDK.** Chipzen's chess engine runs on the platform
only. The SDK never works out which moves are legal: every request lists them.
Test your bot on Chipzen once chess is live; the SDK has no local simulator.

### Colour-reversed pairs

In a bracket pairing, two bots play two games with colours reversed: the bot
that had White in the first game has Black in the second. Each game is its own
match, so your bot sees two `match_start` frames, and in the second its
`state.side` is the other colour. (In other matches the platform decides which
bot sits in seat 0.) A bot that keys anything on its seat number instead of
`state.side` plays the wrong colour in one of the two games.

## 2. Messages

Chess uses the same Layer 1 transport as every Chipzen game
([`TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md)) and follows
[`LAYER2-COMMON.md`](protocol/LAYER2-COMMON.md). The server `hello` declares
`game.state_shape: "chess"`, and a `chipzen.ChessBot` answers with
`supported_games: ["chess"]`.

### 2.1 `match_start.game_config`

| Field | Type | Meaning |
|---|---|---|
| `variant` | string | `"chess"`, the dialect. (The rule set's own `variant`, `"standard"`, is inside `rule_set` and in every request.) |
| `rule_set_id` | string | `"chess-1.0"`, or `"chess-armageddon-1.0"` for a tie-break game. |
| `num_players` | int | `2`. |
| `rule_set` | object | The rule set's full table: `start_fen`, `window_ms`, `bank_ms`, the draw rules, `ply_cap`, `illegal_answer: "reject"`, `clock_visibility: "public"`, `on_miss: "loss_on_time"`. |
| `engine_version` | string | The chess engine release that runs the rule set (`"1.0.0"`). |

`ChessBot.game_config` holds it, parsed (`chipzen.chess.ChessGameConfig`, with
`window_ms`, `bank_ms` and `ply_cap` read from `rule_set`), and every
`ChessState` carries it as `state.game_config`. `state.own_moves_left` is how
many moves your bot has left before the ply cap draws the game.

### 2.2 `turn_request`: one per move

Only the seat whose colour is to move is asked. Every request carries the
whole game, so a bot needs no memory of earlier messages.

| Field | Where | Type | Meaning |
|---|---|---|---|
| `request_id` | envelope | string | Echoed in the answer (the SDK does it). |
| `seat` | envelope | int | Your seat. Not your colour. |
| `timeout_ms` | envelope | int | Your whole time for this move: `clock.window_ms + clock.my_bank_ms`. |
| `valid_actions` | envelope | array | Always `["move"]`. |
| `phase` | state | string | Always `"move"`. |
| `call` | state | string | Always `"move"`. |
| `side` | state | string | `"white"` or `"black"`: your colour this game. |
| `variant` | state | string | `"standard"`. |
| `ply` | state | int | Plies played so far (0 before White's first move). |
| `start_fen` | state | string | The start position in FEN, even when it is the standard one. |
| `moves` | state | array of string | Every move from the start, UCI, in order. |
| `fen` | state | string | The position now, in FEN. |
| `legal_moves` | state | array of string | Every legal move now, UCI, sorted. **Answer with one of these.** |
| `clock` | state | object | `window_ms`, `my_bank_ms`, `opp_bank_ms` (section 3). |

As in every game, `state` also carries the LAYER2-COMMON compatibility keys:
`board` and `your_hole_cards` are `[]`, `pot`, `to_call`, `min_raise`,
`max_raise` and `your_stack` are `0`, and `opponent_stacks` is `[0]`.

Example (White, move 3):

```json
{
  "type": "turn_request",
  "request_id": "req_00002",
  "seat": 0,
  "timeout_ms": 61000,
  "valid_actions": ["move"],
  "state": {
    "phase": "move", "call": "move", "side": "white", "variant": "standard",
    "ply": 4,
    "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "moves": ["e2e4", "e7e5", "f1c4", "b8c6"],
    "fen": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 2 3",
    "legal_moves": ["a2a3", "a2a4", "b1a3", "b1c3", "...", "g2g4", "h2h3", "h2h4"],
    "clock": {"window_ms": 1000, "my_bank_ms": 60000, "opp_bank_ms": 60000},
    "board": [], "your_hole_cards": [], "pot": 0, "to_call": 0,
    "min_raise": 0, "max_raise": 0, "your_stack": 0, "opponent_stacks": [0]
  }
}
```

The Python SDK parses it into `chipzen.chess.ChessState`, strictly: a missing
or mistyped field is an error, not a silent default.

### 2.3 `turn_action`: the answer

```json
{"type": "turn_action", "request_id": "req_00002", "action": "move", "params": {"move": "d1h5"}}
```

`ChessBot.choose_move` returns the UCI string (or `Action.move("d1h5")`); the
SDK builds the message.

### 2.4 After a move

The platform does not send a per-move `turn_result` yet. You learn the
opponent's move from your next request (`state.moves`). The match ends with
`match_end`.

## 3. Clocks

`chess-1.0` gives each move a **free 1 s window**, then charges any time beyond
it to a **60 s bank** per seat, which is never refilled. A move later than the
window plus your bank left loses on time. The clocks are public: every request
shows `clock.window_ms`, your bank (`my_bank_ms`) and your opponent's
(`opp_bank_ms`), as they stood before this move.

- The platform measures your time from sending the request to receiving your
  answer, rounded up to whole milliseconds, so it includes the network both
  ways. The typical and worst-case transit time is not measured yet: keep a
  150 ms margin until it is published.
- A rejected answer does not stop the clock: a retry (section 4) must arrive
  within the same `timeout_ms`, counted from when the request was sent.
- Start-up is not on the clock: the clock starts with the first
  `turn_request`. Do slow work (starting an engine, loading a network) before
  connecting; anything still loading when the first request arrives is
  charged to that move.
- `ChessBot.send_margin_ms` (default 150 ms) is that margin. The SDK stops
  waiting for `choose_move` `send_margin_ms` before `timeout_ms` runs out and
  sends `fallback_move(state)` (the first legal move unless you override it),
  so a slow bot still moves. While that late `choose_move` is still running,
  later requests get the fallback directly.
- Your process keeps running between requests, so thinking on the opponent's
  time is allowed.

## 4. Rejected answers and retries

Chess sets `illegal_answer: "reject"`: an answer that is not one of
`legal_moves` is never corrected and never replaced by the platform. The
platform sends `action_rejected` with a machine code in `details.code`, and you
may answer again, with the same `request_id`, within the time you have left:

| `details.code` | Meaning |
|---|---|
| `bad_shape` | The `turn_action` is not `{"action", "params": {"move": <string>}}`, is not JSON, or is over 4096 bytes. |
| `not_in_valid_actions` | `action` is not `"move"`. |
| `bad_param` | `params.move` is not a UCI move string (`E2E4`, `e2-e4`). |
| `not_in_legal_set` | A UCI string, but not one of `legal_moves`. |

`action_rejected` carries the same `request_id`, the code in `details.code`,
and `remaining_ms`, what is left of the request's `timeout_ms`.

- **At most 3 answers per request:** the first answer and two retries, while
  time is left. The first legal move is played, as if it had been the only
  answer.
- **If the 3rd answer is rejected too, it stands at once, as if time had run
  out:** a miss, so you lose on time. No `action_rejected` is sent for it, and
  no further answer to that request is read.
- If time runs out after a rejection, the last answer stands: a miss. With no
  time left, no `action_rejected` is sent.
- Every rejected answer is listed in your seat's
  `match_end.results[].bot_errors` (`reason` `bot_invalid_action`, `code`,
  `step`, `phase`, `message`).

What the Python SDK does for you:

- before sending, it checks that your answer is in `legal_moves`; if not, it
  logs an error and sends `fallback_move(state)` instead;
- on `action_rejected`, it answers again with `fallback_move(state)`, or with
  the first listed move not sent yet for that request if the fallback was
  already sent, and stops at 3 answers per request
  (`chipzen.chess.MAX_ANSWERS_PER_REQUEST`). It does not retry when
  `remaining_ms` is 0.

## 5. The starters

Both are in [`packages/python/starters/chess/`](../packages/python/starters/chess/).

| Starter | Create it | What it does |
|---|---|---|
| Legal mover | `chipzen-sdk init my_bot --template chess` | Picks from `legal_moves`: the biggest capture, else a queen promotion, else a random move. No engine. |
| UCI bridge | `chipzen-sdk init my_bot --template chess-uci` | Runs a UCI engine you supply and plays its `bestmove`. **No engine is bundled.** |

A minimal bot:

```python
from chipzen import ChessBot


class FirstMove(ChessBot):
    def choose_move(self, state):
        return state.legal_moves[0]
```

### 5.1 The UCI bridge

`chipzen.chess_uci.UciBridgeBot` maps each request onto UCI:

| Step | UCI |
|---|---|
| Before connecting (`start_engine()`, which the starter's `main()` calls) | `uci` / `uciok`, a `setoption` per `uci_options` entry, `ucinewgame`, `isready` / `readyok` |
| Match start (in a background thread) | `ucinewgame`, `isready` / `readyok` (and the start-up above if the engine is not running yet) |
| Each move | `position fen <start_fen> moves <moves...>`, then `go movetime <ms>` |
| Answer | `bestmove e2e4` becomes `{"action": "move", "params": {"move": "e2e4"}}` |
| Match end | `quit` |

`<ms>` is the window plus `my_bank_ms / moves_to_go`, less the margins, and
never more than the request allows. When fewer moves than `moves_to_go` are
left before the ply cap (`state.own_moves_left`), the bank is shared over
those instead. To let the engine manage its own clock,
override `go_command(state)` and return the clock form,
`go wtime <ms> btime <ms> winc <window_ms> binc <window_ms>`, with White's and
Black's banks taken from `my_bank_ms` and `opp_bank_ms` by `state.side`.

The bridge plays `fallback_move(state)` and logs a warning when the engine
answers a move that is not in `legal_moves`, answers `bestmove (none)`, gives
no answer in time (it sends `stop` and drops the late answer before the next
search), or has crashed (it restarts the engine at the next move). Set the
engine with `engine_path` or the `CHIPZEN_UCI_ENGINE` environment variable.

Check your engine's licence before you put it in a bot image.

## 6. Checking a bot: `chipzen-sdk validate`

`chipzen-sdk validate <folder>` recognises a `ChessBot` or `UciBridgeBot`
subclass (or take `--game chess`) and answers every request in the SDK's chess
fixtures: real `move` requests recorded from Chipzen's chess engine, for White
and Black, from the start, middle and end of games, with castling, promotion
and en passant on offer. It judges each answer as the platform does (section
4), and reports a crash, a change to the state it was handed, and an answer
slower than the free window (warning) or than `timeout_ms` (failure). The
fixtures are data only: they cannot tell you the legal moves of any other
position.

## 7. The machine

What a hosted chess seat gets (house bots and builders' bots alike):

| | |
|---|---|
| Processor | 1 vCPU, Linux x86-64 (no arm64). The exact processor model can change between matches: build for the x86-64 baseline, or pick the instruction set at run time. |
| Memory | 2 GB for the whole container, child processes included. Going over it kills the container: a miss. |
| Threads and processes | Allowed, no cap, but they share the one vCPU. A bridge may run its engine as a child process. |
| Disk | The image is read-only; `/tmp` is writable and empty at every match. |
| Network | None during a match, except the match WebSocket. Put engine files, networks and books in the image. |
| Image | A plain image with a non-root `USER` and an `ENTRYPOINT` that runs the bot: 250 MB compressed, 1500 MB unpacked. No log-cap wrapper. |
| Start-up | Free. Connect within 90 s. |
| Logs | stdout and stderr are your match log, not capped. Log a line or two per move; do not forward an engine's `info` lines. |

## 8. Not in this preview

- Fog of War (a separate game, `fogofwar`, on the same engine);
- per-move `turn_result` events, reconnection, and human play;
- published transit times (keep the 150 ms margin);
- JavaScript and Rust chess support.
