"""Tests for the two-layer WebSocket client flow (mock server)."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
from chipzen.bot import ChipzenBot
from chipzen.client import (
    SUPPORTED_PROTOCOL_VERSIONS,
    USER_AGENT,
    _extract_match_id,
    _import_bot,
    _run_session,
    _safe_fallback_action,
    run_bot,
)
from chipzen.models import Action, Card, GameState


class RecordingBot(ChipzenBot):
    """A bot that records all lifecycle calls for testing."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.states: list[GameState] = []
        self.match_info: dict | None = None
        self.round_results: list[dict] = []
        self.match_end_results: dict | None = None
        self.hand_results: list[dict] = []
        self.phase_changes: list[dict] = []
        self.turn_results: list[dict] = []
        self.decision_latencies: list[int] = []

    def decide(self, state: GameState) -> Action:
        self.events.append("decide")
        self.states.append(state)
        if "check" in state.valid_actions:
            return Action.check()
        if "call" in state.valid_actions:
            return Action.call()
        return Action.fold()

    def on_match_start(self, match_info: dict) -> None:
        self.events.append("match_start")
        self.match_info = match_info

    def on_round_start(self, message: dict) -> None:
        self.events.append("round_start")
        # Call super for backward-compat fan-out to on_hand_start
        super().on_round_start(message)

    def on_hand_start(self, hand_number: int, hole_cards: list[Card]) -> None:
        self.events.append("hand_start")

    def on_round_result(self, message: dict) -> None:
        self.events.append("round_result")
        self.round_results.append(message)
        super().on_round_result(message)

    def on_hand_result(self, result: dict) -> None:
        self.events.append("hand_result")
        self.hand_results.append(result)

    def on_phase_change(self, message: dict) -> None:
        self.events.append("phase_change")
        self.phase_changes.append(message)

    def on_turn_result(self, message: dict) -> None:
        self.events.append("turn_result")
        self.turn_results.append(message)

    def on_match_end(self, results: dict) -> None:
        self.events.append("match_end")
        self.match_end_results = results

    def on_decision_latency(self, latency_ms: int) -> None:
        self.events.append("decision_latency")
        self.decision_latencies.append(latency_ms)


class MockWebSocket:
    """A mock WebSocket that replays a scripted sequence of messages.

    Supports the ``async for`` iteration pattern used by the client, the
    ``send``/``recv`` coroutines used during handshake, and records every
    message the client sends.
    """

    def __init__(self, messages: list[dict]):
        self._messages = [json.dumps(m) for m in messages]
        self._index = 0
        self.sent: list[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg

    async def recv(self) -> str:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg

    async def send(self, data: str) -> None:
        self.sent.append(data)


MATCH_ID = "m_test_abc123"


def _server_hello() -> dict:
    return {
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "server_ts": "2026-04-13T14:30:05.123Z",
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "nlhe_6max",
        "capabilities": [],
    }


def _match_start() -> dict:
    return {
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": 2,
        "server_ts": "2026-04-13T14:30:06.000Z",
        "seats": [
            {"seat": 0, "participant_id": "p0", "display_name": "You", "is_self": True},
            {"seat": 1, "participant_id": "p1", "display_name": "Opp", "is_self": False},
        ],
        "game_config": {
            "variant": "nlhe",
            "starting_stack": 1000,
            "small_blind": 5,
            "big_blind": 10,
            "ante": 0,
            "total_hands": 0,
        },
        "turn_timeout_ms": 5000,
    }


def _round_start(hand_number: int = 1, seq: int = 3) -> dict:
    return {
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.000Z",
        "round_id": "r_123",
        "round_number": hand_number,
        "state": {
            "hand_number": hand_number,
            "dealer_seat": 0,
            "your_hole_cards": ["Ah", "Kd"],
            "stacks": [995, 990],
            "deck_commitment": "",
        },
    }


def _turn_request(seq: int = 4, request_id: str = "req_1") -> dict:
    return {
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.500Z",
        "seat": 0,
        "request_id": request_id,
        "timeout_ms": 5000,
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": 1,
            "phase": "preflop",
            "board": [],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 15,
            "your_stack": 995,
            "opponent_stacks": [990],
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
            "action_history": [],
        },
    }


def _turn_result(seq: int = 5) -> dict:
    return {
        "type": "turn_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:08.000Z",
        "is_timeout": False,
        "details": {"seat": 0, "action": "call", "amount": 5},
    }


def _phase_change(seq: int = 6) -> dict:
    return {
        "type": "phase_change",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:09.000Z",
        "state": {"phase": "flop", "board": ["Ts", "7h", "2c"]},
    }


