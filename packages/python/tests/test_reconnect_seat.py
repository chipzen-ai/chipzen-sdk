"""Seat and button after a real reconnect (chipzen-ai/chipzen-sdk#199).

The match server sends no ``reconnected`` frame. A reconnecting bot runs the
handshake again and the new session receives the next game frames, so it never
sees ``match_start`` (the seat) or the current hand's ``round_start`` (the
button). The SDK must then take the seat from ``turn_request.seat`` and work
out the button from the hand's blinds.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot
from chipzen.client import _run_session
from chipzen.models import Action, GameState, _derive_dealer_seat, _turn_seat_context

MATCH_ID = "m_reconnect_199"


class SeatRecordingBot(ChipzenBot):
    def __init__(self) -> None:
        self.states: list[GameState] = []

    def decide(self, state: GameState) -> Action:
        self.states.append(state)
        return Action.check() if "check" in state.valid_actions else Action.fold()


class ScriptedWebSocket:
    def __init__(self, messages: list[dict]):
        self._messages = [json.dumps(m) for m in messages]
        self._index = 0
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        self._index += 1
        return self._messages[self._index - 1]

    async def recv(self) -> str:
        return await self.__anext__()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


def _hello() -> dict:
    return {
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "capabilities": ["reconnect"],
    }


def _entry(seat: int, action: str, amount: int) -> dict:
    return {"seat": seat, "action": action, "amount": amount, "phase": "preflop"}


def _turn(seq: int, seat: int, hand: int, stacks: list[int], history: list[dict]) -> dict:
    return {
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seq": seq,
        "seat": seat,
        "request_id": f"req_{seq}",
        "timeout_ms": 5000,
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": hand,
            "phase": "preflop",
            "board": [],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 25,
            "your_stack": 995,
            "opponent_stacks": stacks,
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
            "action_history": history,
        },
    }


def _round_start(seq: int, hand: int, dealer: int) -> dict:
    return {
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "round_id": f"r_{hand}",
        "round_number": hand,
        "state": {"hand_number": hand, "dealer_seat": dealer, "your_hole_cards": ["2c", "7d"]},
    }


async def _run(messages: list[dict]) -> tuple[SeatRecordingBot, ScriptedWebSocket]:
    ws = ScriptedWebSocket(messages)
    bot = SeatRecordingBot()
    await _run_session(
        ws,
        bot,
        match_id=MATCH_ID,
        token="t",
        ticket=None,
        client_name="test",
        client_version="0",
    )
    return bot, ws


@pytest.mark.asyncio
async def test_reconnected_session_reads_seat_and_button_without_match_start():
    """A 3-seat table: the bot is seat 2 and seat 1 has the button. The first
    connection would have learned both; the reconnected session sees neither
    ``match_start`` nor this hand's ``round_start``, and used to read seat 0
    and button 0."""
    history = [
        _entry(2, "post_small_blind", 5),
        _entry(0, "post_big_blind", 10),
        _entry(1, "call", 10),
    ]
    bot, ws = await _run(
        [
            _hello(),
            # seq continues from the dropped connection
            _turn(seq=41, seat=2, hand=7, stacks=[990, 990], history=history),
            _round_start(seq=45, hand=8, dealer=2),
            _turn(
                seq=46,
                seat=2,
                hand=8,
                stacks=[995, 990],
                history=[_entry(0, "post_small_blind", 5), _entry(1, "post_big_blind", 10)],
            ),
        ]
    )
    assert [(s.your_seat, s.dealer_seat) for s in bot.states] == [(2, 1), (2, 2)]
    assert [m["request_id"] for m in ws.sent if m["type"] == "turn_action"] == [
        "req_41",
        "req_46",
    ]


@pytest.mark.asyncio
async def test_reconnected_heads_up_button_posts_the_small_blind():
    history = [_entry(1, "post_small_blind", 5), _entry(0, "post_big_blind", 10)]
    bot, _ = await _run([_hello(), _turn(seq=9, seat=1, hand=3, stacks=[990], history=history)])
    assert (bot.states[0].your_seat, bot.states[0].dealer_seat) == (1, 1)


@pytest.mark.asyncio
async def test_session_that_saw_match_start_and_round_start_is_unchanged():
    """Normal play: the roster seat and the ``round_start`` button are used as
    before, whatever the blinds would suggest."""
    match_start = {
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": 2,
        "seats": [{"seat": 0, "is_self": False}, {"seat": 1, "is_self": True}],
        "game_config": {},
    }
    history = [_entry(0, "post_small_blind", 5), _entry(1, "post_big_blind", 10)]
    bot, _ = await _run(
        [
            _hello(),
            match_start,
            _round_start(seq=3, hand=1, dealer=1),
            _turn(seq=4, seat=1, hand=1, stacks=[995], history=history),
        ]
    )
    assert (bot.states[0].your_seat, bot.states[0].dealer_seat) == (1, 1)


def test_derive_dealer_skips_busted_seats():
    """POKER-GAME-STATE-PROTOCOL section 5.9's example: 6 seats, seats 1, 2
    and 5 busted, button 0, so seat 3 posts the small blind."""
    state = {
        "opponent_stacks": [1500, 0, 0, 1200, 0],  # seats 0, 1, 2, 4, 5
        "action_history": [_entry(3, "post_small_blind", 5), _entry(4, "post_big_blind", 10)],
    }
    assert _derive_dealer_seat(state, your_seat=3) == 0


def test_derive_dealer_wraps_and_counts_all_in_seats_as_live():
    # Seat 0 is the small blind, so the button is the last live seat (4).
    # Seat 4 is all-in (stack 0) but has acted, so it is still live.
    state = {
        "opponent_stacks": [990, 0, 0, 0],  # seats 1, 2, 3, 4
        "action_history": [
            _entry(0, "post_small_blind", 5),
            _entry(1, "post_big_blind", 10),
            _entry(4, "raise", 300),
        ],
    }
    assert _derive_dealer_seat(state, your_seat=0) == 4


def test_derive_dealer_heads_up_at_a_six_seat_table():
    state = {
        "opponent_stacks": [0, 0, 1990, 0, 0],  # seats 0, 1, 3, 4, 5
        "action_history": [_entry(3, "post_small_blind", 5), _entry(2, "post_big_blind", 10)],
    }
    assert _derive_dealer_seat(state, your_seat=2) == 3


def test_derive_dealer_needs_a_small_blind():
    assert _derive_dealer_seat({"opponent_stacks": [100], "action_history": []}, 0) is None
    # An OFC-shaped state: no blinds at all.
    assert _derive_dealer_seat({"your_rows": {}}, 1) is None


def test_turn_seat_context_keeps_values_the_session_saw():
    message = _turn(
        seq=5,
        seat=2,
        hand=1,
        stacks=[990, 990],
        history=[_entry(2, "post_small_blind", 5), _entry(0, "post_big_blind", 10)],
    )
    assert _turn_seat_context(
        message, your_seat=0, dealer_seat=0, seat_known=False, dealer_known=False
    ) == (2, 1)
    assert _turn_seat_context(
        message, your_seat=0, dealer_seat=0, seat_known=True, dealer_known=True
    ) == (0, 0)
    # No Layer 1 seat and no blinds: today's fallback.
    assert _turn_seat_context(
        {"state": {}}, your_seat=0, dealer_seat=0, seat_known=False, dealer_known=False
    ) == (0, 0)
