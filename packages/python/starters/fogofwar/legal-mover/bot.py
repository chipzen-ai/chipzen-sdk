# cython: language_level=3str
"""Chipzen Fog of War starter: plays a legal move from the request. PREVIEW: not live.

Fog of War is chess where you see only part of the board, and capturing the
king wins (there is no check and no mate). No engine, no chess library: every
request lists the legal moves (`state.legal_moves`, UCI strings like "e2e4"),
so this bot only has to pick one. It captures the king when it sees it in
reach, else takes the most valuable piece it can see, else promotes to a
queen, else plays a random move.

It reads only what the request shows: `state.pieces`, the pieces on the
squares you see. You never get the position or the opponent's moves; a
stronger bot would keep its own guess about the hidden board.

Replace `choose_move()` with your own strategy. Keep `main()` at the bottom:
the image runs it.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

from chipzen import FogOfWarBot
from chipzen.client import run_bot
from chipzen.fogofwar import FogOfWarState

#: What a captured piece is worth, by its FEN letter (lower case). Taking the
#: king wins the game, so it outranks everything.
PIECE_VALUE = {"p": 1, "n": 3, "b": 3, "r": 5, "q": 9, "k": 1000}


class FogLegalMover(FogOfWarBot):
    """King capture first, then the biggest visible capture, then promotion, else random."""

    def __init__(self) -> None:
        # Left unannotated on purpose: this file is cythonized for the
        # runtime image, where a PEP 526 attribute annotation is a type
        # declaration, not a hint.
        self.rng = random.Random()

    def choose_move(self, state: FogOfWarState) -> str:
        best_value = 0
        best = []
        for move in state.legal_moves:
            target = state.captured_by(move)
            value = PIECE_VALUE[target.lower()] if target is not None else 0
            if value > best_value:
                best_value, best = value, [move]
            elif value == best_value and value > 0:
                best.append(move)
        if best:
            return self.rng.choice(best)
        promotions = [move for move in state.legal_moves if move.endswith("q")]
        if promotions:
            return self.rng.choice(promotions)
        return self.rng.choice(state.legal_moves)


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

    asyncio.run(
        run_bot(
            url,
            FogLegalMover(),
            token=os.environ.get("CHIPZEN_TOKEN"),
            ticket=os.environ.get("CHIPZEN_TICKET"),
        )
    )


if __name__ == "__main__":
    main()
