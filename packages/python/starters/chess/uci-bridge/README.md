# UCI bridge: a chess starter (preview)

> **Preview: chess is not live on Chipzen yet.** This starter needs the
> `chipzen-bot` release that adds chess, which is not out yet. Whether a chess
> bot may start a second process (the engine) is part of the chess bot-machine
> sheet, which is not published yet.

Plays the moves of a UCI chess engine you bring. **No engine is bundled.**

## Files

| File | Purpose |
|---|---|
| [`bot.py`](bot.py) | The bridge: a `UciBridgeBot` with your engine's path and settings. Keep `main()`. |
| [`engine/`](engine/) | Put your engine here as `engine/engine` (Linux x86-64), with any files it loads. |
| [`requirements.txt`](requirements.txt) | The SDK pin. |
| [`Dockerfile`](Dockerfile) | Cython build of `bot.py`, plus a read-only copy of `engine/`. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight. |

## What it sends the engine

| When | UCI |
|---|---|
| Match start (in the background) | `uci` (waits for `uciok`), a `setoption` per entry of `uci_options`, `ucinewgame`, `isready` |
| Each move | `position fen <start_fen> moves <moves...>`, then `go movetime <ms>` |
| Match end | `quit` |

`<ms>` is the free window plus `my_bank_ms / moves_to_go`, less
`send_margin_ms` and `engine_margin_ms`, and never more than the request
allows. To let the engine manage its own clock instead, override
`go_command(state)` and return `go wtime ... btime ... winc ... binc ...`
from `state.clock` and `state.side`.

## When the engine does not deliver

The bridge never sends an illegal move. It plays `fallback_move(state)` (the
first legal move) and logs a warning when the engine:

- answers a move that is not in `state.legal_moves` (or `bestmove (none)`);
- gives no `bestmove` in time (the bridge sends `stop`, and drops the late
  answer before the next move);
- crashed or never started (it is restarted at the next move).

## Settings

| Attribute | Default | Meaning |
|---|---|---|
| `engine_path` | `/bot/engine/engine` | The engine binary. `CHIPZEN_UCI_ENGINE` overrides it. |
| `engine_args` | `()` | Command-line arguments for the engine. |
| `uci_options` | `{"Threads": 1, "Hash": 64}` | `setoption` values. |
| `moves_to_go` | `30` | The bank is spread as if this many moves remained. |
| `send_margin_ms` | `150` | Kept back for the answer's trip to the platform. |
| `engine_margin_ms` | `100` | Kept back for the engine to print `bestmove`. |
| `startup_timeout_ms` | `10000` | How long the engine may take to load. |

## Check and package

```
chipzen-sdk validate .        # runs with or without an engine: no engine means fallback moves
docker build -t my-uci-bot:v1 .
docker save my-uci-bot:v1 | gzip > my-uci-bot.tar.gz
```

Check your engine's licence before you ship it in an image. See
[`docs/CHESS-BOTS.md`](../../../../../docs/CHESS-BOTS.md) for the whole
protocol.
