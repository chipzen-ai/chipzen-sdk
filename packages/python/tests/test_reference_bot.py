"""Tests for the upgraded examples/reference-bot/ — pin its behavior so
the demonstrative bot doesn't silently regress as the SDK evolves.
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.models import Action, Card, GameState

REFERENCE_BOT_PATH = (
    Path(__file__).parent.parent.parent.parent / "examples" / "reference-bot" / "bot.py"
)


def _load_reference_bot_module():
    spec = importlib.util.spec_from_file_location("_reference_bot_under_test", REFERENCE_BOT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ref_module():
    return _load_reference_bot_module()


def _state(
    *,
    phase="preflop",
    hole=("Ah", "Kd"),
    board=(),
    valid=("fold", "call", "raise"),
    to_call=10,
    min_raise=20,
    max_raise=1000,
    your_stack=1000,
    pot=15,
    action_history=(),
) -> GameState:
    return GameState(
        hand_number=1,
        phase=phase,
        hole_cards=[Card.from_str(c) for c in hole],
        board=[Card.from_str(c) for c in board],
        pot=pot,
        your_stack=your_stack,
        opponent_stacks=[your_stack],
        your_seat=0,
        dealer_seat=0,
        to_call=to_call,
        min_raise=min_raise,
        max_raise=max_raise,
        valid_actions=list(valid),
        action_history=list(action_history),
    )


# ---------------------------------------------------------------------------
# _preflop_bucket
# ---------------------------------------------------------------------------


class TestPreflopBucket:
    @pytest.mark.parametrize(
        "hand,expected",
        [
            (("Ah", "As"), "premium"),  # pocket aces
            (("Kh", "Ks"), "premium"),  # pocket kings
            (("Jh", "Jd"), "premium"),  # pocket jacks
            (("Ah", "Kd"), "premium"),  # AK off
            (("Th", "Ts"), "strong"),  # pocket tens
            (("9h", "9s"), "strong"),  # pocket nines
            (("Ah", "Qh"), "strong"),  # AQ suited
            (("Kh", "Qh"), "strong"),  # KQ suited
            (("8h", "8d"), "medium"),  # pocket eights
            (("Ah", "5d"), "medium"),  # weak ace
            (("7h", "2c"), "weak"),  # garbage
            (("3d", "9c"), "weak"),
        ],
    )
    def test_buckets_known_hands(self, ref_module, hand, expected):
        cards = [Card.from_str(c) for c in hand]
        assert ref_module._preflop_bucket(cards) == expected

    def test_handles_empty_input(self, ref_module):
        assert ref_module._preflop_bucket([]) == "weak"


# ---------------------------------------------------------------------------
# _made_hand_class
# ---------------------------------------------------------------------------


class TestMadeHandClass:
    def test_no_pair_returns_zero(self, ref_module):
        hole = [Card.from_str("Ah"), Card.from_str("Kd")]
        board = [Card.from_str("7s"), Card.from_str("4c"), Card.from_str("2h")]
        assert ref_module._made_hand_class(hole, board) == 0

    def test_pair_returns_one(self, ref_module):
        hole = [Card.from_str("Ah"), Card.from_str("Kd")]
        board = [Card.from_str("As"), Card.from_str("4c"), Card.from_str("2h")]
        assert ref_module._made_hand_class(hole, board) == 1

    def test_two_pair_returns_two(self, ref_module):
        hole = [Card.from_str("Ah"), Card.from_str("Kd")]
        board = [Card.from_str("As"), Card.from_str("Kc"), Card.from_str("2h")]
        assert ref_module._made_hand_class(hole, board) == 2

    def test_trips_returns_three(self, ref_module):
        hole = [Card.from_str("Ah"), Card.from_str("Ad")]
        board = [Card.from_str("As"), Card.from_str("4c"), Card.from_str("2h")]
        assert ref_module._made_hand_class(hole, board) == 3

    def test_preflop_no_board_no_pair(self, ref_module):
        hole = [Card.from_str("Ah"), Card.from_str("Kd")]
        assert ref_module._made_hand_class(hole, []) == 0

    def test_preflop_pocket_pair(self, ref_module):
        hole = [Card.from_str("Ah"), Card.from_str("Ad")]
        assert ref_module._made_hand_class(hole, []) == 1


# ---------------------------------------------------------------------------
# _bounded_raise
# ---------------------------------------------------------------------------


class TestBoundedRaise:
    def test_returns_target_when_in_range(self, ref_module):
        state = _state(min_raise=20, max_raise=1000)
        assert ref_module._bounded_raise(60, state) == 60

    def test_clamps_to_min(self, ref_module):
        state = _state(min_raise=20, max_raise=1000)
        assert ref_module._bounded_raise(10, state) == 20

    def test_clamps_to_max(self, ref_module):
        state = _state(min_raise=20, max_raise=100)
        assert ref_module._bounded_raise(500, state) == 100

    def test_returns_none_when_raising_illegal(self, ref_module):
        state = _state(min_raise=0, max_raise=0)
        assert ref_module._bounded_raise(60, state) is None


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    def test_on_match_start_records_self_seat(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot.on_match_start(
            {
                "seats": [
                    {"seat": 0, "is_self": False},
                    {"seat": 1, "is_self": True},
                    {"seat": 2, "is_self": False},
                ]
            }
        )
        assert bot._my_seat == 1

    def test_on_match_start_handles_missing_seats(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot.on_match_start({})
        assert bot._my_seat is None

    def test_on_round_start_resets_aggression_counter(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot._opponent_raises_this_hand = 5
        bot.on_round_start({"state": {"hand_number": 2, "your_hole_cards": ["Ah", "Kd"]}})
        assert bot._opponent_raises_this_hand == 0

    def test_on_turn_result_counts_opponent_raises(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot._my_seat = 0
        bot.on_turn_result({"details": {"seat": 1, "action": "raise", "amount": 60}})
        bot.on_turn_result({"details": {"seat": 1, "action": "raise", "amount": 120}})
        assert bot._opponent_raises_this_hand == 2

    def test_on_turn_result_counts_all_in_as_aggression(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot._my_seat = 0
        bot.on_turn_result({"details": {"seat": 1, "action": "all_in"}})
        assert bot._opponent_raises_this_hand == 1

    def test_on_turn_result_ignores_my_own_actions(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot._my_seat = 0
        bot.on_turn_result({"details": {"seat": 0, "action": "raise", "amount": 60}})
        assert bot._opponent_raises_this_hand == 0

    def test_on_turn_result_ignores_calls(self, ref_module):
        bot = ref_module.ReferenceBot()
        bot._my_seat = 0
        bot.on_turn_result({"details": {"seat": 1, "action": "call"}})
        assert bot._opponent_raises_this_hand == 0


# ---------------------------------------------------------------------------
# decide() — return values are always in state.valid_actions
# ---------------------------------------------------------------------------


class TestDecideReturnsLegalActions:
    """The most important invariant: the bot must never return an action
    the server hasn't offered. Crash here and the platform will
    safe-default + emit bot_error on every hand."""

    @pytest.mark.parametrize(
        "phase,hole,valid",
        [
            ("preflop", ("Ah", "As"), ("fold", "call", "raise")),  # premium, all options
            ("preflop", ("Ah", "As"), ("fold", "call")),  # premium, no raise
            ("preflop", ("7h", "2c"), ("fold", "call", "raise")),  # weak hand
            ("preflop", ("7h", "2c"), ("check",)),  # only check
            ("flop", ("Ah", "As"), ("fold", "check", "raise")),  # premium postflop
            ("river", ("7h", "2c"), ("fold", "call")),  # weak postflop
        ],
    )
    def test_returns_action_in_valid_actions(self, ref_module, phase, hole, valid):
        bot = ref_module.ReferenceBot()
        state = _state(phase=phase, hole=hole, valid=valid)
        action = bot.decide(state)
        assert isinstance(action, Action)
        assert action.action in valid, (
            f"Bot returned {action.action!r} but valid_actions was {valid}"
        )

    def test_premium_preflop_raises_when_legal(self, ref_module):
        """Pocket aces with raise legal → should open-raise, not just call."""
        bot = ref_module.ReferenceBot()
        state = _state(
            phase="preflop", hole=("Ah", "As"), valid=("fold", "call", "raise"),
            to_call=10, min_raise=20, max_raise=1000,
        )
        action = bot.decide(state)
        assert action.action == "raise"

    def test_weak_preflop_facing_bet_folds(self, ref_module):
        """Garbage hand facing a non-trivial bet → fold."""
        bot = ref_module.ReferenceBot()
        state = _state(
            phase="preflop", hole=("7h", "2c"), valid=("fold", "call"),
            to_call=200, your_stack=1000,
        )
        action = bot.decide(state)
        assert action.action == "fold"

    def test_two_pair_postflop_raises_unopposed(self, ref_module):
        """Two pair with no opponent aggression → bet."""
        bot = ref_module.ReferenceBot()
        bot._my_seat = 0
        bot.on_round_start({"state": {"hand_number": 1, "your_hole_cards": ["Ah", "Kd"]}})
        state = _state(
            phase="flop", hole=("Ah", "Kd"), board=("As", "Kc", "2h"),
            valid=("fold", "check", "raise"),
            to_call=0, min_raise=10, max_raise=1000, pot=60,
        )
        action = bot.decide(state)
        assert action.action == "raise"

    def test_no_pair_postflop_facing_bet_folds(self, ref_module):
        bot = ref_module.ReferenceBot()
        state = _state(
            phase="flop", hole=("7h", "2c"), board=("As", "Kc", "Td"),
            valid=("fold", "call"), to_call=200, pot=100,
        )
        action = bot.decide(state)
        assert action.action == "fold"


# ---------------------------------------------------------------------------
# Validate the reference bot via the SDK's own validate machinery
# ---------------------------------------------------------------------------


class TestReferenceBotPassesValidate:
    def test_validate_zero_failures(self):
        from chipzen.validate import validate_bot

        # The reference-bot directory has bot.py + Dockerfile + README.md
        # but no requirements.txt (the Dockerfile pip-installs chipzen-bot
        # directly). validate_bot tolerates missing requirements.txt.
        results = validate_bot(REFERENCE_BOT_PATH.parent)
        failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert failures == [], f"Reference bot failed validate: {failures}"
