"""Base class for chess bots. PREVIEW: chess is not live on Chipzen yet.

Subclass :class:`ChessBot` and implement :meth:`~ChessBot.choose_move`: it
gets a :class:`~chipzen.chess.ChessState` and returns one of
``state.legal_moves`` (a UCI string, or an :class:`~chipzen.Action` built with
:meth:`chipzen.Action.move`).

The session loop is the one every Chipzen bot runs
(:func:`chipzen.client.run_bot` / :func:`chipzen.run_external_bot`): at a table
whose server ``hello`` declares ``state_shape: "chess"`` it parses each
``turn_request`` into a :class:`~chipzen.chess.ChessState` and calls
:meth:`~ChessBot.decide`. A :class:`ChessBot` declares
``supported_games=["chess"]`` in its ``hello`` unless the caller passes its own
list.

**No chess rules in the SDK.** The SDK never computes which moves are legal.
Every request lists them (``state.legal_moves``), and the SDK only checks that
an answer is in that list before sending it. Chipzen's chess engine runs on the
platform only.

See ``docs/CHESS-BOTS.md``.
"""

from __future__ import annotations

import dataclasses
import logging
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any

from chipzen.bot import ChipzenBot
from chipzen.chess import (
    MOVE_ACTION,
    STATE_SHAPE,
    ChessGameConfig,
    ChessState,
    ChessStateParseError,
    first_legal_move,
    is_uci_move,
)
from chipzen.models import Action, GameState

logger = logging.getLogger("chipzen")


class ChessBot(ChipzenBot):
    """Base class for a chess bot. Preview: chess is not live on Chipzen yet.

    Implement :meth:`choose_move`. Everything else has a working default.

    **Colour.** Read ``state.side`` (``"white"`` or ``"black"``), never the
    seat: in a pairing the two games are played with colours reversed, so the
    same seat plays White in one game and Black in the other.

    **Timing.** ``state.timeout_ms`` is the free window plus your bank left
    (``state.clock``). The SDK waits for :meth:`choose_move` until
    :attr:`send_margin_ms` before ``timeout_ms`` ends; past that it sends
    :meth:`fallback_move` and logs a warning, because a chess seat that sends
    nothing in time loses on time. The same fallback is sent when
    :meth:`choose_move` raises (under ``safe_mode``) or returns a move that is
    not in ``state.legal_moves``. While a timed-out call is still running,
    later requests get the fallback without calling :meth:`choose_move` again.
    Your process keeps running between requests, so thinking on the
    opponent's time is allowed.

    **Rejections.** If the platform rejects an answer (``action_rejected``),
    the SDK answers again, in the time left, with :meth:`fallback_move`, or
    with the next listed move when that one was already sent. The platform
    reads at most 3 answers per request (the answer and two retries,
    :data:`chipzen.chess.MAX_ANSWERS_PER_REQUEST`); a 3rd rejected answer
    stands as a miss, so the SDK sends no more than that.

    **Ply cap.** The game is drawn after ``game_config.ply_cap`` plies (300
    in ``chess-1.0``). ``state.own_moves_left`` is how many moves you have
    left before it.

    Example::

        from chipzen import ChessBot

        class FirstMove(ChessBot):
            def choose_move(self, state):
                return state.legal_moves[0]
    """

    #: How long before ``timeout_ms`` the SDK stops waiting for
    #: :meth:`choose_move` and sends the fallback. It covers the time the
    #: answer spends in transit, which the platform counts against the clock.
    send_margin_ms: int = 150

    #: The match's ``match_start.game_config``, parsed by the default
    #: :meth:`on_match_start` / :meth:`on_reconnected`. ``None`` until then.
    game_config: ChessGameConfig | None = None

    # ------------------------------------------------------------------
    # The one method to write
    # ------------------------------------------------------------------

    @abstractmethod
    def choose_move(self, state: ChessState) -> str | Action:
        """Return this move: one of ``state.legal_moves``, as a UCI string or an Action."""

    def fallback_move(self, state: ChessState) -> str:
        """The move sent when :meth:`choose_move` cannot answer in time or answers badly.

        The default is ``state.legal_moves[0]``. Override it with something
        cheap and sure (it runs with the clock nearly spent); it must return
        a member of ``state.legal_moves``.
        """
        return state.legal_moves[0]

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def decide(self, state: GameState | ChessState) -> Action:
        """Call :meth:`choose_move` and turn its answer into an :class:`Action`.

        You do not normally override this. It fills in ``state.game_config``
        and returns ``Action.move(...)``. An answer that is not in
        ``state.legal_moves`` is replaced by :meth:`fallback_move`, with an
        error logged: sending it would be rejected, and an unanswered move
        loses on time.

        Raises:
            TypeError: ``state`` is not a chess state (the bot was seated at
                another game's table).
        """
        if not isinstance(state, ChessState):
            raise TypeError(f"ChessBot received a {type(state).__name__}; it only plays chess")
        if self.game_config is not None and state.game_config is None:
            state = dataclasses.replace(state, game_config=self.game_config)
        answer = self.choose_move(state)
        move = answer.params.get("move") if isinstance(answer, Action) else answer
        if isinstance(answer, Action) and answer.action != MOVE_ACTION:
            move = None
        if isinstance(move, str) and move in state.legal_moves:
            return Action.move(move)
        fallback = self.fallback_move(state)
        logger.error(
            "chess request %s: choose_move() returned %r, which is not one of the %d legal "
            "moves; sending %s instead",
            state.request_id,
            answer,
            len(state.legal_moves),
            fallback,
        )
        return Action.move(fallback)

    # ------------------------------------------------------------------
    # Match context
    # ------------------------------------------------------------------

    def on_match_start(self, match_info: dict) -> None:
        """Parse ``match_start.game_config`` into :attr:`game_config`.

        If you override this, call ``super().on_match_start(match_info)``.
        """
        self._learn_game_config(match_info)

    def on_reconnected(self, message: dict) -> None:
        """Re-learn :attr:`game_config` from a ``reconnected`` message.

        If you override this, call ``super().on_reconnected(message)``.
        """
        if "game_config" in message:
            self._learn_game_config(message)

    def _learn_game_config(self, message: Mapping[str, Any]) -> None:
        try:
            self.game_config = ChessGameConfig.from_match_start(message)
        except ChessStateParseError as exc:
            self.game_config = None
            logger.warning("chess game_config could not be read (%s); playing on", exc)


def fallback_action(request: Mapping[str, Any]) -> Action | None:
    """A legal answer for a raw chess ``turn_request``: its first listed legal move.

    Used by the session loop when the payload fails to parse, so a bot whose
    state could not be read still answers with a move from the list the
    platform sent. ``None`` when the request lists no move.
    """
    move = first_legal_move(request)
    return Action.move(move) if move is not None else None


def is_move_answer(action: object, state: ChessState) -> bool:
    """Whether ``action`` is a ``move`` Action naming one of ``state.legal_moves``."""
    if not isinstance(action, Action) or action.action != MOVE_ACTION:
        return False
    move = action.params.get("move")
    return is_uci_move(move) and move in state.legal_moves


__all__ = ["STATE_SHAPE", "ChessBot", "fallback_action", "is_move_answer"]
