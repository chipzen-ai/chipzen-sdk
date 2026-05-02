"""Property-based tests for model serialization/deserialization round-trips.

Uses Hypothesis to generate random valid inputs and verify that
serialization is consistent and reversible.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

try:
    from hypothesis import assume, given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False

from chipzen.models import Action, Card, GameState, Player

RANKS = list("23456789TJQKA")
SUITS = list("hdcs")


def card_strategy():
    """Hypothesis strategy for generating valid Card instances."""
    if not HAS_HYPOTHESIS:
        return None
    return st.builds(Card, rank=st.sampled_from(RANKS), suit=st.sampled_from(SUITS))


def card_str_strategy():
    """Hypothesis strategy for generating valid card strings."""
    if not HAS_HYPOTHESIS:
        return None
    return st.tuples(st.sampled_from(RANKS), st.sampled_from(SUITS)).map(lambda t: t[0] + t[1])


skipif_no_hypothesis = pytest.mark.skipif(not HAS_HYPOTHESIS, reason="hypothesis not installed")


@skipif_no_hypothesis
class TestCardProperties:
    @given(rank=st.sampled_from(RANKS), suit=st.sampled_from(SUITS))
    def test_from_str_roundtrip(self, rank, suit):
        """Card.from_str(str(card)) should return the same card."""
        card = Card(rank=rank, suit=suit)
        s = str(card)
        roundtripped = Card.from_str(s)
        assert roundtripped == card

    @given(rank=st.sampled_from(RANKS), suit=st.sampled_from(SUITS))
    def test_str_roundtrip(self, rank, suit):
        """str(Card.from_str(s)) should return the same string."""
        s = f"{rank}{suit}"
        card = Card.from_str(s)
        assert str(card) == s

    @given(card_str_strategy())
    def test_from_str_always_uppercase_rank(self, s):
        card = Card.from_str(s)
        assert card.rank in RANKS
        assert card.suit in SUITS

    @given(rank=st.sampled_from(RANKS), suit=st.sampled_from(SUITS))
    def test_card_frozen(self, rank, suit):
        card = Card(rank=rank, suit=suit)
        with pytest.raises(AttributeError):
            card.rank = "X"

    @given(
        r1=st.sampled_from(RANKS),
        s1=st.sampled_from(SUITS),
        r2=st.sampled_from(RANKS),
        s2=st.sampled_from(SUITS),
    )
    def test_card_equality(self, r1, s1, r2, s2):
        c1 = Card(rank=r1, suit=s1)
        c2 = Card(rank=r2, suit=s2)
        if r1 == r2 and s1 == s2:
            assert c1 == c2
            assert hash(c1) == hash(c2)
        else:
            assert c1 != c2


@skipif_no_hypothesis
class TestActionProperties:
    @given(st.sampled_from(["fold", "check", "call"]))
    def test_non_raise_to_dict_roundtrip(self, action_type):
        """Non-raise actions serialize without amount."""
        action = Action(action=action_type)
        d = action.to_dict()
        assert d["action"] == action_type
        assert "amount" not in d

    @given(st.integers(min_value=1, max_value=1_000_000))
    def test_raise_to_dict_includes_amount(self, amount):
        action = Action.raise_to(amount)
        d = action.to_dict()
        assert d["action"] == "raise"
        assert d["amount"] == amount

    @given(st.sampled_from(["fold", "check", "call"]))
    def test_factory_methods(self, action_type):
        factory = getattr(Action, action_type)
        action = factory()
        assert action.action == action_type


@skipif_no_hypothesis
class TestGameStateProperties:
    @given(
        hand_number=st.integers(min_value=0, max_value=10000),
        phase=st.sampled_from(["preflop", "flop", "turn", "river"]),
        pot=st.integers(min_value=0, max_value=1_000_000),
        your_stack=st.integers(min_value=0, max_value=1_000_000),
        to_call=st.integers(min_value=0, max_value=1_000_000),
        min_raise=st.integers(min_value=0, max_value=1_000_000),
        max_raise=st.integers(min_value=0, max_value=1_000_000),
    )
    def test_from_action_request_preserves_fields(
        self, hand_number, phase, pot, your_stack, to_call, min_raise, max_raise
    ):
        """from_action_request should preserve all numeric fields."""
        payload = {
            "hand_number": hand_number,
            "phase": phase,
            "board": [],
            "pot": pot,
            "your_stack": your_stack,
            "opponent_stacks": [],
            "to_call": to_call,
            "min_raise": min_raise,
            "max_raise": max_raise,
            "valid_actions": ["fold", "call"],
            "action_history": [],
        }
        state = GameState.from_action_request(payload)
        assert state.hand_number == hand_number
        assert state.phase == phase
        assert state.pot == pot
        assert state.your_stack == your_stack
        assert state.to_call == to_call
        assert state.min_raise == min_raise
        assert state.max_raise == max_raise

    @given(
        board_cards=st.lists(
            st.tuples(st.sampled_from(RANKS), st.sampled_from(SUITS)).map(lambda t: t[0] + t[1]),
            min_size=0,
            max_size=5,
        )
    )
    def test_from_action_request_parses_board(self, board_cards):
        """Board card strings should be parsed into Card objects."""
        payload = {
            "board": board_cards,
            "valid_actions": ["fold"],
        }
        state = GameState.from_action_request(payload)
        assert len(state.board) == len(board_cards)
        for card, card_str in zip(state.board, board_cards):
            assert str(card) == card_str

    @given(
        valid_actions=st.lists(
            st.sampled_from(["fold", "check", "call", "raise"]),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    def test_from_action_request_preserves_valid_actions(self, valid_actions):
        payload = {"valid_actions": valid_actions}
        state = GameState.from_action_request(payload)
        assert state.valid_actions == valid_actions


@skipif_no_hypothesis
class TestPlayerProperties:
    @given(
        seat=st.integers(min_value=0, max_value=9),
        stack=st.integers(min_value=0, max_value=1_000_000),
        is_active=st.booleans(),
        is_all_in=st.booleans(),
    )
    def test_player_fields(self, seat, stack, is_active, is_all_in):
        p = Player(seat=seat, stack=stack, is_active=is_active, is_all_in=is_all_in)
        assert p.seat == seat
        assert p.stack == stack
        assert p.is_active == is_active
        assert p.is_all_in == is_all_in
