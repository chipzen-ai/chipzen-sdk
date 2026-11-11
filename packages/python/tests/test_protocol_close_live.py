"""A terminal 4xxx close on a match socket (4013 first) ends the match, no reconnect.

Chipzen #7008: the external-API gateway now passes the executor's 4013
version refusal through to the bot instead of masking it as ``match_end`` +
1000. Before this change the SDK read that close as a drop and reconnected
until the retry budget ran out. These tests run a real local websockets
server so the real client raises its real close exception, the way it does in
production. They mirror the JavaScript and Rust tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from chipzen import (
    TERMINAL_CLOSE_CODES,
    Action,
    Bot,
    ProtocolCloseError,
    RetryPolicy,
    run_bot,
    run_external_bot,
)
from chipzen.close_codes import protocol_close_from
from chipzen.external import BOT_TOKEN_SUBPROTOCOL

NO_MUTUAL = "No mutually supported protocol version"
NOT_RECONNECTING = "; not reconnecting"


class _FoldBot(Bot):
    def decide(self, state):
        return Action.fold()


def _close_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "ERROR" and r.getMessage().endswith(NOT_RECONNECTING)
    ]


def _policy(attempts: int = 2) -> RetryPolicy:
    return RetryPolicy(max_reconnect_attempts=attempts, initial_backoff_ms=1, max_backoff_ms=1)


def test_terminal_codes_are_pinned():
    # Widening this set changes what a deployed bot does on that close; the
    # gateway's pass-through list may only include codes in here.
    assert TERMINAL_CLOSE_CODES == frozenset({4003, 4005, 4006, 4010, 4012, 4013})


def test_error_carries_code_and_reason():
    err = ProtocolCloseError(4013, NO_MUTUAL)
    assert (err.code, err.reason) == (4013, NO_MUTUAL)
    assert str(err) == f"server closed the match socket with 4013 protocol_mismatch: {NO_MUTUAL}"
    assert "no reason given" in str(ProtocolCloseError(4010))


def test_non_close_exceptions_are_not_terminal():
    assert protocol_close_from(RuntimeError("boom")) is None
    assert protocol_close_from(OSError("reset")) is None


async def _serve_match(handler, attempts: int = 2):
    """Run ``run_bot`` against ``handler``; return (result or exception, connects)."""
    connects = [0]

    async def counted(ws):
        connects[0] += 1
        await handler(ws)

    async with serve(counted, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/ws/match/m-live/bot"
        try:
            outcome = await run_bot(url, _FoldBot(), retry_policy=_policy(attempts), token="t")
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            outcome = exc
    return outcome, connects[0]


@pytest.mark.asyncio
async def test_4013_before_hello_is_not_retried(caplog):
    caplog.set_level("INFO")

    async def handler(ws):
        await ws.recv()  # authenticate
        await ws.close(4013, NO_MUTUAL)

    outcome, connects = await _serve_match(handler)
    assert isinstance(outcome, ProtocolCloseError)
    assert (outcome.code, outcome.reason) == (4013, NO_MUTUAL)
    assert connects == 1
    assert _close_lines(caplog) == [
        f"match m-live: server closed the match socket with 4013 protocol_mismatch: "
        f"{NO_MUTUAL}{NOT_RECONNECTING}"
    ]


@pytest.mark.asyncio
async def test_4013_after_the_bot_hello_is_not_retried(caplog):
    caplog.set_level("INFO")

    async def handler(ws):
        await ws.recv()  # authenticate
        await ws.send(json.dumps({"type": "hello", "selected_version": "1.0"}))
        await ws.recv()  # client hello
        await ws.close(4013, "Missing or invalid supported_versions")

    outcome, connects = await _serve_match(handler)
    assert isinstance(outcome, ProtocolCloseError)
    assert outcome.reason == "Missing or invalid supported_versions"
    assert connects == 1
    assert len(_close_lines(caplog)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(TERMINAL_CLOSE_CODES))
async def test_every_terminal_code_stops(code):
    async def handler(ws):
        await ws.recv()
        await ws.close(code, "why")

    outcome, connects = await _serve_match(handler)
    assert isinstance(outcome, ProtocolCloseError)
    assert outcome.code == code
    assert connects == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1011, 4001, 4002, 4008, 4011])
async def test_other_error_closes_are_still_retried(caplog, code):
    async def handler(ws):
        await ws.recv()
        await ws.close(code, "x")

    outcome, connects = await _serve_match(handler, attempts=2)
    assert not isinstance(outcome, ProtocolCloseError)
    assert isinstance(outcome, Exception)  # budget spent, last error re-raised
    assert connects == 3  # 1 + 2 reconnects, as before
    assert _close_lines(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1000, 1001])
async def test_normal_close_is_unchanged(code):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "hello", "selected_version": "1.0"}))
        await ws.recv()
        await ws.close(code, "")

    outcome, connects = await _serve_match(handler)
    assert outcome is None
    assert connects == 1


def _select_token_subprotocol(connection, subprotocols):
    return BOT_TOKEN_SUBPROTOCOL if BOT_TOKEN_SUBPROTOCOL in subprotocols else None


@pytest.mark.asyncio
async def test_run_external_bot_records_the_close_and_does_not_reconnect(caplog):
    """lobby -> matched -> a gateway that passes the executor's 4013 through."""
    caplog.set_level("INFO")
    gateway_connects = [0]

    async def handler(ws):
        if ws.request.path.startswith("/ws/external/match/"):
            gateway_connects[0] += 1
            await ws.recv()  # authenticate
            await ws.close(4013, NO_MUTUAL)
            return
        await ws.recv()  # lobby authenticate
        await ws.send(json.dumps({"type": "hello", "endpoint": "lobby"}))
        await ws.send(
            json.dumps(
                {
                    "type": "matched",
                    "match_id": "m1",
                    "participant_id": "p1",
                    "gateway_ws_url": "/ws/external/match/m1/p1",
                    "rated": False,
                }
            )
        )
        await ws.wait_closed()

    async with serve(
        handler, "127.0.0.1", 0, select_subprotocol=_select_token_subprotocol
    ) as server:
        port = server.sockets[0].getsockname()[1]
        policy = RetryPolicy(max_reconnect_attempts=3, initial_backoff_ms=50, max_backoff_ms=50)
        results = await asyncio.wait_for(
            run_external_bot(
                _FoldBot(),
                url=f"ws://127.0.0.1:{port}/ws/external/bot/b1",
                token="cz_extbot_x",
                retry_policy=policy,
                max_matches=1,  # the close counts as the match finishing
            ),
            timeout=15,
        )

    assert gateway_connects[0] == 1
    assert results == [
        {"match_id": "m1", "end": None, "close_code": 4013, "close_reason": NO_MUTUAL}
    ]
    assert _close_lines(caplog) == [
        f"match m1: server closed the match socket with 4013 protocol_mismatch: "
        f"{NO_MUTUAL}{NOT_RECONNECTING}"
    ]
