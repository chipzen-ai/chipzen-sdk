"""Data models for the Chipzen poker SDK.

Clean dataclasses that represent the game state your bot receives
and the actions it sends back.

Wire-format mapping for the two-layer protocol:

- Layer 1 (Transport) carries game-agnostic envelope fields (``type``,
  ``match_id``, ``seq``, ``server_ts``, ``request_id``, ``round_id``, ...).
- Layer 2 (Poker) defines the game-specific payloads nested inside Layer 1
  messages such as ``turn_request.state``, ``round_start.state``,
  ``turn_result.details`` and ``round_result.result``.

See ``docs/protocol/TRANSPORT-PROTOCOL.md`` and
``docs/protocol/POKER-GAME-STATE-PROTOCOL.md`` for the authoritative spec.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Lenient coercion helpers for OPTIONAL variant keys
# ---------------------------------------------------------------------------
#
# ``board`` and ``your_hole_cards`` are parsed strictly (``Card.from_str``
# raises) because the Layer 2 specs make them a hard contract: every element is
# a valid two-character card string, always. That strictness is a documented
# hazard -- it fires inside ``from_turn_request``, BEFORE ``decide()`` runs, so
# a malformed payload kills the session rather than one decision.
#
# The variant keys below are parsed leniently on purpose: they are NEW,
# OPTIONAL keys, and a second hard-failure surface would turn "the server sent
# a field shape I did not expect" into "every deployed bot at this table dies".
# A missing or mistyped optional key degrades to its default instead. This is a
# deliberate asymmetry, not an oversight.


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a wire value to ``int``, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce a wire value to ``bool``, falling back to ``default``."""
    return value if isinstance(value, bool) else default


def _as_list(value: Any) -> list:
    """Return ``value`` as a list, or ``[]`` if it is not list-shaped."""
    return list(value) if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    """Return ``value`` as a dict, or ``{}`` if it is not object-shaped."""
    return dict(value) if isinstance(value, dict) else {}


def _as_card_strs(value: Any) -> list[str]:
    """Return a list of raw card strings, unparsed.

    Variant payloads carry cards under several new keys (``cards_to_place``,
    ``your_rows``, ``opponent_rows``). They stay as wire strings rather than
    :class:`Card` objects so they add no new parse-time failure mode; call
    :meth:`Card.from_str` yourself when you want the typed form. The already
    Card-parsed copy of your own pending cards is ``GameState.hole_cards``.
    """
    return [str(c) for c in _as_list(value)]


def _as_int_list(value: Any) -> list[int]:
    return [_as_int(v) for v in _as_list(value)]


def _as_rows(value: Any) -> dict[str, list[str]]:
    """Parse a ``{row_name: [card, ...]}`` object (OFC rows / row contents)."""
    return {str(k): _as_card_strs(v) for k, v in _as_dict(value).items()}


def _as_int_map(value: Any) -> dict[str, int]:
    """Parse a ``{key: int}`` object (``row_capacity``, ``royalties``)."""
    return {str(k): _as_int(v) for k, v in _as_dict(value).items()}


@dataclass(frozen=True, slots=True)
class Card:
    """A playing card.

    Attributes:
        rank: One of "2"-"9", "T", "J", "Q", "K", "A".
        suit: One of "h" (hearts), "d" (diamonds), "c" (clubs), "s" (spades).
    """

    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"

    def __repr__(self) -> str:
        return f"Card('{self.rank}{self.suit}')"

    @classmethod
    def from_str(cls, s: str) -> Card:
        """Parse a card string like 'Ah', '2c', 'Td'."""
        if len(s) != 2:
            raise ValueError(f"Invalid card string: {s!r}")
        rank = s[0].upper()
        suit = s[1].lower()
        if rank not in "23456789TJQKA":
            raise ValueError(f"Invalid rank: {rank!r}")
        if suit not in "hdcs":
            raise ValueError(f"Invalid suit: {suit!r}")
        return cls(rank=rank, suit=suit)


@dataclass(frozen=True, slots=True)
class Player:
    """A player at the table.

    Attributes:
        seat: Seat index (0-based).
        stack: Current chip count.
        is_active: True if still in the hand (hasn't folded).
        is_all_in: True if the player is all-in.
    """

    seat: int
    stack: int
    is_active: bool = True
    is_all_in: bool = False


