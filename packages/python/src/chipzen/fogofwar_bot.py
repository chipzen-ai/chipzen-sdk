"""Base class for Fog of War bots. PREVIEW: Fog of War is not live on Chipzen yet.

Subclass :class:`FogOfWarBot` and implement :meth:`~FogOfWarBot.choose_move`:
it gets a :class:`~chipzen.fogofwar.FogOfWarState` (your view of the board,
your own moves, the legal moves) and returns one of ``state.legal_moves``.

The session loop is the one every Chipzen bot runs: at a table whose server
``hello`` declares ``state_shape: "fogofwar"`` it parses each ``turn_request``
into a :class:`~chipzen.fogofwar.FogOfWarState` and calls
:meth:`~FogOfWarBot.decide`. A :class:`FogOfWarBot` declares
``supported_games=["fogofwar"]`` in its ``hello`` unless the caller passes its
own list. Timing, the fallback move and retries after ``action_rejected`` work
exactly as for :class:`chipzen.ChessBot`.

**No rules in the SDK.** Every request lists the legal moves, worked out by
the platform's engine from the whole (hidden) position. The SDK only checks
that an answer is in that list.

See ``docs/CHESS-BOTS.md`` (the Fog of War section).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from chipzen.chess_bot import MoveListBot
from chipzen.fogofwar import STATE_SHAPE, FogOfWarState, parse_fogofwar_turn_request
from chipzen.models import Action


class FogOfWarBot(MoveListBot):
    """Base class for a Fog of War bot. Preview: Fog of War is not live on Chipzen yet.

    Implement :meth:`choose_move`. Everything else has a working default.

    **What you see.** ``state.visible`` (the squares your pieces stand on and
    can move to) and ``state.pieces`` (the pieces on them, both colours). You
    never get the position or the opponent's moves; a bot that wants a belief
    about the hidden board keeps it itself, from ``state.my_moves``, the views
    it was shown and ``state.ply``.

    **Winning.** Capturing the king wins; there is no check or mate. When the
    opponent's king is in view and one of your moves lands on it,
    ``state.captured_by(move)`` returns ``"k"`` (or ``"K"``).

    **Colour, timing, rejections, ply cap.** As :class:`chipzen.ChessBot`:
    read ``state.side``, not the seat; the SDK sends :meth:`fallback_move`
    :attr:`send_margin_ms` before ``timeout_ms`` ends, or when
    :meth:`choose_move` raises or answers a move not in ``state.legal_moves``;
    after ``action_rejected`` it answers again, at most 3 answers per request.
    A miss of any kind loses on time.

    Example::

        from chipzen import FogOfWarBot

        class TakeTheKing(FogOfWarBot):
            def choose_move(self, state):
                for move in state.legal_moves:
                    if state.captured_by(move) in ("k", "K"):
                        return move
                return state.legal_moves[0]
    """

    game_type: ClassVar[str] = STATE_SHAPE

    @staticmethod
    def parse_request(message: Mapping[str, Any]) -> FogOfWarState:
        """Parse a Fog of War ``turn_request`` into a :class:`~chipzen.fogofwar.FogOfWarState`."""
        return parse_fogofwar_turn_request(message)

    def _is_own_state(self, state: object) -> bool:
        return isinstance(state, FogOfWarState)

    @abstractmethod
    def choose_move(self, state: FogOfWarState) -> str | Action:
        """Return this move: one of ``state.legal_moves``, as a UCI string or an Action."""

    def fallback_move(self, state: FogOfWarState) -> str:
        """The move sent when :meth:`choose_move` cannot answer in time or answers badly.

        The default is ``state.legal_moves[0]``. Override it with something
        cheap and sure; it must return a member of ``state.legal_moves``.
        """
        return state.legal_moves[0]


__all__ = ["STATE_SHAPE", "FogOfWarBot"]
