"""Typed models for the Big Bad Bots Layer 2 dialect (``state_shape: "bigbadbots"``).

A Big Bad Bots match is two stages, gear-up (body shop, auction, fitting) and
the fight, and every step asks both seats at once. Each step's
``turn_request.state`` has its own shape, so each gets its own class, picked
by ``state.phase``:

- ``choose_body`` (phase ``body_shop``): :class:`BodyShopState`
- ``bid`` (phase ``auction``, once per lot): :class:`AuctionState`
- ``fit`` (phase ``fitting``): :class:`FittingState`
- ``act`` (phase ``fight``, once per tick): :class:`FightState`

The answers are built with :meth:`chipzen.Action.choose_body`,
:meth:`~chipzen.Action.bid`, :meth:`~chipzen.Action.fit` and
:meth:`~chipzen.Action.act`.

**These parsers are strict, on purpose.** The poker ``GameState`` parses its
variant keys leniently because an NLHE bot never asked for them. A client that
declares ``bigbadbots`` in ``supported_games`` asserts it implements this
dialect, and every field below is marked required by the spec, so a missing or
mistyped field raises :class:`StateParseError` instead of reaching the bot as a
silent default. Keys this release does not know are ignored: the spec only ever
adds keys (LAYER2-COMMON section 3, rule 5).

See ``docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`` for the authoritative
spec. Section numbers below refer to it.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Union

if TYPE_CHECKING:
    from chipzen.bigbadbots_rules import RuleSet

#: The ``game.state_shape`` (and ``game_type`` and ``variant``) of a Big Bad
#: Bots table (section 1.1).
STATE_SHAPE = "bigbadbots"

#: The gear-up calls; each is also its answer's ``action`` string (section 3.1).
GEARUP_ACTIONS = ("choose_body", "bid", "fit")

#: The ten fight actions, every one of them in every ``act`` request's
#: ``valid_actions`` (section 4.3.4).
FIGHT_ACTIONS = (
    "idle",
    "move_in",
    "move_out",
    "block",
    "punch_high",
    "uppercut",
    "kick_high",
    "kick_low",
    "throw",
    "special",
)

#: The four ``state.phase`` strings, in match order.
PHASES = ("body_shop", "auction", "fitting", "fight")

#: The Layer 1 ``turn_request`` envelope fields a state model carries. Every
#: other field of a state model is a key of ``turn_request.state``, except
#: ``rules``, which comes from ``match_start``.
_ENVELOPE = ("request_id", "seat", "timeout_ms", "valid_actions")


class StateParseError(ValueError):
    """A Big Bad Bots payload is missing a required field, or one is mistyped."""


# ---------------------------------------------------------------------------
# Strict field readers
# ---------------------------------------------------------------------------
#
# Section 4: every integer is a JSON integer (``12.0`` is not one) and every
# float is finite. ``bool`` is a subclass of ``int`` in Python, so each reader
# rules it out explicitly.


def _require(obj: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise StateParseError(f"{where}: missing required field {key!r}")
    return obj[key]


def _fail(where: str, key: str, expected: str, value: Any) -> StateParseError:
    return StateParseError(f"{where}.{key}: expected {expected}, got {value!r}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _int(obj: Mapping[str, Any], key: str, where: str) -> int:
    value = _require(obj, key, where)
    if not _is_int(value):
        raise _fail(where, key, "an integer", value)
    return value


def _opt_int(obj: Mapping[str, Any], key: str, where: str) -> int | None:
    value = _require(obj, key, where)
    if value is not None and not _is_int(value):
        raise _fail(where, key, "an integer or null", value)
    return value


def _number(obj: Mapping[str, Any], key: str, where: str) -> float:
    value = _require(obj, key, where)
    if not _is_number(value):
        raise _fail(where, key, "a finite number", value)
    return value


def _str(obj: Mapping[str, Any], key: str, where: str) -> str:
    value = _require(obj, key, where)
    if not isinstance(value, str):
        raise _fail(where, key, "a string", value)
    return value


def _opt_str(obj: Mapping[str, Any], key: str, where: str) -> str | None:
    value = _require(obj, key, where)
    if value is not None and not isinstance(value, str):
        raise _fail(where, key, "a string or null", value)
    return value


def _bool(obj: Mapping[str, Any], key: str, where: str) -> bool:
    value = _require(obj, key, where)
    if not isinstance(value, bool):
        raise _fail(where, key, "a boolean", value)
    return value


def _list(obj: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = _require(obj, key, where)
    if not isinstance(value, list):
        raise _fail(where, key, "an array", value)
    return list(value)


def _object(obj: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    value = _require(obj, key, where)
    if not isinstance(value, Mapping):
        raise _fail(where, key, "an object", value)
    return dict(value)


def _str_list(obj: Mapping[str, Any], key: str, where: str) -> list[str]:
    items = _list(obj, key, where)
    if not all(isinstance(item, str) for item in items):
        raise _fail(where, key, "an array of strings", items)
    return items


def _int_list(obj: Mapping[str, Any], key: str, where: str) -> list[int]:
    items = _list(obj, key, where)
    if not all(_is_int(item) for item in items):
        raise _fail(where, key, "an array of integers", items)
    return items


def _number_list(obj: Mapping[str, Any], key: str, where: str) -> list[float]:
    items = _list(obj, key, where)
    if not all(_is_number(item) for item in items):
        raise _fail(where, key, "an array of finite numbers", items)
    return items


def _number_map(obj: Mapping[str, Any], key: str, where: str) -> dict[str, float]:
    mapping = _object(obj, key, where)
    if not all(_is_number(value) for value in mapping.values()):
        raise _fail(where, key, "an object of finite numbers", mapping)
    return mapping


def _objects(obj: Mapping[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    items = _list(obj, key, where)
    if not all(isinstance(item, Mapping) for item in items):
        raise _fail(where, key, "an array of objects", items)
    return [dict(item) for item in items]


def _to_wire(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_wire(item) for item in value]
    if isinstance(value, dict):
        return dict(value)
    return value


class _WireModel:
    """``to_dict`` for the payload objects: the wire form they were parsed from."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        """Return this object as its wire JSON object."""
        return {f.name: _to_wire(getattr(self, f.name)) for f in dataclasses.fields(self)}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payload objects (section 4.3.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Body(_WireModel):
    """A body on this seat's offer, in the body shop.

    Attributes:
        id: The body's id.
        cost: Credits it costs.
        stats: Attribute name to base value.
        slots: The body's part slots, by slot type. A type can repeat.
        special: The id of the body's special move.
    """

    id: str
    cost: int
    stats: dict[str, float]
    slots: list[str]
    special: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "Body") -> Body:
        return cls(
            id=_str(data, "id", where),
            cost=_int(data, "cost", where),
            stats=_number_map(data, "stats", where),
            slots=_str_list(data, "slots", where),
            special=_str(data, "special", where),
        )


@dataclass(frozen=True, slots=True)
class Part(_WireModel):
    """A body-mod part: the lot on sale, or one this seat owns.

    Attributes:
        id: The part's id.
        slot: The slot type it fits.
        bonus: Attribute name to the amount it adds (negative subtracts).
        effects: Effect name to a number, for parts that change a rule rather
            than an attribute. Empty for most parts.
    """

    id: str
    slot: str
    bonus: dict[str, float]
    effects: dict[str, float]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "Part") -> Part:
        return cls(
            id=_str(data, "id", where),
            slot=_str(data, "slot", where),
            bonus=_number_map(data, "bonus", where),
            effects=_number_map(data, "effects", where),
        )


@dataclass(frozen=True, slots=True)
class Side(_WireModel):
    """One seat's gear-up state: ``me`` in full, or ``opp`` masked (section 5).

    Attributes:
        credits: Credits left. Public for both seats.
        offer: This seat's offer, by body id. Always ``[]`` for ``opp``.
        body: The chosen body, or ``None`` in the body shop.
        owned: Parts won, by id. Public for both seats.
        fitted: One ``None`` per slot of the chosen body (``[]`` before one is
            chosen), throughout gear-up. The opponent's fitting is never shown.
    """

    credits: int
    offer: list[str]
    body: str | None
    owned: list[str]
    fitted: list[None]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "Side") -> Side:
        fitted = _list(data, "fitted", where)
        if not all(slot is None for slot in fitted):
            raise _fail(where, "fitted", "an array of null", fitted)
        return cls(
            credits=_int(data, "credits", where),
            offer=_str_list(data, "offer", where),
            body=_opt_str(data, "body", where),
            owned=_str_list(data, "owned", where),
            fitted=fitted,
        )


@dataclass(frozen=True, slots=True)
class SoldLot(_WireModel):
    """One entry of ``history``: a lot already sold, seat-relative.

    Attributes:
        part: The part that was sold.
        my_bid: This seat's bid, as applied (after clamping).
        opp_bid: The opponent's bid, as applied.
        winner: ``"me"``, ``"opp"``, or ``None`` when both bid 0 and the lot
            went unsold.
    """

    part: str
    my_bid: int
    opp_bid: int
    winner: Literal["me", "opp"] | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "SoldLot") -> SoldLot:
        winner = _opt_str(data, "winner", where)
        if winner not in ("me", "opp", None):
            raise _fail(where, "winner", '"me", "opp" or null', winner)
        return cls(
            part=_str(data, "part", where),
            my_bid=_int(data, "my_bid", where),
            opp_bid=_int(data, "opp_bid", where),
            winner=winner,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Fighter(_WireModel):
    """One fighter in the fight: ``me`` in full, or ``opp`` masked (section 5).

    While the opponent winds up a move, it shows as ``action == "attack"`` in
    ``phase == "startup"`` with ``frames_left`` ``None``, and its
    ``stamina``, ``special_used``, ``last_attack``, ``repeats`` and ``buffs``
    keep their values from before the move started.

    Attributes:
        body: Body id.
        special: Special move id.
        x: Position on the arena.
        hp: Hit points now.
        max_hp: The round's maximum hit points.
        stamina: Stamina now.
        max_stamina: The round's maximum stamina.
        action: A move or special id, ``"hitstun"``, ``"dazed"``, ``"attack"``
            (the opponent winding up), or ``None`` when free.
        phase: ``"startup"``, ``"active"``, ``"recovery"``, ``"hitstun"``,
            ``"dazed"``, or ``None`` when free.
        frames_left: Ticks left in the current phase; ``None`` for the
            opponent during a wind-up.
        special_used: Whether the special has been used this round.
        last_attack: The last attack started, for the repeated-move penalty.
        repeats: How many times in a row ``last_attack`` has been used.
        strike_range: The fighter's strike range, from its Reach.
        move_rate: Distance per tick when moving, from its Speed.
        attrs: The six attributes after fitting and live buffs.
        buffs: Active effect name to what is left of it: ticks for a timed
            effect, strikes for a per-strike one.
    """

    body: str
    special: str
    x: float
    hp: float
    max_hp: float
    stamina: float
    max_stamina: float
    action: str | None
    phase: str | None
    frames_left: int | None
    special_used: bool
    last_attack: str | None
    repeats: int
    strike_range: float
    move_rate: float
    attrs: dict[str, float]
    buffs: dict[str, float]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "Fighter") -> Fighter:
        return cls(
            body=_str(data, "body", where),
            special=_str(data, "special", where),
            x=_number(data, "x", where),
            hp=_number(data, "hp", where),
            max_hp=_number(data, "max_hp", where),
            stamina=_number(data, "stamina", where),
            max_stamina=_number(data, "max_stamina", where),
            action=_opt_str(data, "action", where),
            phase=_opt_str(data, "phase", where),
            frames_left=_opt_int(data, "frames_left", where),
            special_used=_bool(data, "special_used", where),
            last_attack=_opt_str(data, "last_attack", where),
            repeats=_int(data, "repeats", where),
            strike_range=_number(data, "strike_range", where),
            move_rate=_number(data, "move_rate", where),
            attrs=_number_map(data, "attrs", where),
            buffs=_number_map(data, "buffs", where),
        )


# ---------------------------------------------------------------------------
# Game config (section 4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BigBadBotsGameConfig(_WireModel):
    """``match_start.game_config`` at a Big Bad Bots table.

    Attributes:
        variant: Always ``"bigbadbots"``.
        rule_set_id: The rule set this match is pinned to, e.g.
            ``"bigbadbots-1.0"``. Fixed for the whole match.
        num_players: Always ``2``.
        rule_set: The rule set's full parameter table, by section
            (``gearup``, ``arena``, ``rounds``, ``forfeit``, ``deadline``, the
            bodies, parts, moves and specials). Held as the raw wire object
            because its sections vary by rule set. **Read the numbers from
            here** (a :class:`chipzen.BigBadBotsBot` also hands them to every
            call, typed and read-only, as ``state.rules``); never hard-code
            them. Rule sets are versioned; see
            https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
    """

    variant: str
    rule_set_id: str
    num_players: int
    rule_set: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "game_config") -> BigBadBotsGameConfig:
        variant = _str(data, "variant", where)
        if variant != STATE_SHAPE:
            raise _fail(where, "variant", repr(STATE_SHAPE), variant)
        return cls(
            variant=variant,
            rule_set_id=_str(data, "rule_set_id", where),
            num_players=_int(data, "num_players", where),
            rule_set=_object(data, "rule_set", where),
        )

    @classmethod
    def from_match_start(cls, message: Mapping[str, Any]) -> BigBadBotsGameConfig:
        """Parse the ``game_config`` of a ``match_start`` message."""
        return cls.from_dict(_object(message, "game_config", "match_start"))


# ---------------------------------------------------------------------------
# Turn request states (section 4.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BigBadBotsState:
    """What every Big Bad Bots ``turn_request`` carries, whatever the call.

    Envelope attributes (Layer 1):
        request_id: Echo it in the ``turn_action``.
        seat: The receiving seat. At a simultaneous step both seats are the
            seat whose turn it is.
        timeout_ms: The whole time the server waits for this answer. In the
            fight that is the per-tick deadline plus what is left of this
            seat's time bank; ``timeout_ms - rule_set.deadline.deadline_ms``
            is the bank.
        valid_actions: The call's legal ``action`` strings (section 3.1).

    State attributes (Layer 2):
        phase: ``"body_shop"``, ``"auction"``, ``"fitting"`` or ``"fight"``.
        board, your_hole_cards: Always ``[]``; Big Bad Bots has no cards.
        pot, to_call, min_raise, max_raise, your_stack: Always ``0``; Big Bad
            Bots has no chips. Credits are on ``me.credits`` / ``opp.credits``.
        opponent_stacks: Always ``[0]``.

    Match context (from ``match_start``, not from the ``turn_request``):
        rules: The rule set this match is played under, typed and read-only
            (:class:`chipzen.bigbadbots_rules.RuleSet`): its id
            (``rules.id``) and every number it plays by. A
            :class:`chipzen.BigBadBotsBot` fills it in on every call, in every
            phase; it is ``None`` only when no ``match_start`` was seen (or an
            ``on_match_start`` override did not call ``super()``). Read the
            numbers from here and the bot runs unchanged on a new rule set.
            See https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
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
    rules: RuleSet | None = dataclasses.field(default=None, kw_only=True, compare=False, repr=False)

    def to_state(self) -> dict[str, Any]:
        """Return the ``turn_request.state`` payload this state was parsed from."""
        return {
            f.name: _to_wire(getattr(self, f.name))
            for f in dataclasses.fields(self)
            if f.name not in _ENVELOPE and f.name != "rules"
        }

    @staticmethod
    def _common(message: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
        where = "turn_request.state"
        return {
            "request_id": _str(message, "request_id", "turn_request"),
            "seat": _int(message, "seat", "turn_request"),
            "timeout_ms": _int(message, "timeout_ms", "turn_request"),
            "valid_actions": _str_list(message, "valid_actions", "turn_request"),
            "phase": _str(state, "phase", where),
            "board": _list(state, "board", where),
            "your_hole_cards": _list(state, "your_hole_cards", where),
            "pot": _int(state, "pot", where),
            "to_call": _int(state, "to_call", where),
            "min_raise": _int(state, "min_raise", where),
            "max_raise": _int(state, "max_raise", where),
            "your_stack": _int(state, "your_stack", where),
            "opponent_stacks": _int_list(state, "opponent_stacks", where),
        }


@dataclass(frozen=True, slots=True)
class GearUpState(BigBadBotsState):
    """What the three gear-up calls share (sections 4.3.1 to 4.3.3).

    Attributes:
        me: This seat's side, in full.
        opp: The opponent's side, masked: ``offer`` is always ``[]`` and
            ``fitted`` never shows a part.
        lot_index: 0-based index of the lot being sold; ``0`` in the body shop
            and ``lots_total`` once the auction is over.
        lots_total: How many lots the auction sells.
        history: Every lot already sold, in order, with both bids.
    """

    me: Side
    opp: Side
    lot_index: int
    lots_total: int
    history: list[SoldLot]

    @staticmethod
    def _gearup(state: Mapping[str, Any]) -> dict[str, Any]:
        where = "turn_request.state"
        return {
            "me": Side.from_dict(_object(state, "me", where), f"{where}.me"),
            "opp": Side.from_dict(_object(state, "opp", where), f"{where}.opp"),
            "lot_index": _int(state, "lot_index", where),
            "lots_total": _int(state, "lots_total", where),
            "history": [
                SoldLot.from_dict(lot, f"{where}.history[{i}]")
                for i, lot in enumerate(_objects(state, "history", where))
            ],
        }


@dataclass(frozen=True, slots=True)
class BodyShopState(GearUpState):
    """The ``choose_body`` call (phase ``body_shop``, section 4.3.1).

    Answer with :meth:`chipzen.Action.choose_body`.

    Attributes:
        offer: This seat's own offer, in full. The opponent's is never shown.
    """

    offer: list[Body]


@dataclass(frozen=True, slots=True)
class AuctionState(GearUpState):
    """One ``bid`` call (phase ``auction``, section 4.3.2).

    Answer with :meth:`chipzen.Action.bid`. Future lots are never shown, and
    neither is the opponent's bid on this one.

    Attributes:
        lot: The part being sold now.
    """

    lot: Part


@dataclass(frozen=True, slots=True)
class FittingState(GearUpState):
    """The ``fit`` call (phase ``fitting``, section 4.3.3).

    Answer with :meth:`chipzen.Action.fit`, naming slots by their index into
    ``slots``.

    Attributes:
        owned: Every part this seat won, in full, including more than its
            slots hold and parts for slot types its body lacks.
        slots: This seat's body's slot types, by slot index.
    """

    owned: list[Part]
    slots: list[str]


@dataclass(frozen=True, slots=True)
class FightState(BigBadBotsState):
    """One ``act`` call: one fight tick (phase ``fight``, section 4.3.4).

    Answer with :meth:`chipzen.Action.act`. ``valid_actions`` is always all
    ten fight actions; whether one can be performed now is decided when the
    tick resolves.

    Attributes:
        tick: The tick within the round, 0-based.
        round: The fight round, 1-based.
        rounds_won: ``[mine, opponent's]``.
        me: This seat's fighter, in full.
        opp: The opponent's fighter, masked (section 5).
        distance: The distance between the two fighters, in arena units.
        facing: ``1`` if the opponent is to this seat's right, ``-1`` if to
            its left.
        walls: ``[left, right]``: where the arena's walls stand this tick.
    """

    tick: int
    round: int
    rounds_won: list[int]
    me: Fighter
    opp: Fighter
    distance: float
    facing: int
    walls: list[float]


#: Any parsed Big Bad Bots ``turn_request``. Branch on the class, or on
#: ``phase``.
BigBadBotsTurnState = Union[BodyShopState, AuctionState, FittingState, FightState]


def parse_bigbadbots_turn_request(message: Mapping[str, Any]) -> BigBadBotsTurnState:
    """Parse a Big Bad Bots ``turn_request`` into the model for its call.

    Routes on ``state.phase`` (section 3.1).

    Raises:
        StateParseError: ``state`` is missing, its ``phase`` is not one of the
            four Big Bad Bots phases, or a required field is missing or
            mistyped.
    """
    state = _object(message, "state", "turn_request")
    where = "turn_request.state"
    common = BigBadBotsState._common(message, state)
    phase = common["phase"]

    if phase == "fight":
        return FightState(
            **common,
            tick=_int(state, "tick", where),
            round=_int(state, "round", where),
            rounds_won=_int_list(state, "rounds_won", where),
            me=Fighter.from_dict(_object(state, "me", where), f"{where}.me"),
            opp=Fighter.from_dict(_object(state, "opp", where), f"{where}.opp"),
            distance=_number(state, "distance", where),
            facing=_int(state, "facing", where),
            walls=_number_list(state, "walls", where),
        )
    if phase == "body_shop":
        return BodyShopState(
            **common,
            **GearUpState._gearup(state),
            offer=[
                Body.from_dict(body, f"{where}.offer[{i}]")
                for i, body in enumerate(_objects(state, "offer", where))
            ],
        )
    if phase == "auction":
        return AuctionState(
            **common,
            **GearUpState._gearup(state),
            lot=Part.from_dict(_object(state, "lot", where), f"{where}.lot"),
        )
    if phase == "fitting":
        return FittingState(
            **common,
            **GearUpState._gearup(state),
            owned=[
                Part.from_dict(part, f"{where}.owned[{i}]")
                for i, part in enumerate(_objects(state, "owned", where))
            ],
            slots=_str_list(state, "slots", where),
        )
    raise StateParseError(
        f"{where}.phase: {phase!r} is not a Big Bad Bots phase (expected one of {PHASES})"
    )