def _round_result(seq: int = 7) -> dict:
    return {
        "type": "round_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:12.000Z",
        "round_id": "r_123",
        "round_number": 1,
        "result": {
            "hand_number": 1,
            "winner_seats": [0],
            "pot": 40,
            "payouts": [{"seat": 0, "amount": 40}],
            "showdown": [],
            "action_history": [],
            "stacks": [1020, 980],
            "deck_commitment": "",
            "deck_reveal": None,
        },
    }


def _match_end(seq: int = 8) -> dict:
    return {
        "type": "match_end",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:35:00.000Z",
        "reason": "complete",
        "results": [
            {"seat": 0, "participant_id": "p0", "rank": 1, "score": 1020},
            {"seat": 1, "participant_id": "p1", "rank": 2, "score": 980},
        ],
    }


@pytest.mark.asyncio
async def test_full_match_lifecycle():
    """Exercise the full handshake + round lifecycle end-to-end."""
    # Handshake messages are consumed via ``recv()``; game-loop messages
    # via the async iterator. Because our mock advances a shared index,
    # we queue them in order.
    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(),
        _turn_result(),
        _phase_change(),
        _round_result(),
        _match_end(),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="test_token",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    # Bot saw the expected lifecycle events, in order.
    assert bot.events == [
        "match_start",
        "round_start",
        "hand_start",  # fired by default on_round_start
        "decide",
        "decision_latency",  # fired after turn_action is sent
        "turn_result",
        "phase_change",
        "round_result",
        "hand_result",  # fired by default on_round_result
        "match_end",
    ]
    # The recorded latency should be a non-negative int (perf_counter
    # rounded to milliseconds). A no-op decide on a mock socket will
    # land in [0, ~few] ms in practice.
    assert len(bot.decision_latencies) == 1
    assert isinstance(bot.decision_latencies[0], int)
    assert bot.decision_latencies[0] >= 0
    assert bot.decision_latencies[0] < 5000  # very loose sanity bound

    # The client sent: authenticate, client hello, turn_action.
    sent = [json.loads(s) for s in mock_ws.sent]
    assert sent[0]["type"] == "authenticate"
    assert sent[0]["token"] == "test_token"
    assert sent[0]["match_id"] == MATCH_ID

    assert sent[1]["type"] == "hello"
    assert sent[1]["supported_versions"] == SUPPORTED_PROTOCOL_VERSIONS
    assert sent[1]["client_name"] == "chipzen-sdk-test"

    assert sent[2]["type"] == "turn_action"
    assert sent[2]["match_id"] == MATCH_ID
    assert sent[2]["request_id"] == "req_1"  # echoed
    assert sent[2]["action"] == "call"
    assert sent[2]["params"] == {}

    # GameState was populated from the nested ``state`` object.
    assert len(bot.states) == 1
    state = bot.states[0]
    assert state.phase == "preflop"
    assert state.pot == 15
    assert state.to_call == 5
    assert state.min_raise == 20
    assert state.max_raise == 995
    assert state.valid_actions == ["fold", "call", "raise"]
    assert state.your_seat == 0
    assert state.dealer_seat == 0
    assert state.request_id == "req_1"
    assert state.round_id == "r_123"
    assert [str(c) for c in state.hole_cards] == ["Ah", "Kd"]