@dataclass(frozen=True, slots=True)
class Action:
    """An action to send to the server.

    Attributes:
        action: The action name. NLHE offers "fold", "check", "call",
                "raise" and "all_in"; variant tables add "draw" (2-7 Triple
                Draw) and "place" (Pineapple OFC). Held as a plain string
                rather than an enum so a table speaking a vocabulary this
                SDK release predates is still expressible.
        amount: Raise amount (total bet, not additional chips). Ignored for
                fold/check/call/all_in.
        params: Extra wire parameters, emitted verbatim under ``params`` by
                :meth:`to_wire`. Empty for every NLHE action -- ``raise``
                derives its ``params`` from ``amount``. Every variant action
                parameter travels here, because the server's field allowlist
                (``type``, ``action``, ``amount``, ``session_token``,
                ``match_id``, ``request_id``, ``params``) rejects a bespoke
                top-level key as ``UNEXPECTED_FIELDS`` before any rule set
                sees the message.
    """

    action: str
    amount: int = 0
    # ``hash=False`` keeps Action hashable: a dict field would otherwise be
    # folded into the frozen dataclass's generated __hash__ and raise.
    # Equality still compares params.
    params: dict = field(default_factory=dict, hash=False)

    def to_dict(self) -> dict:
        """Convert to the legacy flat wire format (action + amount).

        Use :meth:`to_wire` for the new two-layer ``turn_action`` format
        with a nested ``params`` object.
        """
        d: dict = {"action": self.action}
        if self.action == "raise":
            d["amount"] = self.amount
        if self.params:
            # The legacy flat format has no home for structured parameters,
            # but dropping them silently would turn a draw or a placement
            # into an empty action. Carry them rather than lose them.
            d["params"] = dict(self.params)
        return d

    def to_wire(self) -> dict:
        """Convert to the two-layer ``turn_action`` payload fields.

        Returns a dict with ``action`` (string) and ``params`` (object)
        suitable for inclusion in a ``turn_action`` message. Callers are
        expected to add the Layer 1 envelope fields (``type``, ``match_id``,
        ``request_id``).
        """
        if self.params:
            # Explicit params win: this is how every variant action
            # (``draw``, ``place``) carries its payload.
            return {"action": self.action, "params": dict(self.params)}
        if self.action == "raise":
            return {"action": "raise", "params": {"amount": self.amount}}
        # fold, check, call, all_in: no params required.
        return {"action": self.action, "params": {}}

    @classmethod
    def fold(cls) -> Action:
        return cls(action="fold")

    @classmethod
    def check(cls) -> Action:
        return cls(action="check")

    @classmethod
    def call(cls) -> Action:
        return cls(action="call")

    @classmethod
    def raise_to(cls, amount: int) -> Action:
        return cls(action="raise", amount=amount)

    @classmethod
    def all_in(cls) -> Action:
        return cls(action="all_in")

    # ------------------------------------------------------------------
    # Variant actions
    #
    # Additive: an NLHE bot never constructs these, and the NLHE factories
    # above are byte-unchanged on the wire.
    # ------------------------------------------------------------------

    @classmethod
    def discard(cls, cards: Sequence[str | int] | None = None) -> Action:
        """2-7 Triple Draw: the ``draw`` action.

        Args:
            cards: What to throw away -- card strings matched against your
                own holding (``["Ah", "Kd"]``), or 0-based positions into
                ``GameState.hole_cards``. At most ``GameState.max_discard``
                entries. ``None`` or ``[]`` is a **stand pat**.

        Emits ``{"action": "draw", "params": {"discard": [...]}}``. A stand
        pat is a real action, not a no-op: it appears in ``action_history``
        with ``amount: 0`` and it passes the turn.

        See ``docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md`` sections 3.5 and 5.5.
        """
        return cls(action="draw", params={"discard": [c for c in (cards or [])]})

    @classmethod
    def stand_pat(cls) -> Action:
        """2-7 Triple Draw: keep all five cards. Alias for ``discard([])``."""
        return cls.discard([])

    @classmethod
    def place(
        cls,
        placements: Sequence[tuple[str, str] | Mapping[str, str]],
        discard: str | Sequence[str] | None = None,
    ) -> Action:
        """Pineapple OFC: the ``place`` action -- the only action OFC offers.

        Args:
            placements: Exactly ``GameState.place`` ``(card, row)`` pairs (or
                ``{"card": ..., "row": ...}`` mappings), drawn from
                ``GameState.cards_to_place``. Row names are the keys of
                ``GameState.row_capacity``: ``"top"``, ``"middle"``,
                ``"bottom"``.
            discard: Exactly ``GameState.must_discard`` cards -- a single card
                string, a list, or ``None``/``[]`` on the opening set.

        Emits ``{"action": "place", "params": {"placements": [...],
        "discard": ...}}``.

        Unlike NLHE and 27TD, an illegal placement is **rejected, not
        clamped**: placement is irrevocable, so there is no defensible
        "nearest legal placement". Check ``row_capacity`` before submitting.

        See ``docs/protocol/OFC-GAME-STATE-PROTOCOL.md`` section 3.5.
        """
        wire_placements: list[dict] = []
        for entry in placements:
            if isinstance(entry, Mapping):
                wire_placements.append({"card": str(entry["card"]), "row": str(entry["row"])})
            else:
                card, row = entry
                wire_placements.append({"card": str(card), "row": str(row)})

        wire_discard: Any
        if discard is None:
            wire_discard = []
        elif isinstance(discard, str):
            wire_discard = discard
        else:
            wire_discard = [str(c) for c in discard]

        return cls(
            action="place",
            params={"placements": wire_placements, "discard": wire_discard},
        )


