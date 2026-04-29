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

from dataclasses import dataclass, field


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
        action: One of "fold", "check", "call", "raise", "all_in".
        amount: Raise amount (total bet, not additional chips). Ignored for
                fold/check/call/all_in.
    """

    action: str
    amount: int = 0

    def to_dict(self) -> dict:
        """Convert to the legacy flat wire format (action + amount).

        Use :meth:`to_wire` for the new two-layer ``turn_action`` format
        with a nested ``params`` object.
        """
        d: dict = {"action": self.action}
        if self.action == "raise":
            d["amount"] = self.amount
        return d

    def to_wire(self) -> dict:
        """Convert to the two-layer ``turn_action`` payload fields.

        Returns a dict with ``action`` (string) and ``params`` (object)
        suitable for inclusion in a ``turn_action`` message. Callers are
        expected to add the Layer 1 envelope fields (``type``, ``match_id``,
        ``request_id``).
        """
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
            dealer_seat: Current dealer seat (from the most recent
                ``round_start.state``).
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
            your_seat=your_seat,
            dealer_seat=dealer_seat,
            to_call=int(state.get("to_call", 0)),
            min_raise=int(state.get("min_raise", 0)),
            max_raise=int(state.get("max_raise", 0)),
            valid_actions=[str(a) for a in message.get("valid_actions", [])],
            action_history=list(state.get("action_history", [])),
            round_id=str(message.get("round_id", "")),
            request_id=str(message.get("request_id", "")),
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
