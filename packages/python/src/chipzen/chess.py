"""Typed models for chess (``state_shape: "chess"``). PREVIEW: chess is not live.

**Preview.** Chess is not playable on Chipzen yet. This module follows the
chess Layer 2 dialect (``CHESS-GAME-STATE-PROTOCOL.md``, a draft) and Chipzen's
chess engine (rule set ``chess-1.0``, standard chess). The dialect is still a
draft, so field names may change before chess goes live. See
``docs/CHESS-BOTS.md``.

Chess is turn-based: each step asks only the seat whose colour is to move, with
one call, ``move``. Every ``turn_request`` carries everything a bot needs to
play legally with no chess library (game-interface spec 4.5):

- ``valid_actions`` is always ``["move"]``;
- ``state.legal_moves`` lists every legal move in UCI notation (``e2e4``,
  ``e7e8q``, castling as the king's two-square move ``e1g1``), sorted;
- ``state.start_fen`` and ``state.moves`` give the whole game so far, and
  ``state.fen`` the position now;
- ``state.side`` is the colour this seat plays (``"white"`` or ``"black"``).
  A seat number is not a colour;
- ``state.clock`` holds the public clocks (:class:`ChessClock`).

The answer is ``{"action": "move", "params": {"move": "<uci>"}}``, built with
:meth:`chipzen.Action.move`.

**This SDK has no chess rules.** It never works out which moves are legal; it
only reads the list the platform sends. Chipzen's chess engine runs on the
platform only.

These parsers are strict, like the Big Bad Bots ones: a client that declares
``chess`` in ``supported_games`` asserts it implements this dialect, so a
missing or mistyped required field raises :class:`ChessStateParseError`. Keys
this release does not know are ignored (LAYER2-COMMON section 3, rule 5).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from chipzen.bigbadbots import (
    StateParseError,
    _int,
    _int_list,
    _list,
    _object,
    _str,
    _str_list,
    _to_wire,
)

#: The ``game.state_shape`` (and ``game_type``) of a chess table.
STATE_SHAPE = "chess"

#: The one call, and its one action string (game-interface spec 4.5).
MOVE_ACTION = "move"

#: The ``state.phase`` of every move request.
MOVE_PHASE = "move"

#: The colours, as ``state.side`` names them. White moves first.
SIDES = ("white", "black")

#: The ``state.variant`` values this SDK release parses (the rule set's own
#: ``variant`` key). Fog of War is a separate game, ``fogofwar``, with its own
#: dialect, and is not supported yet.
SUPPORTED_VARIANTS = ("standard",)

#: ``match_start.game_config.variant`` at a chess table: the dialect's name.
#: Not the rule set's ``variant`` (``"standard"``), which is in ``rule_set``.
DIALECT = "chess"

#: The most answers the platform reads for one request: the first answer and
#: two retries after ``action_rejected``. A 3rd rejected answer stands at once,
#: as if time had run out: a miss, which loses on time (TRANSPORT-PROTOCOL
#: section 8.11).
MAX_ANSWERS_PER_REQUEST = 3

#: The machine codes the platform may send in ``action_rejected.details.code``
#: for a chess answer (game-interface spec 4.3 and 6.4):
#:
#: - ``bad_shape``: the ``turn_action`` is not ``{"action", "params"}`` with a
#:   ``params.move`` string;
#: - ``not_in_valid_actions``: ``action`` is not ``"move"``;
#: - ``bad_param``: ``params.move`` is not a UCI move string;
#: - ``not_in_legal_set``: a UCI string, but not one of ``state.legal_moves``.
REJECTION_CODES = ("bad_shape", "not_in_valid_actions", "bad_param", "not_in_legal_set")

#: The shape of a UCI move: from-square, to-square, optional promotion piece.
#: A shape check only; whether a move is legal is the platform's call, and
#: ``state.legal_moves`` is the list of legal ones.
UCI_MOVE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")

#: The Layer 1 ``turn_request`` envelope fields a :class:`ChessState` carries.
_ENVELOPE = ("request_id", "seat", "timeout_ms", "valid_actions")


class ChessStateParseError(StateParseError):
    """A chess payload is missing a required field, or one is mistyped."""


def is_uci_move(text: object) -> bool:
    """Whether ``text`` has the shape of a UCI move (``e2e4``, ``e7e8q``).

    A shape check, not a legality check: ``"a1h8"`` has the shape. Use
    ``state.legal_moves`` to know whether a move is legal.
    """
    return isinstance(text, str) and UCI_MOVE.fullmatch(text) is not None


@dataclass(frozen=True, slots=True)
class ChessClock:
    """``state.clock``: the public clocks (game-interface spec 5.6).

    Each move has a free window; time beyond it is paid from the seat's bank,
    which is never refilled. A move later than ``window_ms + my_bank_ms`` loses
    on time. The values are the banks before this move.

    Attributes:
        window_ms: The free time this move gets before the bank is charged
            (1000 in ``chess-1.0``).
        my_bank_ms: This seat's bank left.
        opp_bank_ms: The opponent's bank left.
    """

    window_ms: int
    my_bank_ms: int
    opp_bank_ms: int

    @property
    def budget_ms(self) -> int:
        """The whole time this move may take: ``window_ms + my_bank_ms``."""
        return self.window_ms + self.my_bank_ms

    def to_dict(self) -> dict[str, Any]:
        """Return the wire object."""
        return {
            "window_ms": self.window_ms,
            "my_bank_ms": self.my_bank_ms,
            "opp_bank_ms": self.opp_bank_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "clock") -> ChessClock:
        return cls(
            window_ms=_int(data, "window_ms", where),
            my_bank_ms=_int(data, "my_bank_ms", where),
            opp_bank_ms=_int(data, "opp_bank_ms", where),
        )


@dataclass(frozen=True, slots=True)
class ChessGameConfig:
    """``match_start.game_config`` at a chess table.

    Attributes:
        variant: ``"chess"``, the dialect (:data:`DIALECT`). The rule set's
            own ``variant`` (``"standard"``) is in :attr:`rule_set`.
        rule_set_id: The rule set this match is pinned to: ``"chess-1.0"``,
            or ``"chess-armageddon-1.0"`` for a bracket's tie-break game.
        num_players: Always ``2``.
        rule_set: The rule set's full table as the server sent it (start
            position, clock, draw rules, ply cap). Read numbers from here or
            from each request's ``clock``; never hard-code them.
        engine_version: The chess engine release that runs the rule set
            (``"1.0.0"``), or ``None`` when the server did not send it.
    """

    variant: str
    rule_set_id: str
    num_players: int
    rule_set: dict[str, Any]
    engine_version: str | None = None

    def _rule_int(self, key: str) -> int | None:
        value = self.rule_set.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def window_ms(self) -> int | None:
        """The free window per move (``rule_set.window_ms``, 1000 in ``chess-1.0``)."""
        return self._rule_int("window_ms")

    @property
    def bank_ms(self) -> int | None:
        """Each seat's bank at the start, never refilled (``rule_set.bank_ms``, 60000)."""
        return self._rule_int("bank_ms")

    @property
    def ply_cap(self) -> int | None:
        """The ply count at which the game is drawn (``rule_set.ply_cap``, 300)."""
        return self._rule_int("ply_cap")

    def to_dict(self) -> dict[str, Any]:
        """Return the wire object (``engine_version`` only when known)."""
        out = {f.name: _to_wire(getattr(self, f.name)) for f in dataclasses.fields(self)}
        if out["engine_version"] is None:
            del out["engine_version"]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "game_config") -> ChessGameConfig:
        try:
            return cls(
                variant=_str(data, "variant", where),
                rule_set_id=_str(data, "rule_set_id", where),
                num_players=_int(data, "num_players", where),
                rule_set=_object(data, "rule_set", where),
                engine_version=(
                    _str(data, "engine_version", where) if "engine_version" in data else None
                ),
            )
        except ChessStateParseError:
            raise
        except StateParseError as exc:
            raise ChessStateParseError(str(exc)) from exc

    @classmethod
    def from_match_start(cls, message: Mapping[str, Any]) -> ChessGameConfig:
        """Parse the ``game_config`` of a ``match_start`` (or ``reconnected``) message."""
        try:
            config = _object(message, "game_config", "match_start")
        except StateParseError as exc:
            raise ChessStateParseError(str(exc)) from exc
        return cls.from_dict(config)


