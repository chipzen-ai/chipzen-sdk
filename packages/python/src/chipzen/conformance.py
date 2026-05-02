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
import json
from dataclasses import dataclass
from typing import Literal

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


# ---------------------------------------------------------------------------
# Scenario evaluation
# ---------------------------------------------------------------------------


def _classify_turn_action(payload: str) -> tuple[bool, str]:
    """Return ``(is_valid, message)`` for a captured client-side payload.

    Valid means: parseable JSON, ``type == "turn_action"``, includes a
    ``request_id`` matching the canned request, and an ``action`` in
    the legal set.
    """
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError as exc:
        return False, f"sent payload was not valid JSON: {exc}"
    if msg.get("type") != "turn_action":
        return True, f"non-action message ({msg.get('type')!r}) — ignored"
    if msg.get("request_id") != "req_1":
        return False, (
            f"turn_action request_id {msg.get('request_id')!r} did not echo the "
            "server's req_1 — the server uses request_id for correlation, idempotency, "
            "and action_rejected retries"
        )
    params = msg.get("params") or {}
    action = params.get("action") or msg.get("action")
    if action not in {"fold", "check", "call", "raise", "all_in"}:
        return False, f"turn_action action {action!r} is not in the legal set"
    return True, f"sent turn_action: action={action!r}"


async def _run_full_match_scenario(bot: ChipzenBot, timeout_s: float) -> ConformanceCheck:
    """Drive ``_run_session`` against a full-match script, with a timeout."""
    name = "connectivity_full_match"
    mock_ws = _MockWebSocket(_full_match_script())
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
    except asyncio.TimeoutError:
        return ConformanceCheck(
            "fail",
            name,
            f"bot did not complete the canned full-match exchange within {timeout_s}s — "
            "either decide() is too slow or the bot is hung waiting on something",
        )
    except Exception as exc:  # noqa: BLE001 — surface anything the bot raised
        return ConformanceCheck(
            "fail",
            name,
            f"bot raised {type(exc).__name__} during the canned exchange: {exc}",
        )

    # Bot completed the script without exception — verify it sent at least
    # one valid turn_action.
    if not mock_ws.sent:
        return ConformanceCheck(
            "fail",
            name,
            "bot did not send any messages during the canned exchange — at minimum the "
            "client should have sent authenticate / hello / turn_action",
        )

    turn_actions = []
    for payload in mock_ws.sent:
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "turn_action":
            turn_actions.append(payload)

    if not turn_actions:
        return ConformanceCheck(
            "fail",
            name,
            "bot completed the exchange but never sent a turn_action — decide() may "
            "have returned an unexpected value or the SDK's runner hit a fallback path",
        )

    # Validate the first turn_action (we only canned one in the script).
    ok, detail = _classify_turn_action(turn_actions[0])
    if not ok:
        return ConformanceCheck("fail", name, detail)

    return ConformanceCheck(
        "pass",
        name,
        f"completed handshake + 1 hand + match_end; {detail}",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_conformance_checks(bot: ChipzenBot, *, timeout_s: float = 10.0) -> list[ConformanceCheck]:
    """Run every conformance scenario against ``bot`` and return per-check results.

    Args:
        bot: An instance of the user's ``Bot`` subclass — same instance the
            user would call ``.run()`` on in production.
        timeout_s: Per-scenario timeout. Default 10 seconds, which is well
            above the platform's per-action 5-second budget but still bounded
            for CI use.

    Returns:
        A list of ``ConformanceCheck`` results, one per scenario. Empty
        list means no scenarios were configured (should not happen in
        normal operation).
    """
    scenarios = [_run_full_match_scenario]
    return [asyncio.run(scenario(bot, timeout_s)) for scenario in scenarios]
