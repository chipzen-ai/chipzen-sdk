# Legal mover: a Fog of War starter (preview)

> **Preview: Fog of War is not live on Chipzen yet.** This starter needs the
> `chipzen-bot` release that adds Fog of War, which is not out yet.

**Style: greedy.** Fog of War is chess where you see only part of the board,
and capturing the king wins: there is no check and no mate. Every move this
bot captures the king if it can see it in reach, else takes the most valuable
piece it can see, else promotes a pawn to a queen, else plays a random legal
move.

It has no chess engine and no chess library. Every request lists the legal
moves (`state.legal_moves`, in UCI: `e2e4`, `e7e8q`, castling as `e1g1`), so
the bot only chooses among them. To see what a move captures it reads
`state.pieces`: the pieces on the squares it can see.

## Files

| File | Purpose |
|---|---|
| [`bot.py`](bot.py) | The bot. Replace `choose_move()` with your strategy; keep `main()`. |
| [`requirements.txt`](requirements.txt) | The SDK pin. Add your own dependencies here. |
| [`Dockerfile`](Dockerfile) | Multi-stage Cython build: the runtime image has no `.py` source for the bot. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight. |

## What your bot gets

`choose_move(state)` gets a `FogOfWarState` and returns one of
`state.legal_moves`. There is **no** `fen` and **no** `moves`: the position
and the opponent's moves are what the fog hides. The fields you will use most:

| Field | Meaning |
|---|---|
| `state.side` | `"white"` or `"black"`: your colour this game. Not your seat. |
| `state.legal_moves` | Every legal move, UCI, sorted. Answer with one of these. |
| `state.visible` | Every square you see now: the squares your pieces stand on and can move to. |
| `state.pieces` | The pieces on those squares, both colours, by square (`{"e4": "P", "f5": "p"}`). A visible square with no entry is empty. |
| `state.my_moves` | Your own moves so far. Never the opponent's. |
| `state.castling` | Your own castling rights (`KQ`, `kq`, `-`, ...). |
| `state.ply`, `state.start_fen` | Plies played by both sides, and the (public) start position. |
| `state.clock` | `window_ms` (free time per move), `my_bank_ms`, `opp_bank_ms`. |

Helpers: `state.captured_by(move)` (the opponent piece a move lands on, if you
see one; `"k"`/`"K"` wins), `state.my_pieces`, `state.seen_opponent_pieces`,
`state.is_visible(square)`.

## Time

As in chess: a free window per move (1 s in `fogofwar-1.0`), then a 60 s bank
that is never refilled. A move later than the window plus your bank loses on
time, and so does any other miss. The SDK sends `fallback_move(state)` (the
first legal move) `send_margin_ms` (150 ms) before the deadline, so a slow bot
still moves. If an answer is rejected, the SDK answers again with another
listed move, at most 3 answers per move. The game is drawn after 300 plies.

## Check and package

```
chipzen-sdk validate .
docker build -t my-fog-bot:v1 .
docker save my-fog-bot:v1 | gzip > my-fog-bot.tar.gz
```

Upload it declaring `fogofwar` as its game. A chess bot does not play Fog of
War, and this bot does not play chess.

See [`docs/CHESS-BOTS.md`](../../../../../docs/CHESS-BOTS.md) (the Fog of War
section) for the whole protocol.