@dataclass(frozen=True, slots=True)
class ChessState:
    """One ``move`` request (standard chess).

    Envelope attributes (Layer 1):
        request_id: Echo it in the ``turn_action`` (the SDK does).
        seat: The receiving seat. **Not your colour**: read :attr:`side`.
        timeout_ms: The whole time the server waits for this answer:
            ``clock.window_ms + clock.my_bank_ms``.
        valid_actions: Always ``["move"]``.

    State attributes (Layer 2):
        phase: Always ``"move"``.
        board, your_hole_cards: Always ``[]``; chess has no cards.
        pot, to_call, min_raise, max_raise, your_stack: Always ``0``.
        opponent_stacks: Always ``[0]``.
        call: Always ``"move"``.
        side: ``"white"`` or ``"black"``: the colour you play this game.
        variant: ``"standard"``.
        ply: Plies played so far (0 before White's first move).
        start_fen: The start position in FEN, even when it is the standard one.
        moves: Every move played from the start, in UCI, in order.
        fen: The position now, in FEN.
        legal_moves: Every legal move now, in UCI, sorted. Answer with one of
            these: anything else is rejected.
        clock: The public clocks (:class:`ChessClock`).

    Match context (from ``match_start``, not from the ``turn_request``):
        game_config: The match's :class:`ChessGameConfig`, filled in by
            :class:`chipzen.ChessBot` when it saw ``match_start``.
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
    moves: list[str]
    fen: str
    legal_moves: list[str]
    clock: ChessClock
    game_config: ChessGameConfig | None = dataclasses.field(
        default=None, kw_only=True, compare=False, repr=False
    )

    @property
    def own_moves_left(self) -> int | None:
        """How many more moves this seat can make before the ply cap draws the game.

        Counts this move. ``None`` when the ply cap is not known (no
        ``game_config``, or a rule set without ``ply_cap``).
        """
        cap = self.game_config.ply_cap if self.game_config is not None else None
        if cap is None:
            return None
        return max((cap - self.ply + 1) // 2, 0)

    @property
    def is_white(self) -> bool:
        """Whether this seat plays White this game."""
        return self.side == "white"

    @property
    def my_moves(self) -> list[str]:
        """This seat's own moves so far, in order (from :attr:`moves`)."""
        return self.moves[0::2] if self.side == self.first_side else self.moves[1::2]

    @property
    def opponent_moves(self) -> list[str]:
        """The opponent's moves so far, in order (from :attr:`moves`)."""
        return self.moves[1::2] if self.side == self.first_side else self.moves[0::2]

    @property
    def first_side(self) -> str:
        """The side that moved first from :attr:`start_fen` (``w`` or ``b`` in its second field)."""
        fields = self.start_fen.split()
        return "black" if len(fields) > 1 and fields[1] == "b" else "white"

    def to_state(self) -> dict[str, Any]:
        """Return the ``turn_request.state`` payload this state was parsed from."""
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name in _ENVELOPE or f.name == "game_config":
                continue
            value = getattr(self, f.name)
            out[f.name] = value.to_dict() if isinstance(value, ChessClock) else _to_wire(value)
        return out


