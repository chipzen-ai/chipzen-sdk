# cython: language_level=3str
"""Chipzen chess starter: plays a legal move from the request. PREVIEW: chess is not live.

No chess engine, no chess library. Every request lists the legal moves
(`state.legal_moves`, UCI strings like "e2e4"), so this bot only has to pick
one. It prefers captures, taking the most valuable piece it can, then queen
promotions, then a random move.

To see what a move captures it reads the board from the FEN (`state.fen`):
which piece stands on which square. That is reading the position, not
working out the rules; the platform decides what is legal.

Replace `choose_move()` with your own strategy. Keep `main()` at the bottom:
the image runs it.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

from chipzen import ChessBot
from chipzen.chess import ChessState
from chipzen.client import run_bot

#: What a captured piece is worth, by its FEN letter (lower case).
PIECE_VALUE = {"p": 1, "n": 3, "b": 3, "r": 5, "q": 9, "k": 0}


def board_from_fen(fen: str) -> dict:
    """The pieces on the board: square name ("e4") to FEN letter ("P" White, "p" Black)."""
    board = {}
    placement = fen.split()[0]
    for rank_index, row in enumerate(placement.split("/")):
        rank = 8 - rank_index
        file_index = 0
        for char in row:
            if char.isdigit():
                file_index += int(char)
            else:
                board["abcdefgh"[file_index] + str(rank)] = char
                file_index += 1
    return board


def captured_value(move: str, board: dict, en_passant: str, white: bool) -> int:
    """What `move` captures, by PIECE_VALUE; 0 for a quiet move."""
    target = board.get(move[2:4])
    if target is not None and target.isupper() != white:
        return PIECE_VALUE[target.lower()]
    mover = board.get(move[0:2], "")
    if mover.lower() == "p" and move[2:4] == en_passant:
        return PIECE_VALUE["p"]
    return 0


class LegalMover(ChessBot):
    """Captures first, then queen promotions, then a random legal move."""

    def __init__(self) -> None:
        # Left unannotated on purpose: this file is cythonized for the
        # runtime image, where a PEP 526 attribute annotation is a type
        # declaration, not a hint.
        self.rng = random.Random()

    def choose_move(self, state: ChessState) -> str:
        board = board_from_fen(state.fen)
        en_passant = state.fen.split()[3]
        best_value = 0
        best = []
        for move in state.legal_moves:
            value = captured_value(move, board, en_passant, state.is_white)
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
            LegalMover(),
            token=os.environ.get("CHIPZEN_TOKEN"),
            ticket=os.environ.get("CHIPZEN_TICKET"),
        )
    )


if __name__ == "__main__":
    main()
