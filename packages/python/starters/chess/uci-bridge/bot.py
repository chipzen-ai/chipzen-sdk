# cython: language_level=3str
"""Chipzen chess starter: a bridge to your UCI engine. PREVIEW: chess is not live.

Plays the moves of a UCI chess engine you supply (Stockfish-style: it speaks
`uci`, `position`, `go`, `bestmove`). No engine is bundled: put yours in the
`engine/` folder as `engine/engine` (a Linux x86-64 build), and the Dockerfile
copies it into the image.

The engine is started before the bot connects: start-up is free, but anything
still loading when the first request arrives is charged to that move.

For each move the bridge sends the engine `position fen <start_fen> moves ...`
and `go movetime <ms>`, sized to the free window (1 s in `chess-1.0`) plus a
share of your bank (60 s, never refilled), shared over the moves left before
the 300-ply draw when that is fewer, and answers with the engine's `bestmove`.
If the engine answers a move that is not in the request's legal moves, gives
no answer in time, or has crashed, the bridge plays a legal move from the
request instead and logs a warning. If the platform rejects an answer anyway,
the SDK answers again with another listed move: at most 3 answers per move.

Tune the settings below. To run the engine from somewhere else, set
`CHIPZEN_UCI_ENGINE` to its path. Keep `main()` at the bottom: the image runs it.
"""

from __future__ import annotations

import asyncio
import os
import sys

from chipzen.chess_uci import UciBridgeBot
from chipzen.client import run_bot


class MyEngine(UciBridgeBot):
    """Your engine, with its settings."""

    #: Where the Dockerfile puts your engine.
    engine_path = "/bot/engine/engine"

    #: `setoption` values sent at start-up. A chess seat has 1 vCPU and 2 GB
    #: for the whole container, so one thread, and keep Hash well under 1024.
    uci_options = {"Threads": 1, "Hash": 64}

    #: The bank is spent as if this many moves were still to come (or as many
    #: as are left before the ply cap, if fewer).
    moves_to_go = 30

    #: Kept back from every move for the answer's trip to the platform, which
    #: counts against your clock. Transit is not measured yet: keep 150 ms.
    send_margin_ms = 150


def main() -> None:
    """Entry point, called by the Dockerfile ENTRYPOINT.

    The Chipzen platform injects `CHIPZEN_WS_URL` and `CHIPZEN_TOKEN` (or
    `CHIPZEN_TICKET`) when it starts your container.
    """
    url = os.environ.get("CHIPZEN_WS_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not url:
        print(
            "error: CHIPZEN_WS_URL not set and no URL passed on the command line",
            file=sys.stderr,
        )
        sys.exit(1)

    bot = MyEngine()
    # Load the engine before connecting, off the clock.
    bot.start_engine()
    asyncio.run(
        run_bot(
            url,
            bot,
            token=os.environ.get("CHIPZEN_TOKEN"),
            ticket=os.environ.get("CHIPZEN_TICKET"),
        )
    )


if __name__ == "__main__":
    main()