@pytest.mark.asyncio
async def test_ping_is_answered_with_pong():
    """Every ``ping`` must elicit a ``pong`` so the server keeps the link open."""
    messages = [
        _server_hello(),
        {
            "type": "ping",
            "match_id": MATCH_ID,
            "seq": 2,
            "server_ts": "2026-04-13T14:31:00.000Z",
        },
        _match_end(seq=3),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    sent = [json.loads(s) for s in mock_ws.sent]
    types = [s["type"] for s in sent]
    assert "pong" in types
    pong = next(s for s in sent if s["type"] == "pong")
    assert pong["match_id"] == MATCH_ID


@pytest.mark.asyncio
async def test_action_rejected_triggers_safe_retry():
    """When the server rejects an action the client retries with a safe fallback."""
    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(request_id="req_retry"),
        {
            "type": "action_rejected",
            "match_id": MATCH_ID,
            "seq": 5,
            "server_ts": "2026-04-13T14:30:08.100Z",
            "request_id": "req_retry",
            "reason": "invalid_action",
            "message": "Bad action",
            "remaining_ms": 4000,
        },
        _match_end(seq=6),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    sent = [json.loads(s) for s in mock_ws.sent]
    turn_actions = [s for s in sent if s["type"] == "turn_action"]
    # First the bot's chosen action, then the safe retry.
    assert len(turn_actions) == 2
    assert turn_actions[0]["request_id"] == "req_retry"
    assert turn_actions[1]["request_id"] == "req_retry"  # same id echoed
    # Retry is check or fold.
    assert turn_actions[1]["action"] in {"check", "fold"}


@pytest.mark.asyncio
async def test_action_rejected_uses_valid_actions_from_payload():
    """When the server includes ``valid_actions`` in the rejection payload
    the client retries with a legal action instead of the blind ``check``
    fallback. Chipzen v0.3.53+ includes this field; pre-v0.3.53 servers
    omit it and the client falls back to the legacy behavior (covered by
    the previous test).

    The bug this guards against: when the bot's first action is rejected
    in a state where neither ``check`` nor ``fold`` is legal (e.g.
    ``valid_actions=["call","raise","all_in"]``), the legacy blind retry
    sent ``check`` again and the server rejected a second time, counting
    as an auto-substitute. Repeated 15× in a match, that triggered
    ``BOT_UNRESPONSIVE_AUTO_SUBSTITUTE_LIMIT`` and killed the match.
    """
    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(request_id="req_retry"),
        {
            "type": "action_rejected",
            "match_id": MATCH_ID,
            "seq": 5,
            "server_ts": "2026-05-05T09:30:00.000Z",
            "request_id": "req_retry",
            "reason": "invalid_action",
            "message": "Action 'check' not legal",
            "remaining_ms": 4000,
            "valid_actions": ["call", "raise", "all_in"],
        },
        _match_end(seq=6),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    sent = [json.loads(s) for s in mock_ws.sent]
    turn_actions = [s for s in sent if s["type"] == "turn_action"]
    assert len(turn_actions) == 2
    # Retry must come from the rejection's ``valid_actions``, not the
    # hardcoded ``["check","fold"]`` (neither of which is legal here).
    assert turn_actions[1]["action"] in {"call", "raise", "all_in"}


@pytest.mark.asyncio
async def test_bot_exception_falls_back_to_fold():
    """If ``decide`` raises, the client should submit a ``fold`` instead of crashing."""

    class CrashBot(ChipzenBot):
        def decide(self, state: GameState) -> Action:
            raise RuntimeError("Strategy error!")

    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(request_id="req_crash"),
        _match_end(seq=5),
    ]
    mock_ws = MockWebSocket(messages)
    bot = CrashBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    sent = [json.loads(s) for s in mock_ws.sent]
    turn_actions = [s for s in sent if s["type"] == "turn_action"]
    assert len(turn_actions) == 1
    assert turn_actions[0]["action"] == "fold"
    assert turn_actions[0]["request_id"] == "req_crash"


@pytest.mark.asyncio
async def test_ticket_is_used_when_token_missing():
    """Competitive endpoints authenticate with ``ticket`` instead of ``token``."""
    messages = [_server_hello(), _match_end(seq=2)]
    mock_ws = MockWebSocket(messages)

    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token=None,
        ticket="tk_abc",
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    sent = [json.loads(s) for s in mock_ws.sent]
    auth = sent[0]
    assert auth["type"] == "authenticate"
    assert auth["ticket"] == "tk_abc"
    assert "token" not in auth


