"""Tests for the chipzen SDK data models."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.models import Action, Card, GameState, Player, TurnResult


class TestCard:
    def test_from_str_valid(self):
        card = Card.from_str("Ah")
        assert card.rank == "A"
        assert card.suit == "h"

    def test_from_str_ten(self):
        card = Card.from_str("Ts")
        assert card.rank == "T"
        assert card.suit == "s"

    def test_from_str_lowercase_rank(self):
        card = Card.from_str("kd")
        assert card.rank == "K"
        assert card.suit == "d"

    def test_from_str_invalid_length(self):
        with pytest.raises(ValueError, match="Invalid card string"):
            Card.from_str("A")

    def test_from_str_invalid_rank(self):
        with pytest.raises(ValueError, match="Invalid rank"):
            Card.from_str("Xh")

    def test_from_str_invalid_suit(self):
        with pytest.raises(ValueError, match="Invalid suit"):
            Card.from_str("Ax")

    def test_str(self):
        card = Card(rank="A", suit="h")
        assert str(card) == "Ah"

    def test_repr(self):
        card = Card(rank="2", suit="c")
        assert repr(card) == "Card('2c')"

    def test_equality(self):
        assert Card(rank="A", suit="h") == Card(rank="A", suit="h")
        assert Card(rank="A", suit="h") != Card(rank="K", suit="h")

    def test_frozen(self):
        card = Card(rank="A", suit="h")
        with pytest.raises(AttributeError):
            card.rank = "K"  # type: ignore[misc]


class TestPlayer:
    def test_defaults(self):
        player = Player(seat=0, stack=10000)
        assert player.is_active is True
        assert player.is_all_in is False

    def test_custom(self):
        player = Player(seat=1, stack=5000, is_active=False, is_all_in=True)
        assert player.seat == 1
        assert player.stack == 5000
        assert player.is_active is False
        assert player.is_all_in is True


class TestAction:
    def test_fold(self):
        a = Action.fold()
        assert a.action == "fold"
        assert a.to_dict() == {"action": "fold"}

    def test_check(self):
        a = Action.check()
        assert a.action == "check"
        assert a.to_dict() == {"action": "check"}

    def test_call(self):
        a = Action.call()
        assert a.action == "call"
        assert a.to_dict() == {"action": "call"}

    def test_raise_to(self):
        a = Action.raise_to(500)
        assert a.action == "raise"
        assert a.amount == 500
        assert a.to_dict() == {"action": "raise", "amount": 500}

    def test_raise_dict_excludes_amount_for_non_raise(self):
        a = Action.call()
        assert "amount" not in a.to_dict()


class TestGameState:
    def test_from_action_request(self):
        payload = {
            "hand_number": 5,
            "phase": "flop",
            "board": ["Ts", "7h", "2c"],
            "pot": 300,
            "your_stack": 9700,
            "opponent_stacks": [9800],
            "to_call": 100,
            "min_raise": 200,
            "max_raise": 9800,
            "valid_actions": ["fold", "call", "raise"],
            "action_history": [{"seat": 1, "action": "raise", "amount": 200}],
        }
        hole = [Card.from_str("Ah"), Card.from_str("Kd")]
        state = GameState.from_action_request(payload, hole_cards=hole, your_seat=0, dealer_seat=1)

        assert state.hand_number == 5
        assert state.phase == "flop"
        assert len(state.board) == 3
        assert state.board[0] == Card(rank="T", suit="s")
        assert state.pot == 300
        assert state.your_stack == 9700
        assert state.to_call == 100
        assert state.min_raise == 200
        assert state.max_raise == 9800
        assert state.valid_actions == ["fold", "call", "raise"]
        assert len(state.action_history) == 1
        assert state.hole_cards == hole
        assert state.your_seat == 0
        assert state.dealer_seat == 1

    def test_from_action_request_defaults(self):
        state = GameState.from_action_request({})
        assert state.hand_number == 0
        assert state.phase == "preflop"
        assert state.board == []
        assert state.hole_cards == []
        assert state.pot == 0


class TestTurnResult:
    """``is_timeout`` lives in ``details`` (Chipzen #7586).

    Frames sent before that change carry it at the top level.
    """

    @staticmethod
    def _frame(details: dict, **top: object) -> dict:
        return {"type": "turn_result", "match_id": "m", "seq": 5, "details": details, **top}

    def test_is_timeout_read_from_details(self):
        result = TurnResult.from_message(
            self._frame(
                {"seat": 1, "action": "fold", "amount": 0, "source": "default", "is_timeout": True}
            )
        )
        assert result.is_timeout is True
        assert result.seat == 1 and result.action == "fold"

    def test_details_false_wins_over_top_level(self):
        result = TurnResult.from_message(
            self._frame({"action": "call", "amount": 5, "is_timeout": False}, is_timeout=True)
        )
        assert result.is_timeout is False

    def test_details_true_wins_over_top_level(self):
        result = TurnResult.from_message(
            self._frame({"action": "fold", "is_timeout": True}, is_timeout=False)
        )
        assert result.is_timeout is True

    def test_falls_back_to_top_level_when_details_lacks_it(self):
        result = TurnResult.from_message(
            self._frame({"seat": 0, "action": "fold", "amount": 0}, is_timeout=True)
        )
        assert result.is_timeout is True

    def test_defaults_to_false_when_absent(self):
        result = TurnResult.from_message(self._frame({"seat": 0, "action": "check", "amount": 0}))
        assert result.is_timeout is False

    def test_missing_or_null_details_uses_top_level(self):
        assert (
            TurnResult.from_message({"type": "turn_result", "is_timeout": True}).is_timeout is True
        )
        assert (
            TurnResult.from_message(
                {"type": "turn_result", "details": None, "is_timeout": True}
            ).is_timeout
            is True
        )
