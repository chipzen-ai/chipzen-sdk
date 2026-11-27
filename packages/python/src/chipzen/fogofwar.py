"""Typed models for Fog of War (``state_shape: "fogofwar"``). PREVIEW: not live.

**Preview.** Fog of War is not playable on Chipzen yet. This module follows the
Fog of War Layer 2 dialect (``FOGOFWAR-GAME-STATE-PROTOCOL.md``, a draft, a
delta of the chess dialect) and the rule set ``fogofwar-1.0``. See
``docs/CHESS-BOTS.md`` (the Fog of War section).

Fog of War is its own game, not a chess rule set: a chess bot does not play it,
and a Fog of War bot declares ``fogofwar`` in ``supported_games``. It differs
from chess in what a request shows:

- there is **no** ``fen`` and **no** ``moves``: the position and the opponent's
  moves are what the fog hides;
- ``state.my_moves`` is this seat's own moves from the start, in UCI;
- ``state.visible`` is every square this seat sees now (the squares its pieces
  stand on and can move to), and ``state.pieces`` the pieces on them, by square,
  as FEN letters (``P N B R Q K`` White, lower case Black). A visible square
  with no entry is empty;
- ``state.castling`` is this seat's own castling rights.

Everything else is chess's: one call, ``move``; ``state.legal_moves`` lists
every legal move in UCI, sorted; the answer is
``{"action": "move", "params": {"move": "<uci>"}}`` (:meth:`chipzen.Action.move`);
the clock (:class:`chipzen.chess.ChessClock`), the game config
(:class:`chipzen.chess.ChessGameConfig`), the rejection codes and the retry cap
are chess's.

The rules differ too: **capturing the king wins**. There is no check, no
checkmate and no stalemate, so any move the pieces can make is legal, moving
into attack included. **This SDK has no rules**: it only reads
``legal_moves``, which the platform's engine works out.

The parser is strict, like the chess one: a missing or mistyped required field
raises :class:`FogOfWarStateParseError`. Keys this release does not know are
ignored (LAYER2-COMMON section 3, rule 5).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from chipzen.bigbadbots import (
    StateParseError,
    _fail,
    _int,
    _int_list,
    _list,
    _object,
    _str,
    _str_list,
    _to_wire,
)
from chipzen.chess import (
    MAX_ANSWERS_PER_REQUEST,
    MOVE_ACTION,
    MOVE_PHASE,
    REJECTION_CODES,
    SIDES,
    ChessClock,
    ChessGameConfig,
    ChessStateParseError,
)

#: The ``game.state_shape`` (and ``game_type``) of a Fog of War table.
STATE_SHAPE = "fogofwar"

#: ``match_start.game_config.variant`` at a Fog of War table: the dialect's name.
#: Not the rule set's ``variant`` (``"fog"``), which is in ``rule_set``.
DIALECT = "fogofwar"

#: The ``state.variant`` values this SDK release parses (the rule set's own
#: ``variant`` key).
SUPPORTED_VARIANTS = ("fog",)

#: FEN piece letters, as ``state.pieces`` carries them.
PIECE_LETTERS = frozenset("PNBRQKpnbrqk")

#: Square names, ``a1`` to ``h8``.
SQUARES = frozenset(f + r for f in "abcdefgh" for r in "12345678")

#: The Layer 1 ``turn_request`` envelope fields a :class:`FogOfWarState` carries.
_ENVELOPE = ("request_id", "seat", "timeout_ms", "valid_actions")


class FogOfWarStateParseError(ChessStateParseError):
    """A Fog of War payload is missing a required field, or one is mistyped."""


#: The game config of a Fog of War match has chess's shape: ``variant``
#: (``"fogofwar"``), ``rule_set_id`` (``"fogofwar-1.0"``), ``num_players``,
#: ``rule_set`` and ``engine_version``.
FogOfWarGameConfig = ChessGameConfig


@dataclass(frozen=True, slots=True)
class FogOfWarState:
    """One ``move`` request at a Fog of War table.

    Envelope attributes (Layer 1):
        request_id: Echo it in the ``turn_action`` (the SDK does).
        seat: The receiving seat. **Not your colour**: read :attr:`side`.
        timeout_ms: The whole time the server waits for this answer:
            ``clock.window_ms + clock.my_bank_ms``.
        valid_actions: Always ``["move"]``.

    State attributes (Layer 2):
        phase: Always ``"move"``.
        board, your_hole_cards: Always ``[]``.
        pot, to_call, min_raise, max_raise, your_stack: Always ``0``.
        opponent_stacks: Always ``[0]``.
        call: Always ``"move"``.
        side: ``"white"`` or ``"black"``: the colour you play this game.
        variant: ``"fog"``.
        ply: Plies played so far, by both sides (0 before White's first move).
        start_fen: The start position in FEN. Public: both sides know it.
        my_moves: Your own moves from the start, in UCI, in order. Never the
            opponent's.
        visible: Every square you see now, as square names.
        pieces: The pieces on the visible squares, by square, as FEN letters.
            A visible square with no entry is empty.
        castling: Your own castling rights in FEN letters, or ``"-"``.
        legal_moves: Every legal move now, in UCI, sorted. Answer with one of
            these: anything else is rejected.
        clock: The public clocks (:class:`chipzen.chess.ChessClock`).

    Match context (from ``match_start``, not from the ``turn_request``):
        game_config: The match's game config, filled in by
            :class:`chipzen.FogOfWarBot` when it saw ``match_start``.
    """

    request_id: str
    seat: int
    timeout_ms: int
    valid_actions: list[str]
    phase: str
    board: list[Any]
    your_hole_cards: list[Any]
    pot: int
    to_call: int
    min_raise: int
    max_raise: int
    your_stack: int
    opponent_stacks: list[int]
    call: str
    side: str
    variant: str
    ply: int
    start_fen: str
    my_moves: list[str]
    visible: list[str]
    pieces: dict[str, str]
    castling: str
    legal_moves: list[str]
    clock: ChessClock
    game_config: ChessGameConfig | None = dataclasses.field(
        default=None, kw_only=True, compare=False, repr=False
    )

    @property
    def own_moves_left(self) -> int | None:
        """How many more moves this seat can make before the ply cap draws the game.

        Counts this move. ``None`` when the ply cap is not known.
        """
        cap = self.game_config.ply_cap if self.game_config is not None else None
        if cap is None:
            return None
        return max((cap - self.ply + 1) // 2, 0)

    @property
    def is_white(self) -> bool:
        """Whether this seat plays White this game."""
        return self.side == "white"

    def piece_at(self, square: str) -> str | None:
        """The FEN letter on ``square`` if you see a piece there, else ``None``.

        ``None`` means empty *or* unseen; :meth:`is_visible` tells which.
        """
        return self.pieces.get(square)

    def is_visible(self, square: str) -> bool:
        """Whether you see ``square`` now."""
        return square in self.visible

    def is_mine(self, letter: str) -> bool:
        """Whether a FEN letter is one of your own pieces (upper case for White)."""
        return letter.isupper() == self.is_white

    @property
    def my_pieces(self) -> dict[str, str]:
        """Your own pieces, by square (you always see all of them)."""
        return {sq: p for sq, p in self.pieces.items() if self.is_mine(p)}

    @property
    def seen_opponent_pieces(self) -> dict[str, str]:
        """The opponent's pieces you see now, by square. Not all of them, in general."""
        return {sq: p for sq, p in self.pieces.items() if not self.is_mine(p)}

    def captured_by(self, move: str) -> str | None:
        """The opponent piece ``move`` lands on, if you see one there, else ``None``.

        Reads the view only: an en passant capture lands on an empty square,
        so it returns ``None`` for one. ``"k"`` / ``"K"`` means the move
        captures the king, which wins the game.
        """
        target = self.pieces.get(move[2:4])
        return target if target is not None and not self.is_mine(target) else None

    def to_state(self) -> dict[str, Any]:
        """Return the ``turn_request.state`` payload this state was parsed from."""
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name in _ENVELOPE or f.name == "game_config":
                continue
            value = getattr(self, f.name)
            out[f.name] = value.to_dict() if isinstance(value, ChessClock) else _to_wire(value)
        return out


def _pieces(state: Mapping[str, Any], where: str) -> dict[str, str]:
    pieces = _object(state, "pieces", where)
    for square, letter in pieces.items():
        if square not in SQUARES or not isinstance(letter, str) or letter not in PIECE_LETTERS:
            raise _fail(where, "pieces", "an object of square -> FEN piece letter", pieces)
    return pieces


def parse_fogofwar_turn_request(message: Mapping[str, Any]) -> FogOfWarState:
    """Parse a Fog of War ``turn_request`` into a :class:`FogOfWarState`.

    Raises:
        FogOfWarStateParseError: ``state`` is missing, a required field is
            missing or mistyped, or the variant is not one this release parses.
    """
    where = "turn_request.state"
    try:
        state = _object(message, "state", "turn_request")
        variant = _str(state, "variant", where)
        if variant not in SUPPORTED_VARIANTS:
            raise FogOfWarStateParseError(
                f"{where}.variant: {variant!r} is not supported by this SDK release "
                f"(expected one of {SUPPORTED_VARIANTS})"
            )
        side = _str(state, "side", where)
        if side not in SIDES:
            raise FogOfWarStateParseError(
                f"{where}.side: expected 'white' or 'black', got {side!r}"
            )
        return FogOfWarState(
            request_id=_str(message, "request_id", "turn_request"),
            seat=_int(message, "seat", "turn_request"),
            timeout_ms=_int(message, "timeout_ms", "turn_request"),
            valid_actions=_str_list(message, "valid_actions", "turn_request"),
            phase=_str(state, "phase", where),
            board=_list(state, "board", where),
            your_hole_cards=_list(state, "your_hole_cards", where),
            pot=_int(state, "pot", where),
            to_call=_int(state, "to_call", where),
            min_raise=_int(state, "min_raise", where),
            max_raise=_int(state, "max_raise", where),
            your_stack=_int(state, "your_stack", where),
            opponent_stacks=_int_list(state, "opponent_stacks", where),
            call=_str(state, "call", where),
            side=side,
            variant=variant,
            ply=_int(state, "ply", where),
            start_fen=_str(state, "start_fen", where),
            my_moves=_str_list(state, "my_moves", where),
            visible=_str_list(state, "visible", where),
            pieces=_pieces(state, where),
            castling=_str(state, "castling", where),
            legal_moves=_str_list(state, "legal_moves", where),
            clock=ChessClock.from_dict(_object(state, "clock", where), f"{where}.clock"),
        )
    except FogOfWarStateParseError:
        raise
    except StateParseError as exc:
        raise FogOfWarStateParseError(str(exc)) from exc


__all__ = [
    "DIALECT",
    "MAX_ANSWERS_PER_REQUEST",
    "MOVE_ACTION",
    "MOVE_PHASE",
    "PIECE_LETTERS",
    "REJECTION_CODES",
    "SIDES",
    "SQUARES",
    "STATE_SHAPE",
    "SUPPORTED_VARIANTS",
    "FogOfWarGameConfig",
    "FogOfWarState",
    "FogOfWarStateParseError",
    "parse_fogofwar_turn_request",
]