@pytest.mark.asyncio
async def test_unknown_message_type_is_ignored():
    """Forward-compat: unknown message types must not raise."""
    messages = [
        _server_hello(),
        {
            "type": "future_feature_xyz",
            "match_id": MATCH_ID,
            "seq": 2,
            "server_ts": "2026-04-13T14:31:00.000Z",
            "data": {"anything": "here"},
        },
        _match_end(seq=3),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    # Should not raise.
    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    assert bot.match_end_results is not None


@pytest.mark.asyncio
async def test_handshake_aborts_when_server_does_not_send_hello():
    """If the server's first frame is not ``hello`` the session exits cleanly."""
    messages = [
        {
            "type": "error",
            "match_id": MATCH_ID,
            "seq": 1,
            "server_ts": "2026-04-13T14:30:05.000Z",
            "code": "auth_failed",
            "message": "Bad token",
        },
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="bad",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    # Only the authenticate frame should have been sent.
    sent = [json.loads(s) for s in mock_ws.sent]
    assert len(sent) == 1
    assert sent[0]["type"] == "authenticate"
    assert bot.match_end_results is None


def test_extract_match_id_handles_well_formed_urls():
    assert _extract_match_id("ws://localhost:8001/ws/match/abc-123/bot") == "abc-123"
    assert _extract_match_id("wss://api.chipzen.ai/ws/match/uuid-xyz/participant-1") == "uuid-xyz"


def test_extract_match_id_returns_unknown_for_bad_urls():
    assert _extract_match_id("ws://localhost/other/path") == "unknown"


def test_safe_fallback_prefers_check_over_fold():
    assert _safe_fallback_action(["check", "fold", "call"]).action == "check"
    assert _safe_fallback_action(["fold"]).action == "fold"
    assert _safe_fallback_action([]).action == "fold"


class TestImportBot:
    def test_invalid_specifier_raises(self):
        with pytest.raises(ValueError, match="must be"):
            _import_bot("no_colon_here")

    def test_valid_specifier_with_nonexistent_module(self):
        with pytest.raises((ImportError, ModuleNotFoundError)):
            _import_bot("nonexistent_module_xyz:SomeBot")


# ---------------------------------------------------------------------------
# on_decision_latency hook (Issue #46)
# ---------------------------------------------------------------------------


def test_user_agent_header_uses_chipzen_sdk_python_with_version():
    """The SDK identifies itself with a non-default UA so Cloudflare
    bot-fight doesn't reject the handshake on the chipzen.ai zone.

    This is defense-in-depth alongside the server-side path allowlist
    (Chipzen #2222 / Issue 19); if either side regresses the other
    keeps bots connecting.
    """
    assert USER_AGENT.startswith("chipzen-sdk-python/")
    # Ends with the SDK version, not the default ``Python/X.Y websockets/Z.Z``.
    assert USER_AGENT.endswith(chipzen.__version__)
    # No spaces — keeps the header valid and avoids accidental
    # ``Python/...`` substrings that would still match Cloudflare's
    # default-library heuristic.
    assert " " not in USER_AGENT


def test_on_decision_latency_default_is_noop():
    """Subclasses that don't override the hook still work."""

    class MinimalBot(ChipzenBot):
        def decide(self, state: GameState) -> Action:
            return Action.fold()

    bot = MinimalBot()
    # Must not raise, must not return anything meaningful.
    assert bot.on_decision_latency(0) is None
    assert bot.on_decision_latency(123) is None


@pytest.mark.asyncio
async def test_on_decision_latency_fires_with_nonneg_value():
    """The hook fires exactly once per turn_action and reports a
    non-negative integer latency.
    """
    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(),
        _match_end(seq=5),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    # Exactly one turn was dispatched -> exactly one latency sample.
    assert len(bot.decision_latencies) == 1
    sample = bot.decision_latencies[0]
    assert isinstance(sample, int)
    assert sample >= 0
    # decide() is trivial and the socket is a Python mock -- a real
    # turn on a real socket would still land well under one second.
    assert sample < 5000


@pytest.mark.asyncio
async def test_on_decision_latency_reflects_slow_decide():
    """A deliberately slow ``decide`` shows up as a noticeably larger
    latency value than a fast one. We verify both ordering and
    magnitude — a 50ms sleep should be measured as >= ~40ms (allowing
    for clock fuzz on busy CI runners).
    """
    import asyncio as _asyncio

    class SlowBot(ChipzenBot):
        def __init__(self, sleep_seconds: float) -> None:
            self.sleep_seconds = sleep_seconds
            self.latencies: list[int] = []

        def decide(self, state: GameState) -> Action:
            # Synchronous sleep so perf_counter advances the same way
            # a real CPU-bound bot would.
            import time as _time

            _time.sleep(self.sleep_seconds)
            return Action.fold()

        def on_decision_latency(self, latency_ms: int) -> None:
            self.latencies.append(latency_ms)

    fast = SlowBot(sleep_seconds=0.0)
    slow = SlowBot(sleep_seconds=0.05)  # ~50ms

    for bot in (fast, slow):
        messages = [
            _server_hello(),
            _match_start(),
            _round_start(),
            _turn_request(),
            _match_end(seq=5),
        ]
        await _run_session(
            MockWebSocket(messages),
            bot,
            match_id=MATCH_ID,
            token="",
            ticket=None,
            client_name="chipzen-sdk-test",
            client_version="0.2.0",
        )

    assert len(fast.latencies) == 1
    assert len(slow.latencies) == 1
    # The slow bot's measured latency is at least 40ms.
    assert slow.latencies[0] >= 40, f"expected slow.latencies[0] >= 40, got {slow.latencies[0]}"
    # And meaningfully larger than the fast one.
    assert slow.latencies[0] > fast.latencies[0]


@pytest.mark.asyncio
async def test_on_decision_latency_hook_exception_does_not_kill_session():
    """A user hook that raises must not bring down the session loop --
    observability should never break gameplay.
    """

    class BadHookBot(ChipzenBot):
        def __init__(self) -> None:
            self.decide_count = 0

        def decide(self, state: GameState) -> Action:
            self.decide_count += 1
            return Action.fold()

        def on_decision_latency(self, latency_ms: int) -> None:
            raise RuntimeError("user-hook explosion")

    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(),
        _match_end(seq=5),
    ]
    mock_ws = MockWebSocket(messages)
    bot = BadHookBot()

    # Should resolve cleanly despite the hook raising.
    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    # The session still completed, so decide was called and the
    # turn_action was sent.
    assert bot.decide_count == 1
    sent = [json.loads(s) for s in mock_ws.sent]
    assert any(s["type"] == "turn_action" for s in sent)


@pytest.mark.asyncio
async def test_on_decision_latency_reconnected_path_hook_exception_does_not_kill_session():
    """The reconnect path also has its own latency-hook call; verify
    a raising hook there doesn't take down the session either.
    """

    class BadHookBot(ChipzenBot):
        def __init__(self) -> None:
            self.decide_count = 0

        def decide(self, state: GameState) -> Action:
            self.decide_count += 1
            return Action.fold()

        def on_decision_latency(self, latency_ms: int) -> None:
            raise RuntimeError("user-hook explosion on reconnect")

    pending = _turn_request(seq=99, request_id="req_pending_bad")
    pending = {k: v for k, v in pending.items() if k != "seq"}
    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        {
            "type": "reconnected",
            "match_id": MATCH_ID,
            "seq": 4,
            "round_number": 1,
            "pending_request": pending,
        },
        _match_end(seq=5),
    ]
    mock_ws = MockWebSocket(messages)
    bot = BadHookBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    assert bot.decide_count == 1
    sent = [json.loads(s) for s in mock_ws.sent]
    assert any(s.get("type") == "turn_action" for s in sent)


