# Legal mover: a chess starter (preview)

> **Preview: chess is not live on Chipzen yet.** This starter needs the
> `chipzen-bot` release that adds chess, which is not out yet.

**Style: greedy.** Every move it takes the most valuable piece it can capture,
else promotes a pawn to a queen, else plays a random legal move.

It has no chess engine and no chess library. Every request lists the legal
moves (`state.legal_moves`, in UCI: `e2e4`, `e7e8q`, castling as `e1g1`), so
the bot only chooses among them. To see what a move captures it reads the
board from the FEN (`state.fen`).

## Files

| File | Purpose |
|---|---|
| [`bot.py`](bot.py) | The bot. Replace `choose_move()` with your strategy; keep `main()`. |
| [`requirements.txt`](requirements.txt) | The SDK pin. Add your own dependencies here. |
| [`Dockerfile`](Dockerfile) | Multi-stage Cython build: the runtime image has no `.py` source for the bot. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight. |

## What your bot gets

`choose_move(state)` gets a `ChessState` and returns one of
`state.legal_moves`. The fields you will use most:

| Field | Meaning |
|---|---|
| `state.side` | `"white"` or `"black"`: your colour this game. Not your seat: colours are reversed between the two games of a pairing. |
| `state.legal_moves` | Every legal move, UCI, sorted. Answer with one of these. |
| `state.fen`, `state.start_fen`, `state.moves` | The position now, the start position, every move so far. |
| `state.clock` | `window_ms` (free time per move), `my_bank_ms`, `opp_bank_ms`. |

## Time

Each move gets a free window (1 s in `chess-1.0`); time beyond it comes out of
a 60 s bank that is never refilled. A move later than the window plus your bank
loses on time. The SDK stops waiting `send_margin_ms` (150 ms) before the
deadline and sends `fallback_move(state)` (the first legal move) instead, so a
slow bot still moves. This bot answers in well under a millisecond.

The time is measured on the platform from sending the request to receiving
your answer, so it includes the network; 150 ms is the margin to keep until
transit times are published. If an answer is rejected, the SDK answers again
with another listed move, at most 3 answers per move. The game is drawn after
300 plies (`state.own_moves_left` counts your moves left before that).

## Check and package

```
chipzen-sdk validate .
docker build -t my-chess-bot:v1 .
docker save my-chess-bot:v1 | gzip > my-chess-bot.tar.gz
```

See [`docs/CHESS-BOTS.md`](../../../../../docs/CHESS-BOTS.md) for the whole
protocol.
