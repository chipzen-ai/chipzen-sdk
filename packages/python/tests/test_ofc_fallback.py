"""The safe fallback at a Pineapple OFC turn (chipzen-sdk#197).

An OFC turn offers exactly ``["place"]``: no fold, no check. When a bot's
``decide()`` raises, returns something that is not an ``Action``, or its answer
gets ``action_rejected``, the frame sent is the protocol's default placement
(OFC-GAME-STATE-PROTOCOL section 5.9), built from that turn's state. Mirrors the
JavaScript SDK's ``packages/javascript/tests/ofc.test.ts``, on the same fixtures.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot
from chipzen.client import (
    BotDecisionError,
    _ofc_default_placement,
    _run_session,
    _safe_fallback_action,
)
from chipzen.conformance import (
    MATCH_ID,
    _drive_session,
    _extract_turn_actions,
    _MockWebSocket,
    _ofc_match_start,
    _ofc_round_start,
    _ofc_script,
    _ofc_turn_request,
    _server_hello,
    _turn_request,
)
from chipzen.models import Action, GameState

#: The default placement for ``_ofc_turn_request()``. Capacity top 2, middle 3,
#: bottom 3: 9s ties middle/bottom -> bottom; then middle (3) beats bottom (2)
#: -> Js to middle; Ks is the discard.
STREET_DEFAULT = {
    "placements": [{"card": "9s", "row": "bottom"}, {"card": "Js", "row": "middle"}],
    "discard": ["Ks"],
}
EMPTY_PLACEMENT = {"placements": [], "discard": []}


def _deal1_turn_request(seq: int = 4, request_id: str = "req_1") -> dict:
    """The opening set for the same hand: five cards dealt, place 5, discard 0."""
    msg = copy.deepcopy(_ofc_turn_request(seq, request_id))
    opening = _ofc_round_start()["state"]["cards_to_place"]
    msg["state"].update(
        phase="deal1",
        your_hole_cards=list(opening),
        cards_to_place=list(opening),
        your_rows={"top": [], "middle": [], "bottom": []},
        opponent_rows={"0": {"top": [], "middle": [], "bottom": []}},
        place=5,
        must_discard=0,
        row_capacity={"top": 3, "middle": 5, "bottom": 5},
        action_history=[],
    )
    return msg


def _fantasy_land_turn_request(seq: int = 4, request_id: str = "req_1") -> dict:
    """A Fantasy Land turn (OFC section 5.6): 14 cards, place 13, discard 1."""
    cards = ["2c", "3d", "4h", "5s", "6s", "7c", "8d", "9h", "Ts", "Jc", "Qd", "Kh", "As", "Ac"]
    msg = _deal1_turn_request(seq, request_id)
    msg["state"].update(
        your_hole_cards=list(cards),
        cards_to_place=list(cards),
        place=13,
        must_discard=1,
        in_fantasy_land=True,
        phase_sequence=["deal1", "complete"],
    )
    return msg


TURNS = {
    "a pineapple street": _ofc_turn_request,
    "the opening set": _deal1_turn_request,
    "a Fantasy Land turn": _fantasy_land_turn_request,
}


def _script(turn: dict) -> list[dict]:
    return [_server_hello(game_type="ofc_pineapple"), _ofc_match_start(seq=2), turn]


def _rejection(seq: int, request_id: str) -> dict:
    return {
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "seq": seq,
        "request_id": request_id,
        "reason": "invalid_action",
        "message": "illegal placement",
        "remaining_ms": 4000,
        "valid_actions": ["place"],
    }


def _assert_legal_placement(params: dict, state: GameState) -> None:
    """OFC section 3.5: exactly ``place`` placements and ``must_discard`` discards."""
    placements = params["placements"]
    assert len(placements) == state.place
    assert len(params["discard"]) == state.must_discard
    used = [p["card"] for p in placements] + list(params["discard"])
    assert sorted(used) == sorted(state.cards_to_place)
    counts: dict[str, int] = {}
    for p in placements:
        counts[p["row"]] = counts.get(p["row"], 0) + 1
    for row, count in counts.items():
        assert count <= state.row_capacity[row], row


class ThrowingBot(ChipzenBot):
    def decide(self, state: GameState) -> Action:
        raise RuntimeError("boom")


class ReturningBot(ChipzenBot):
    """Returns whatever it was built with, whether or not that is an Action."""

    def __init__(self, value: object) -> None:
        self.value = value

    def decide(self, state: GameState) -> Action:
        return self.value  # type: ignore[return-value]


class TestDefaultPlacement:
    def test_most_capacity_ties_toward_the_back_and_the_rest_is_discarded(self):
        state = GameState.from_turn_request(_ofc_turn_request())
        assert _ofc_default_placement(state).to_wire()["params"] == STREET_DEFAULT

    def test_differs_from_the_decide_placement_hook(self):
        """The hook fills bottom first; the protocol default balances capacity."""
        state = GameState.from_turn_request(_ofc_turn_request())
        hook = ThrowingBot().decide_placement(state).to_wire()["params"]
        assert [p["row"] for p in hook["placements"]] == ["bottom", "bottom"]

    def test_respects_a_full_row(self):
        msg = _ofc_turn_request()
        msg["state"]["row_capacity"] = {"top": 0, "middle": 0, "bottom": 2}
        params = _ofc_default_placement(GameState.from_turn_request(msg)).to_wire()["params"]
        assert [p["row"] for p in params["placements"]] == ["bottom", "bottom"]

    def test_does_not_mutate_the_state(self):
        state = GameState.from_turn_request(_ofc_turn_request())
        _ofc_default_placement(state)
        assert state.row_capacity == {"top": 2, "middle": 3, "bottom": 3}
        assert state.cards_to_place == ["9s", "Js", "Ks"]

    @pytest.mark.parametrize("name", sorted(TURNS))
    def test_is_legal(self, name):
        state = GameState.from_turn_request(TURNS[name]())
        action = _safe_fallback_action(["place"], state)
        assert action.action == "place"
        _assert_legal_placement(action.to_wire()["params"], state)

    def test_without_a_state_it_is_an_empty_well_formed_placement(self):
        assert _safe_fallback_action(["place"]).to_wire() == {
            "action": "place",
            "params": EMPTY_PLACEMENT,
        }

    def test_leaves_the_other_fallbacks_where_they_are_offered(self):
        state = GameState.from_turn_request(_ofc_turn_request())
        assert _safe_fallback_action(["check", "raise"], state).action == "check"
        assert _safe_fallback_action(["fold", "call", "raise"], state).action == "fold"
        assert _safe_fallback_action(["call", "raise"], state).action == "call"
        assert _safe_fallback_action([], state).action == "fold"


class TestSessionFrames:
    async def test_a_raise_sends_the_default_placement_not_a_fold(self):
        mock_ws, exc = await _drive_session(ThrowingBot(), _ofc_script(), timeout_s=5.0)
        assert exc is None
        action = _extract_turn_actions(mock_ws.sent)[0]
        assert action["request_id"] == "req_1"
        assert action["action"] == "place"
        assert action["params"] == STREET_DEFAULT

    @pytest.mark.parametrize("name", sorted(TURNS))
    async def test_a_raise_is_legal_on_every_kind_of_turn(self, name):
        mock_ws, exc = await _drive_session(ThrowingBot(), _script(TURNS[name]()), timeout_s=5.0)
        assert exc is None
        action = _extract_turn_actions(mock_ws.sent)[0]
        assert action["action"] == "place"
        _assert_legal_placement(action["params"], GameState.from_turn_request(TURNS[name]()))

    @pytest.mark.parametrize("value", ["place", None, 7, {"action": "place"}])
    async def test_a_non_action_return_sends_the_default_placement(self, value):
        mock_ws, exc = await _drive_session(ReturningBot(value), _ofc_script(), timeout_s=5.0)
        assert exc is None
        action = _extract_turn_actions(mock_ws.sent)[0]
        assert action["action"] == "place"
        assert action["params"] == STREET_DEFAULT

    async def test_an_action_return_is_sent_as_it_is(self):
        """The bot's own answer stands, even a wrong one: the server rejects it."""
        mock_ws, exc = await _drive_session(ReturningBot(Action.fold()), _ofc_script(), timeout_s=5)
        assert exc is None
        assert _extract_turn_actions(mock_ws.sent)[0]["action"] == "fold"

    async def test_an_action_rejected_retries_with_the_default_placement(self):
        script = [*_script(_ofc_turn_request(3, "req_1")), _rejection(4, "req_1")]
        mock_ws, exc = await _drive_session(ReturningBot(Action.fold()), script, timeout_s=5.0)
        assert exc is None
        actions = _extract_turn_actions(mock_ws.sent)
        assert [(a["request_id"], a["action"]) for a in actions] == [
            ("req_1", "fold"),
            ("req_1", "place"),
        ]
        assert actions[1]["params"] == STREET_DEFAULT

    async def test_an_action_rejected_for_an_unseen_turn_sends_an_empty_placement(self):
        script = [*_script(_ofc_turn_request(3, "req_1")), _rejection(4, "req_other")]
        mock_ws, exc = await _drive_session(ReturningBot(Action.fold()), script, timeout_s=5.0)
        assert exc is None
        retry = _extract_turn_actions(mock_ws.sent)[1]
        assert retry["request_id"] == "req_other"
        assert retry["action"] == "place"
        assert retry["params"] == EMPTY_PLACEMENT

    async def test_the_retry_is_built_from_the_turn_that_was_rejected(self):
        script = [
            *_script(_deal1_turn_request(3, "req_1")),
            _ofc_turn_request(4, "req_2"),
            _rejection(5, "req_2"),
        ]
        mock_ws, exc = await _drive_session(ReturningBot(Action.fold()), script, timeout_s=5.0)
        assert exc is None
        retry = _extract_turn_actions(mock_ws.sent)[2]
        assert retry["request_id"] == "req_2"
        assert retry["params"] == STREET_DEFAULT

    async def test_a_reconnect_remembers_the_pending_turn_for_a_retry(self):
        script = [
            _server_hello(game_type="ofc_pineapple"),
            {
                "type": "reconnected",
                "match_id": MATCH_ID,
                "seq": 2,
                "round_number": 1,
                "seats": [{"seat": 1, "participant_id": "p1", "is_self": True}],
                "pending_request": _ofc_turn_request(3, "req_1"),
            },
            _rejection(4, "req_1"),
        ]
        mock_ws, exc = await _drive_session(ReturningBot(Action.fold()), script, timeout_s=5.0)
        assert exc is None
        retry = _extract_turn_actions(mock_ws.sent)[1]
        assert retry["action"] == "place"
        assert retry["params"] == STREET_DEFAULT


class TestWithoutSafeMode:
    @pytest.mark.parametrize(
        ("bot", "expected"),
        [
            (ThrowingBot(), "boom"),
            (ReturningBot("place"), "not an Action"),
        ],
    )
    async def test_a_bad_decision_on_an_ofc_turn_is_terminal(self, bot, expected):
        with pytest.raises(BotDecisionError, match=expected):
            await _run_session(
                _MockWebSocket(_ofc_script()),
                bot,
                match_id=MATCH_ID,
                token="t",
                ticket=None,
                client_name="test",
                client_version="0.0.0",
                safe_mode=False,
            )


class TestHoldemIsUnchanged:
    async def test_a_raise_at_a_holdem_table_still_folds(self):
        script = [_server_hello(), _ofc_match_start(seq=2), _turn_request(seq=3)]
        mock_ws, exc = await _drive_session(ThrowingBot(), script, timeout_s=5.0)
        assert exc is None
        assert _extract_turn_actions(mock_ws.sent)[0]["action"] == "fold"