@dataclass(slots=True)
class GameState:
    """The game state your bot receives when it's time to act.

    Built from the server's ``turn_request`` message (Layer 1 envelope +
    Layer 2 ``state`` payload), enriched with context from ``round_start``
    (hole cards, dealer seat, stacks).

    Attributes:
        hand_number: Current hand number in the match.
        phase: One of "preflop", "flop", "turn", "river".
        hole_cards: Your two private cards.
        board: Community cards dealt so far.
        pot: Total chips in the pot.
        your_stack: Your remaining chip count.
        opponent_stacks: List of opponent stack sizes.
        your_seat: Your seat index.
        dealer_seat: Dealer button position.
        to_call: Chips needed to call (0 if you can check).
        min_raise: Minimum legal raise-to amount.
        max_raise: Maximum legal raise-to amount (your effective all-in).
        valid_actions: List of legal action type strings.
        action_history: Actions taken so far in this hand.
        round_id: Globally unique round (hand) identifier from Layer 1.
            Empty string if unknown (e.g., in local testing).
        request_id: The turn's ``request_id`` from Layer 1. Must be echoed
            by the client in the ``turn_action`` response. Empty string if
            unknown (e.g., in local testing).

    Variant fields (2-7 Triple Draw and Pineapple OFC) are listed on the
    dataclass below. **Every one of them is optional with a default**, so a
    bot written against NLHE sees exactly the state it always saw: at an NLHE
    table they all hold their defaults, and at a variant table an NLHE bot
    that never reads them behaves as it did before. Nothing above changes
    type or meaning at a variant table.
    """

    hand_number: int = 0
    phase: str = "preflop"
    hole_cards: list[Card] = field(default_factory=list)
    board: list[Card] = field(default_factory=list)
    pot: int = 0
    your_stack: int = 0
    opponent_stacks: list[int] = field(default_factory=list)
    your_seat: int = 0
    dealer_seat: int = 0
    to_call: int = 0
    min_raise: int = 0
    max_raise: int = 0
    valid_actions: list[str] = field(default_factory=list)
    action_history: list[dict] = field(default_factory=list)
    round_id: str = ""
    request_id: str = ""

    # ------------------------------------------------------------------
    # 2-7 Triple Draw (``game_config.variant == "27tripledraw"``)
    #
    # See docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md section 3.3. All default
    # to the "not a draw game" reading, so an NLHE bot is unaffected.
    # ------------------------------------------------------------------

    #: True iff this turn owes a DRAW rather than a betting decision. The
    #: single dial to branch on: a betting action in a draw phase (or a
    #: ``draw`` in a betting phase) is an error.
    is_draw_phase: bool = False
    #: 1-3 inside a draw round, 0 in every other phase.
    draw_number: int = 0
    #: Draw rounds still to come, counting one in progress. 3 during draw1.
    draws_remaining: int = 0
    #: Largest discard legal right now (5 in a draw phase, 0 outside one).
    #: The bound lives here, NOT in ``valid_actions`` -- which in a draw
    #: phase is exactly ``["draw"]``.
    max_discard: int = 0
    #: Your own per-round draw counts so far; a stand pat contributes 0.
    your_draw_counts: list[int] = field(default_factory=list)
    #: Seat index (as a DECIMAL STRING, because JSON object keys are strings)
    #: to that seat's per-round draw counts. Excludes you. Draw counts are
    #: public information; opponents' cards and discards never appear.
    opponent_draw_counts: dict[str, list[int]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Pineapple OFC (``game_config.variant == "pineapple"``)
    #
    # See docs/protocol/OFC-GAME-STATE-PROTOCOL.md section 3.3. Card-carrying
    # fields hold RAW wire strings, not Card objects -- see the note on
    # ``_as_card_strs``. There is no betting: ``to_call``, ``min_raise`` and
    # ``max_raise`` are 0 in every OFC payload, and ``pot`` is 0 until the
    # hand settles.
    # ------------------------------------------------------------------

    #: Your own three rows, by row name, unredacted. ``{"top": [...], ...}``.
    your_rows: dict[str, list[str]] = field(default_factory=dict)
    #: Seat index (decimal string) to that seat's rows. Public -- a placed
    #: card is visible the moment it is placed. A hidden Fantasy Land board
    #: reads as empty rows.
    opponent_rows: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    #: The cards this street brought you: 5 on the opening set, 3 on a
    #: pineapple street, 14 in Fantasy Land. Same cards as ``hole_cards``,
    #: under the name the ``place`` action uses.
    cards_to_place: list[str] = field(default_factory=list)
    #: How many of ``cards_to_place`` must be placed: 5 / 2 / 13. Per SEAT,
    #: not per phase -- a Fantasy Land seat places 13 in the same phase where
    #: its opponent places 5.
    place: int = 0
    #: How many must be discarded. ``place + must_discard`` always equals
    #: ``len(cards_to_place)``.
    must_discard: int = 0
    #: Free slots per row, by row name. An illegal placement is REJECTED,
    #: not clamped, so read this before submitting.
    row_capacity: dict[str, int] = field(default_factory=dict)
    #: Your live royalties per row. Only a complete row can pay.
    royalties: dict[str, int] = field(default_factory=dict)
    #: Seat index (decimal string) to that seat's live royalties per row.
    opponent_royalties: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Chips one point is worth this hand. OFC settles in points.
    point_value: int = 0
    #: Whether YOU are playing this hand in Fantasy Land.
    in_fantasy_land: bool = False
    #: The phases THIS SEAT walks -- ``["deal1", "complete"]`` for a Fantasy
    #: Land seat. A progress indicator must read this, not a match-level
    #: phase list.
    phase_sequence: list[str] = field(default_factory=list)

    @classmethod
    def from_action_request(
        cls,
        payload: dict,
        *,
        hole_cards: list[Card] | None = None,
        your_seat: int = 0,
        dealer_seat: int = 0,
    ) -> GameState:
        """Build a ``GameState`` from a legacy flat ``action_request`` payload.

        Retained for backward compatibility with the local testing harness
        and code that predates the two-layer protocol. New code should use
        :meth:`from_turn_request`.
        """
        board_strs = payload.get("board", [])
        board = [Card.from_str(c) for c in board_strs]

        return cls(
            hand_number=int(payload.get("hand_number", 0)),
            phase=str(payload.get("phase", "preflop")),
            hole_cards=hole_cards or [],
            board=board,
            pot=int(payload.get("pot", 0)),
            your_stack=int(payload.get("your_stack", 0)),
            opponent_stacks=[int(s) for s in payload.get("opponent_stacks", [])],
            your_seat=your_seat,
            dealer_seat=dealer_seat,
            to_call=int(payload.get("to_call", 0)),
            min_raise=int(payload.get("min_raise", 0)),
            max_raise=int(payload.get("max_raise", 0)),
            valid_actions=[str(a) for a in payload.get("valid_actions", [])],
            action_history=list(payload.get("action_history", [])),
        )

    @classmethod
    def from_turn_request(
        cls,
        message: dict,
        *,
        your_seat: int = 0,
        dealer_seat: int = 0,
    ) -> GameState:
        """Build a ``GameState`` from a two-layer ``turn_request`` message.

        The message envelope carries ``valid_actions`` and ``request_id`` at
        the top level; the game-specific payload (hole cards, board, pot,
        stacks, ``to_call``, ``min_raise``, ``max_raise``, ``action_history``)
        lives in the nested ``state`` object per the Poker Layer 2 spec.

        Args:
            message: The full ``turn_request`` message (Layer 1 envelope).
            your_seat: The bot's seat index (determined at ``match_start``).
                Used only as a fallback: OFC's ``turn_request.state`` carries
                ``your_seat`` itself and that value wins when present.
            dealer_seat: Current dealer seat (from the most recent
                ``round_start.state``). Same fallback rule -- OFC carries
                ``dealer_seat`` in state, 27TD does not (a 27TD bot must
                retain it from ``round_start``).
        """
        state = message.get("state", {}) or {}
        hole_strs = state.get("your_hole_cards", [])
        board_strs = state.get("board", [])

        return cls(
            hand_number=int(state.get("hand_number", 0)),
            phase=str(state.get("phase", "preflop")),
            hole_cards=[Card.from_str(c) for c in hole_strs],
            board=[Card.from_str(c) for c in board_strs],
            pot=int(state.get("pot", 0)),
            your_stack=int(state.get("your_stack", 0)),
            opponent_stacks=[int(s) for s in state.get("opponent_stacks", [])],
            your_seat=_as_int(state.get("your_seat"), your_seat),
            dealer_seat=_as_int(state.get("dealer_seat"), dealer_seat),
            to_call=int(state.get("to_call", 0)),
            min_raise=int(state.get("min_raise", 0)),
            max_raise=int(state.get("max_raise", 0)),
            valid_actions=[str(a) for a in message.get("valid_actions", [])],
            action_history=list(state.get("action_history", [])),
            round_id=str(message.get("round_id", "")),
            request_id=str(message.get("request_id", "")),
            # --- 2-7 Triple Draw (absent at an NLHE table -> defaults) ---
            is_draw_phase=_as_bool(state.get("is_draw_phase")),
            draw_number=_as_int(state.get("draw_number")),
            draws_remaining=_as_int(state.get("draws_remaining")),
            max_discard=_as_int(state.get("max_discard")),
            your_draw_counts=_as_int_list(state.get("your_draw_counts")),
            opponent_draw_counts={
                str(seat): _as_int_list(counts)
                for seat, counts in _as_dict(state.get("opponent_draw_counts")).items()
            },
            # --- Pineapple OFC (absent at an NLHE table -> defaults) ---
            your_rows=_as_rows(state.get("your_rows")),
            opponent_rows={
                str(seat): _as_rows(rows)
                for seat, rows in _as_dict(state.get("opponent_rows")).items()
            },
            cards_to_place=_as_card_strs(state.get("cards_to_place")),
            place=_as_int(state.get("place")),
            must_discard=_as_int(state.get("must_discard")),
            row_capacity=_as_int_map(state.get("row_capacity")),
            royalties=_as_int_map(state.get("royalties")),
            opponent_royalties={
                str(seat): _as_int_map(royalties)
                for seat, royalties in _as_dict(state.get("opponent_royalties")).items()
            },
            point_value=_as_int(state.get("point_value")),
            in_fantasy_land=_as_bool(state.get("in_fantasy_land")),
            phase_sequence=[str(ph) for ph in _as_list(state.get("phase_sequence"))],
        )


@dataclass(frozen=True, slots=True)
class RoundStart:
    """Parsed ``round_start`` message (Layer 1 envelope + Layer 2 state).

    Attributes:
        hand_number: 1-indexed hand number within the match.
        dealer_seat: Seat index of the dealer (button).
        hole_cards: Your two private cards for this hand.
        stacks: Chip stacks indexed by seat, before blinds are posted.
        round_id: Globally unique round identifier (Layer 1).
        deck_commitment: SHA-256 commitment for RNG verification. Empty
            string if verification is not enabled.
    """

    hand_number: int
    dealer_seat: int
    hole_cards: list[Card]
    stacks: list[int]
    round_id: str = ""
    deck_commitment: str = ""

    @classmethod
    def from_message(cls, message: dict) -> RoundStart:
        state = message.get("state", {}) or {}
        hole_strs = state.get("your_hole_cards", [])
        return cls(
            hand_number=int(state.get("hand_number", 0)),
            dealer_seat=int(state.get("dealer_seat", 0)),
            hole_cards=[Card.from_str(c) for c in hole_strs],
            stacks=[int(s) for s in state.get("stacks", [])],
            round_id=str(message.get("round_id", "")),
            deck_commitment=str(state.get("deck_commitment", "")),
        )


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Parsed ``turn_result`` broadcast (Layer 1 envelope + Layer 2 details).

    Attributes:
        seat: Seat number of the participant who acted.
        action: Action string (e.g., ``fold``, ``call``, ``raise``).
        amount: Chips committed by the action (``0`` for fold/check).
        is_timeout: True if the server auto-applied the action due to timeout.
    """

    seat: int
    action: str
    amount: int = 0
    is_timeout: bool = False

    @classmethod
    def from_message(cls, message: dict) -> TurnResult:
        details = message.get("details", {}) or {}
        return cls(
            seat=int(details.get("seat", message.get("seat", 0))),
            action=str(details.get("action", "")),
            amount=int(details.get("amount", 0)),
            is_timeout=bool(message.get("is_timeout", False)),
        )
