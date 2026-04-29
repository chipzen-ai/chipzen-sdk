"""Integration tests verifying SDK types are compatible with the platform's bot_api.py types.

These tests ensure the SDK models (chipzen.models) and the platform models
(chipzen.bots.bot_api) remain structurally compatible, even though they are
separate implementations.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot as SDKBot
from chipzen.models import Action as SDKAction
from chipzen.models import Card as SDKCard
from chipzen.models import GameState as SDKGameState
from chipzen.models import Player as SDKPlayer


class TestSDKTypeCompatibility:
    """Verify that SDK types have the fields the platform protocol expects."""

    def test_action_has_to_dict(self):
        """Actions must serialize to dicts for WebSocket transport."""
        actions = [SDKAction.fold(), SDKAction.check(), SDKAction.call(), SDKAction.raise_to(500)]
        for action in actions:
            d = action.to_dict()
            assert isinstance(d, dict)
            assert "action" in d

    def test_action_to_dict_fold(self):
        d = SDKAction.fold().to_dict()
        assert d == {"action": "fold"}

    def test_action_to_dict_raise_includes_amount(self):
        d = SDKAction.raise_to(300).to_dict()
        assert d == {"action": "raise", "amount": 300}

    def test_action_to_dict_call_excludes_amount(self):
        d = SDKAction.call().to_dict()
        assert "amount" not in d

    def test_card_from_str_roundtrip(self):
        """Card.from_str -> str(card) should round-trip."""
        for s in ["Ah", "2c", "Td", "Ks"]:
            card = SDKCard.from_str(s)
            assert str(card) == s

    def test_game_state_from_action_request_protocol(self):
        """GameState.from_action_request must handle the server's wire format."""
        wire = {
            "hand_number": 10,
            "phase": "turn",
            "board": ["Ts", "7h", "2c", "Kd"],
            "pot": 800,
            "your_stack": 5000,
            "opponent_stacks": [4500],
            "to_call": 200,
            "min_raise": 400,
            "max_raise": 5000,
            "valid_actions": ["fold", "call", "raise"],
            "action_history": [
                {"seat": 1, "action": "raise", "amount": 400},
            ],
        }
        state = SDKGameState.from_action_request(
            wire,
            hole_cards=[SDKCard.from_str("Ah"), SDKCard.from_str("Kd")],
            your_seat=0,
            dealer_seat=1,
        )
        assert state.hand_number == 10
        assert state.phase == "turn"
        assert len(state.board) == 4
        assert state.pot == 800
        assert state.to_call == 200
        assert state.valid_actions == ["fold", "call", "raise"]

    def test_player_dataclass_has_required_fields(self):
        p = SDKPlayer(seat=0, stack=10000)
        assert p.seat == 0
        assert p.stack == 10000
        assert p.is_active is True
        assert p.is_all_in is False


class TestSDKBotInterface:
    """Verify the bot interface contract."""

    def test_bot_subclass_must_implement_decide(self):
        """Cannot instantiate a bot without decide()."""
        with pytest.raises(TypeError):

            class IncompletBot(SDKBot):
                pass

            IncompletBot()

    def test_bot_lifecycle_hooks_are_optional(self):
        """Lifecycle hooks should be callable without override."""

        class MinimalBot(SDKBot):
            def decide(self, state):
                return SDKAction.fold()

        bot = MinimalBot()
        # These should not raise
        bot.on_match_start({})
        bot.on_hand_start(1, [])
        bot.on_hand_result({})
        bot.on_match_end({})


# `TestExampleBotsIntegration` previously exercised the built-in example bots
# end-to-end through the local match runner. The SDK no longer ships a
# local match simulator (the platform handles bot-vs-bot evaluation after
# upload), so those tests have been removed. The example bots themselves
# remain importable as `Bot` subclasses; their construction + decide() shape
# is exercised by the type-compatibility tests above plus the per-example
# unit tests in tests/test_models.py.