def parse_chess_turn_request(message: Mapping[str, Any]) -> ChessState:
    """Parse a chess ``turn_request`` into a :class:`ChessState`.

    Raises:
        ChessStateParseError: ``state`` is missing, a required field is
            missing or mistyped, or the variant is not one this release
            parses (Fog of War is not supported yet).
    """
    where = "turn_request.state"
    try:
        state = _object(message, "state", "turn_request")
        variant = _str(state, "variant", where)
        if variant not in SUPPORTED_VARIANTS:
            raise ChessStateParseError(
                f"{where}.variant: {variant!r} is not supported by this SDK release "
                f"(expected one of {SUPPORTED_VARIANTS})"
            )
        side = _str(state, "side", where)
        if side not in SIDES:
            raise ChessStateParseError(f"{where}.side: expected 'white' or 'black', got {side!r}")
        return ChessState(
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
            moves=_str_list(state, "moves", where),
            fen=_str(state, "fen", where),
            legal_moves=_str_list(state, "legal_moves", where),
            clock=ChessClock.from_dict(_object(state, "clock", where), f"{where}.clock"),
        )
    except ChessStateParseError:
        raise
    except StateParseError as exc:
        raise ChessStateParseError(str(exc)) from exc


def first_legal_move(request: Mapping[str, Any]) -> str | None:
    """The first move of a raw ``turn_request``'s ``state.legal_moves``, or ``None``.

    Works on the raw message, so it still answers when the payload failed to
    parse. Membership in the list the platform sent, nothing more: the SDK
    never works out legality itself.
    """
    state = request.get("state")
    if not isinstance(state, Mapping):
        return None
    legal = state.get("legal_moves")
    if isinstance(legal, list):
        for move in legal:
            if is_uci_move(move):
                return str(move)
    return None


__all__ = [
    "DIALECT",
    "MAX_ANSWERS_PER_REQUEST",
    "MOVE_ACTION",
    "MOVE_PHASE",
    "REJECTION_CODES",
    "SIDES",
    "STATE_SHAPE",
    "SUPPORTED_VARIANTS",
    "ChessClock",
    "ChessGameConfig",
    "ChessState",
    "ChessStateParseError",
    "first_legal_move",
    "is_uci_move",
    "parse_chess_turn_request",
]
