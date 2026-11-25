# UCI bridge: a chess starter (preview)

> **Preview: chess is not live on Chipzen yet.** This starter needs the
> `chipzen-bot` release that adds chess, which is not out yet.

A chess seat may run your engine as a child process: that is what this starter
does. The seat is **1 vCPU** (Linux x86-64, no arm64) and **2 GB** for the
whole container, with **no network** during a match, so the engine binary and
anything it loads (a network file, an opening book) must be in the image, and
`Threads` 1 with a `Hash` well under 1024 MB is the sensible setting. Build the
engine for the x86-64 baseline, or one that picks its instruction set at run
time: the exact processor can change from match to match.

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
| Before connecting (`main()` calls `start_engine()`) | `uci` (waits for `uciok`), a `setoption` per entry of `uci_options`, `ucinewgame`, `isready` |
| Match start (in the background) | `ucinewgame`, `isready` (and the start-up above if the engine is not running) |
| Each move | `position fen <start_fen> moves <moves...>`, then `go movetime <ms>` |
| Match end | `quit` |

Start-up is not on the clock, but anything still loading when the first
request arrives is charged to that move: that is why `main()` starts the
engine before it connects.

`<ms>` is the free window (1 s in `chess-1.0`) plus `my_bank_ms / moves_to_go`
(the bank is 60 s, never refilled), less `send_margin_ms` and
`engine_margin_ms`, and never more than the request allows. Near the end of a
long game the bank is shared over the moves left before the 300-ply draw
(`state.own_moves_left`) instead, when that is fewer. To let the engine manage its own clock instead, override
`go_command(state)` and return `go wtime ... btime ... winc ... binc ...`
from `state.clock` and `state.side`.

## When the engine does not deliver

The bridge never sends an illegal move. It plays `fallback_move(state)` (the
first legal move) and logs a warning when the engine:

- answers a move that is not in `state.legal_moves` (or `bestmove (none)`);
- gives no `bestmove` in time (the bridge sends `stop`, and drops the late
  answer before the next move);
- crashed or never started (it is restarted at the next move).

If the platform still rejects an answer (`action_rejected`), the SDK answers
again with `fallback_move(state)`, or the next listed move if that one was
already sent. The platform reads at most 3 answers per move (the answer and
two retries); a 3rd rejected answer is a miss, which loses on time.

## Settings

| Attribute | Default | Meaning |
|---|---|---|
| `engine_path` | `/bot/engine/engine` | The engine binary. `CHIPZEN_UCI_ENGINE` overrides it. |
| `engine_args` | `()` | Command-line arguments for the engine. |
| `uci_options` | `{"Threads": 1, "Hash": 64}` | `setoption` values. |
| `moves_to_go` | `30` | The bank is spread as if this many moves remained (or the moves left before the ply cap, if fewer). |
| `send_margin_ms` | `150` | Kept back for the answer's trip to the platform, which counts against your clock. Transit is not measured yet: keep 150 ms. |
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
