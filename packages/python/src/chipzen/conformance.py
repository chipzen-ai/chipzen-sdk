"""Protocol-conformance harness used by ``chipzen-sdk validate --check-connectivity``.

The job here is narrow: drive a bot instance through a canned protocol
exchange (handshake + one full hand + match_end) and report whether the
bot completed it without exception and emitted valid ``turn_action``
messages. Pure connectivity — no judgement of bot strength.

A clean run means the upload pipeline will accept the bot on protocol
grounds. It does **not** mean the bot is good.

Implementation note: this drives ``chipzen.client._run_session`` against
an in-process mock WebSocket rather than spinning up a real server.
The transport layer (``websockets``) is well-tested upstream; what we
verify here is the bot's own protocol handling, which is the only part
the user's code influences.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Literal

from chipzen.bot import ChipzenBot
from chipzen.client import _run_session

Severity = Literal["pass", "warn", "fail"]


@dataclass
class ConformanceCheck:
    """Single conformance scenario result.

    Mirrors the shape of the ``(severity, name, message)`` tuples emitted
    by the rest of ``validate.py`` so the CLI can render them uniformly.
    """

    severity: Severity
    name: str
    message: str


# ---------------------------------------------------------------------------
# Mock WebSocket — replays a scripted message sequence
# ---------------------------------------------------------------------------

# These fixtures intentionally mirror the shape of the real protocol
# fixtures in ``tests/test_client.py``. Future cleanup can dedupe
# (move both to a shared internal module); for the conformance harness
# we keep them local so this module is self-contained.

MATCH_ID = "m_conformance_test"


class _MockWebSocket:
    """A scripted WebSocket that the SDK's session loop can drive.

    Supports the ``async for`` iterator pattern the client uses for the
    main message loop, plus ``send`` / ``recv`` for the handshake. Every
    message the bot sends is captured in ``self.sent`` for post-scenario
    verification.
    """

    def __init__(self, messages: list[dict]) -> None:
        self._messages = [json.dumps(m) for m in messages]
        self._index = 0
        self.sent: list[str] = []

    def __aiter__(self) -> "_MockWebSocket":
        return self

    async def __anext__(self) -> str:
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


# ---------------------------------------------------------------------------
# Canned protocol messages — one full hand
# ---------------------------------------------------------------------------


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


def _round_start(seq: int = 3) -> dict:
    return {
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.000Z",
        "round_id": "r_1",
        "round_number": 1,
        "state": {
            "hand_number": 1,
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


def _round_result(seq: int = 6) -> dict:
    return {
        "type": "round_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:12.000Z",
        "round_id": "r_1",
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


def _match_end(seq: int = 7) -> dict:
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


def _phase_change(seq: int, phase: str, board: list[str]) -> dict:
    return {
        "type": "phase_change",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:09.000Z",
        "state": {"phase": phase, "board": board},
    }


def _action_rejected(seq: int, request_id: str = "req_1", remaining_ms: int = 4000) -> dict:
    """Server-side rejection of a previously-sent ``turn_action``.

    Drives the SDK's safe-fallback retry path. The SDK should respond with
    a ``turn_action`` echoing this same ``request_id`` and a safe action
    (``check`` or ``fold``) within ``remaining_ms``.
    """
    return {
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:08.000Z",
        "request_id": request_id,
        "reason": "invalid_action",
        "message": "action not in valid_actions",
        "remaining_ms": remaining_ms,
    }


def _full_match_script() -> list[dict]:
    """One handshake + one hand + match_end. Smallest end-to-end exchange."""
    return [
        _server_hello(),
        _match_start(),
        _round_start(),
        _turn_request(),
        _turn_result(),
        _round_result(),
        _match_end(),
    ]


def _multi_turn_script() -> list[dict]:
    """Three turn_requests across preflop/flop/turn — exercises request_id echo on every turn.

    The original ``_full_match_script`` only checks the first ``turn_action``;
    a bug where the second action drops or mangles the ``request_id`` would
    pass the basic harness silently. This script drives three turns and
    verifies all three ``request_ids`` round-trip.
    """
    return [
        _server_hello(),
        _match_start(),
        _round_start(seq=3),
        _turn_request(seq=4, request_id="req_1"),
        _turn_result(seq=5),
        _phase_change(seq=6, phase="flop", board=["2s", "7d", "Tc"]),
        _turn_request(seq=7, request_id="req_2"),
        _turn_result(seq=8),
        _phase_change(seq=9, phase="turn", board=["2s", "7d", "Tc", "Kh"]),
        _turn_request(seq=10, request_id="req_3"),
        _turn_result(seq=11),
        _round_result(seq=12),
        _match_end(seq=13),
    ]


def _action_rejected_script() -> list[dict]:
    """One ``turn_request`` followed by ``action_rejected`` — exercises the safe-fallback retry.

    The full-match script never delivers an ``action_rejected``, so the
    SDK's retry path goes untested in conformance even though it's a
    common production code path (raise amount out of bounds, action kind
    not in valid_actions, etc.). This scenario verifies the SDK responds
    to the rejection with a second ``turn_action`` echoing the same
    ``request_id`` and using a safe (``check`` or ``fold``) action.
    """
    return [
        _server_hello(),
        _match_start(),
        _round_start(seq=3),
        _turn_request(seq=4, request_id="req_1"),
        _action_rejected(seq=5, request_id="req_1"),
        _turn_result(seq=6),
        _round_result(seq=7),
        _match_end(seq=8),
    ]


def _retry_storm_script() -> list[dict]:
    """One ``turn_request`` followed by THREE consecutive ``action_rejected`` messages.

    Catches a class of failure where a buggy SDK might enter an infinite
    response loop, or hang waiting for a non-rejection message that
    never arrives. The fix here is bounded reactivity: the SDK should
    only respond to messages the server actually sends, and the script
    must terminate cleanly on ``match_end``.
    """
    return [
        _server_hello(),
        _match_start(),
        _round_start(seq=3),
        _turn_request(seq=4, request_id="req_1"),
        _action_rejected(seq=5, request_id="req_1"),
        _action_rejected(seq=6, request_id="req_1"),
        _action_rejected(seq=7, request_id="req_1"),
        _turn_result(seq=8),
        _round_result(seq=9),
        _match_end(seq=10),
    ]


# ---------------------------------------------------------------------------
# Scenario evaluation
# ---------------------------------------------------------------------------


def _classify_turn_action(payload: str, expected_request_id: str = "req_1") -> tuple[bool, str]:
    """Return ``(is_valid, message)`` for a captured client-side payload.

    Valid means: parseable JSON, ``type == "turn_action"``, includes a
    ``request_id`` matching ``expected_request_id``, and an ``action`` in
    the legal set.

    Non-``turn_action`` payloads (e.g. ``authenticate``, ``hello``) return
    ``(True, ...)`` so callers can use this on the captured-send buffer
    without filtering first.
    """
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError as exc:
        return False, f"sent payload was not valid JSON: {exc}"
    if msg.get("type") != "turn_action":
        return True, f"non-action message ({msg.get('type')!r}) — ignored"
    if msg.get("request_id") != expected_request_id:
        return False, (
            f"turn_action request_id {msg.get('request_id')!r} did not echo the "
            f"server's {expected_request_id!r} — the server uses request_id for "
            "correlation, idempotency, and action_rejected retries"
        )
    params = msg.get("params") or {}
    action = params.get("action") or msg.get("action")
    if action not in {"fold", "check", "call", "raise", "all_in"}:
        return False, f"turn_action action {action!r} is not in the legal set"
    return True, f"sent turn_action: action={action!r}"


def _extract_turn_actions(sent: list[str]) -> list[dict]:
    """Filter the captured-send buffer down to parsed ``turn_action`` payloads."""
    out: list[dict] = []
    for payload in sent:
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "turn_action":
            out.append(msg)
    return out


async def _drive_session(
    bot: ChipzenBot, script: list[dict], *, timeout_s: float
) -> tuple[_MockWebSocket | None, BaseException | None]:
    """Helper: drive ``_run_session`` against ``script`` with a per-scenario timeout.

    Returns ``(mock_ws, None)`` on clean completion, ``(None, exc)`` on the
    inner ``_run_session`` raising or the asyncio timeout firing. Callers
    inspect ``mock_ws.sent`` for what the bot emitted.

    Note: ``asyncio.wait_for`` only cancels at ``await`` points. A bot whose
    ``decide()`` busy-loops or calls a long blocking function will block the
    event loop and prevent the timeout from firing on time. The hard
    watchdog in ``_run_with_hard_timeout`` is the second line of defense.
    """
    mock_ws = _MockWebSocket(script)
    try:
        await asyncio.wait_for(
            _run_session(
                mock_ws,
                bot,
                match_id=MATCH_ID,
                token="conformance",
                ticket=None,
                client_name="chipzen-sdk-conformance",
                client_version="0.0.0",
            ),
            timeout=timeout_s,
        )
        return mock_ws, None
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        return None, exc


async def _run_full_match_scenario(bot: ChipzenBot, timeout_s: float) -> ConformanceCheck:
    """Drive ``_run_session`` against a full-match script, with a timeout."""
    name = "connectivity_full_match"
    mock_ws, exc = await _drive_session(bot, _full_match_script(), timeout_s=timeout_s)
    if exc is not None or mock_ws is None:
        if isinstance(exc, asyncio.TimeoutError):
            return ConformanceCheck(
                "fail",
                name,
                f"bot did not complete the canned full-match exchange within {timeout_s}s — "
                "either decide() is too slow or the bot is hung waiting on something",
            )
        return ConformanceCheck(
            "fail",
            name,
            f"bot raised {type(exc).__name__} during the canned exchange: {exc}",
        )

    if not mock_ws.sent:
        return ConformanceCheck(
            "fail",
            name,
            "bot did not send any messages during the canned exchange — at minimum the "
            "client should have sent authenticate / hello / turn_action",
        )

    turn_actions = _extract_turn_actions(mock_ws.sent)
    if not turn_actions:
        return ConformanceCheck(
            "fail",
            name,
            "bot completed the exchange but never sent a turn_action — decide() may "
            "have returned an unexpected value or the SDK's runner hit a fallback path",
        )

    ok, detail = _classify_turn_action(json.dumps(turn_actions[0]))
    if not ok:
        return ConformanceCheck("fail", name, detail)

    return ConformanceCheck(
        "pass",
        name,
        f"completed handshake + 1 hand + match_end; {detail}",
    )


async def _run_multi_turn_scenario(bot: ChipzenBot, timeout_s: float) -> ConformanceCheck:
    """Drive three turn_requests and verify request_id is echoed correctly on each.

    The full-match scenario only checks the first action. A bug where the
    second-or-later action drops or rewrites the ``request_id`` would slip
    through. This scenario verifies all three round-trip.
    """
    name = "multi_turn_request_id_echo"
    mock_ws, exc = await _drive_session(bot, _multi_turn_script(), timeout_s=timeout_s)
    if exc is not None or mock_ws is None:
        if isinstance(exc, asyncio.TimeoutError):
            return ConformanceCheck(
                "fail",
                name,
                f"bot did not complete the multi-turn exchange within {timeout_s}s",
            )
        return ConformanceCheck(
            "fail",
            name,
            f"bot raised {type(exc).__name__} during the multi-turn exchange: {exc}",
        )

    turn_actions = _extract_turn_actions(mock_ws.sent)
    expected_ids = ["req_1", "req_2", "req_3"]

    if len(turn_actions) < len(expected_ids):
        return ConformanceCheck(
            "fail",
            name,
            f"expected {len(expected_ids)} turn_actions across preflop/flop/turn, "
            f"saw only {len(turn_actions)} — bot stopped responding partway through the hand",
        )

    for i, expected_id in enumerate(expected_ids):
        ok, detail = _classify_turn_action(json.dumps(turn_actions[i]), expected_id)
        if not ok:
            return ConformanceCheck(
                "fail",
                name,
                f"turn {i + 1} of 3 failed: {detail}",
            )

    return ConformanceCheck(
        "pass",
        name,
        f"all {len(expected_ids)} turn_actions echoed request_id correctly across "
        "preflop/flop/turn",
    )


async def _run_action_rejected_scenario(bot: ChipzenBot, timeout_s: float) -> ConformanceCheck:
    """Drive a turn_request followed by action_rejected and verify the SDK retries safely.

    On rejection the SDK should send a second ``turn_action`` echoing the
    same ``request_id`` and using a safe action (``check`` or ``fold``).
    Pre-this-scenario, the harness had no coverage of the rejection path
    even though it's a routine production code path.
    """
    name = "action_rejected_recovery"
    mock_ws, exc = await _drive_session(bot, _action_rejected_script(), timeout_s=timeout_s)
    if exc is not None or mock_ws is None:
        if isinstance(exc, asyncio.TimeoutError):
            return ConformanceCheck(
                "fail",
                name,
                f"bot did not complete the action_rejected scenario within {timeout_s}s — "
                "the SDK's safe-fallback retry path may be hung",
            )
        return ConformanceCheck(
            "fail",
            name,
            f"bot raised {type(exc).__name__} during action_rejected handling: {exc}",
        )

    turn_actions = _extract_turn_actions(mock_ws.sent)
    if len(turn_actions) < 2:
        return ConformanceCheck(
            "fail",
            name,
            f"expected 2 turn_actions (initial + safe-fallback retry), saw {len(turn_actions)}; "
            "the SDK did not respond to the action_rejected message",
        )

    retry = turn_actions[1]
    if retry.get("request_id") != "req_1":
        return ConformanceCheck(
            "fail",
            name,
            f"safe-fallback retry used request_id {retry.get('request_id')!r} instead of "
            "the original 'req_1' — server-side correlation will fail",
        )

    retry_action = (retry.get("params") or {}).get("action") or retry.get("action")
    if retry_action not in {"check", "fold"}:
        return ConformanceCheck(
            "fail",
            name,
            f"safe-fallback retry sent action {retry_action!r}; expected 'check' or 'fold' "
            "(the only universally-safe actions when valid_actions is unknown)",
        )

    return ConformanceCheck(
        "pass",
        name,
        f"action_rejected handled cleanly: original action sent, retry sent {retry_action!r} "
        "with original request_id",
    )


async def _run_retry_storm_scenario(bot: ChipzenBot, timeout_s: float) -> ConformanceCheck:
    """Drive a turn_request followed by THREE action_rejected messages back-to-back.

    Catches a class of failure where a buggy SDK might hang after the
    first rejection or enter an infinite send loop. The SDK is expected
    to respond to each rejection with a safe-fallback ``turn_action`` and
    exit cleanly when ``match_end`` arrives — purely reactive, never
    initiating sends on its own.
    """
    name = "retry_storm_bounded"
    mock_ws, exc = await _drive_session(bot, _retry_storm_script(), timeout_s=timeout_s)
    if exc is not None or mock_ws is None:
        if isinstance(exc, asyncio.TimeoutError):
            return ConformanceCheck(
                "fail",
                name,
                f"bot did not complete the retry-storm scenario within {timeout_s}s — "
                "the SDK may be stuck in a retry loop",
            )
        return ConformanceCheck(
            "fail",
            name,
            f"bot raised {type(exc).__name__} during retry-storm handling: {exc}",
        )

    turn_actions = _extract_turn_actions(mock_ws.sent)
    # Expected: 1 initial + 3 retries = 4 turn_actions total. The SDK is
    # reactive: each action_rejected provokes exactly one retry.
    expected_count = 4
    if len(turn_actions) != expected_count:
        severity: Severity = "fail" if len(turn_actions) < expected_count else "warn"
        return ConformanceCheck(
            severity,
            name,
            f"expected {expected_count} turn_actions (1 initial + 3 retries) under retry "
            f"storm, saw {len(turn_actions)} — the SDK's retry behavior may be unbounded "
            "or may have stopped responding",
        )

    return ConformanceCheck(
        "pass",
        name,
        f"SDK responded to all 3 action_rejected messages with safe-fallback retries "
        f"({expected_count} turn_actions total) and exited cleanly on match_end",
    )


# ---------------------------------------------------------------------------
# Hard watchdog for fully-blocked event loops
# ---------------------------------------------------------------------------


def _run_with_hard_timeout(
    name: str,
    coro_factory: Callable[[], Coroutine[Any, Any, ConformanceCheck]],
    *,
    inner_timeout_s: float,
    hard_timeout_s: float,
) -> ConformanceCheck:
    """Run an async scenario in a daemon thread with a hard wall-clock timeout.

    ``asyncio.wait_for`` only cancels at ``await`` points. A bot whose
    ``decide()`` busy-loops (``while True: pass``) blocks the event loop
    and the inner ``wait_for`` never fires. This wrapper guarantees the
    harness terminates by running the entire ``asyncio.run`` in a daemon
    thread and abandoning it if it overruns ``hard_timeout_s``.

    The abandoned thread continues to run after this function returns —
    daemonic, so the process won't wait for it on exit. That's acceptable
    for a CLI invocation; a true production runtime would need explicit
    decide-side preemption (executor + cancel) which is out of scope here.
    """

    def runner() -> ConformanceCheck:
        return asyncio.run(coro_factory())

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="chipzen-conformance"
    ) as pool:
        future = pool.submit(runner)
        try:
            return future.result(timeout=hard_timeout_s)
        except concurrent.futures.TimeoutError:
            # Don't block waiting for the hung thread to finish; let the
            # context manager's __exit__ cancel pending work and return.
            pool.shutdown(wait=False, cancel_futures=True)
            return ConformanceCheck(
                "fail",
                name,
                f"harness watchdog terminated the scenario after {hard_timeout_s}s — "
                f"decide() likely entered a busy-loop or blocking call (the inner "
                f"asyncio timeout of {inner_timeout_s}s could not fire because the "
                "event loop was blocked)",
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_ScenarioFn = Callable[[ChipzenBot, float], Coroutine[Any, Any, ConformanceCheck]]

SCENARIOS: list[tuple[str, _ScenarioFn]] = [
    ("connectivity_full_match", _run_full_match_scenario),
    ("multi_turn_request_id_echo", _run_multi_turn_scenario),
    ("action_rejected_recovery", _run_action_rejected_scenario),
    ("retry_storm_bounded", _run_retry_storm_scenario),
]


def _make_factory(
    fn: _ScenarioFn, bot: ChipzenBot, timeout_s: float
) -> Callable[[], Coroutine[Any, Any, ConformanceCheck]]:
    """Bind a scenario function to its arguments without losing precise types.

    The lambda equivalent of this function is rejected by mypy because
    its return type cannot be inferred when the lambda is built inside a
    list comprehension. This nested function makes the binding explicit
    and types check cleanly.
    """

    def factory() -> Coroutine[Any, Any, ConformanceCheck]:
        return fn(bot, timeout_s)

    return factory


def run_conformance_checks(bot: ChipzenBot, *, timeout_s: float = 10.0) -> list[ConformanceCheck]:
    """Run every conformance scenario against ``bot`` and return per-check results.

    Args:
        bot: An instance of the user's ``Bot`` subclass — same instance the
            user would call ``.run()`` on in production.
        timeout_s: Per-scenario timeout. Default 10 seconds, which is well
            above the platform's per-action 5-second budget but still bounded
            for CI use. The harness's hard wall-clock watchdog uses
            ``timeout_s + 5``; bots whose ``decide()`` blocks the event loop
            (busy-loops, sync ``time.sleep`` longer than the timeout) are
            still detected, just on the slower path.

    Returns:
        A list of ``ConformanceCheck`` results, one per scenario.
    """
    hard_timeout = timeout_s + 5.0
    return [
        _run_with_hard_timeout(
            name,
            _make_factory(fn, bot, timeout_s),
            inner_timeout_s=timeout_s,
            hard_timeout_s=hard_timeout,
        )
        for name, fn in SCENARIOS
    ]