@pytest.mark.asyncio
async def test_on_decision_latency_fires_for_reconnected_pending_request():
    """The ``reconnected`` path also dispatches a turn_action — the
    latency hook must fire there too so devs see every decision.
    """
    pending = _turn_request(seq=99, request_id="req_pending")
    # Strip ``seq`` so it doesn't conflict with the outer envelope.
    pending = {k: v for k, v in pending.items() if k != "seq"}
    messages = [
        _server_hello(),
        _match_start(),
        _round_start(),
        {
            "type": "reconnected",
            "match_id": MATCH_ID,
            "seq": 4,
            "round_number": 1,
            "pending_request": pending,
        },
        _match_end(seq=5),
    ]
    mock_ws = MockWebSocket(messages)
    bot = RecordingBot()

    await _run_session(
        mock_ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
    )

    # Exactly one decision was made (the pending one).
    assert len(bot.decision_latencies) == 1
    assert bot.decision_latencies[0] >= 0


@pytest.mark.asyncio
async def test_run_bot_passes_chipzen_user_agent_to_connect():
    """``run_bot`` must propagate the chipzen-sdk-python UA into the
    ``websockets.connect`` handshake — otherwise the Cloudflare bot-
    fight workaround is silently inert.
    """
    # Fake ``connect`` async context manager that captures the kwargs.
    captured: dict = {}

    class _FakeWS:
        async def send(self, data: str) -> None:  # pragma: no cover - unused
            pass

        async def recv(self) -> str:  # pragma: no cover - unused
            return json.dumps(_server_hello())

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeConnect:
        def __init__(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs

        async def __aenter__(self):
            return _FakeWS()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    fake_module = MagicMock()
    fake_module.connect = _FakeConnect

    # Patch the import inside run_bot. The function tries
    # ``from websockets.asyncio.client import connect`` first; replace
    # that module so our fake is used.
    with patch.dict(
        sys.modules,
        {"websockets.asyncio.client": fake_module},
    ):
        # _run_session will exit immediately on the StopAsyncIteration
        # from our fake reader because the only frame is the server
        # hello and the loop terminates when no further messages arrive.
        bot = RecordingBot()
        await run_bot(
            "ws://localhost:8001/ws/match/m_test/bot",
            bot,
            max_retries=0,
            token="",
        )

    assert "user_agent_header" in captured["kwargs"]
    assert captured["kwargs"]["user_agent_header"] == USER_AGENT
    assert captured["kwargs"]["user_agent_header"].startswith("chipzen-sdk-python/")
