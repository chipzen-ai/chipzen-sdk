"""A match seat taken over by another connection of the same bot (#242),
against a real local websockets server.

The scripted tests in ``test_external.py`` fake the transport; these let the
real ``websockets`` client raise its real close exception, so the 4000 is
detected the way it is in production. They mirror the JavaScript and Rust
real-socket tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from chipzen import Action, Bot, RetryPolicy, run_bot, run_external_bot
from chipzen.external import BOT_TOKEN_SUBPROTOCOL

SEAT_LINE = "seat taken over by another connection of this bot; not reconnecting"


class _FoldBot(Bot):
    def decide(self, state):
        return Action.fold()


def _seat_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if SEAT_LINE in r.getMessage()]


@pytest.mark.asyncio
async def test_run_bot_does_not_retry_a_real_4000_close(caplog):
    caplog.set_level("INFO")
    seen = [0]

    async def handler(ws):
        seen[0] += 1
        await ws.recv()  # authenticate
        await ws.close(4000, "replaced_by_new_connection")

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        policy = RetryPolicy(max_reconnect_attempts=2, initial_backoff_ms=1, max_backoff_ms=1)
        url = f"ws://127.0.0.1:{port}/ws/match/m-live/bot"
        assert await run_bot(url, _FoldBot(), retry_policy=policy, token="t") is None

    assert seen[0] == 1
    assert _seat_lines(caplog) == [f"match m-live: {SEAT_LINE}"]


def _select_token_subprotocol(connection, subprotocols):
    """Echo the token sentinel on the gateway leg; the lobby offers none."""
    return BOT_TOKEN_SUBPROTOCOL if BOT_TOKEN_SUBPROTOCOL in subprotocols else None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [4000, 1011])
async def test_run_external_bot_on_a_real_socket(caplog, code):
    """The full external path: lobby -> matched -> a gateway that closes ``code``.

    4000 (after the error frame): one gateway connection, no result. The lobby
    evicts only after a pause longer than the backoff, so a reconnect would
    show up as a second gateway connection. 1011: a drop, reconnected until the
    budget is spent (1 + 3 connections), then the match is abandoned.
    """
    caplog.set_level("INFO")
    gateway_connects = [0]
    gateway_closed = asyncio.Event()

    async def handler(ws):
        if ws.request.path.startswith("/ws/external/match/"):
            gateway_connects[0] += 1
            await ws.recv()  # authenticate
            if code == 4000:
                await ws.send(json.dumps({"type": "error", "code": "replaced_by_new_connection"}))
            await ws.close(code, "replaced_by_new_connection" if code == 4000 else "")
            gateway_closed.set()
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
        if code == 4000:
            await gateway_closed.wait()
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"type": "evict"}))
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
                max_matches=1,
            ),
            timeout=15,
        )

    if code == 4000:
        assert gateway_connects[0] == 1  # no reconnect
        assert results == []  # not a finished match, no bogus result entry
        assert _seat_lines(caplog) == [f"match m1: {SEAT_LINE}"]
    else:
        assert gateway_connects[0] == 4  # reconnected until the budget was spent
        assert [r["end"] for r in results] == [None]
        assert _seat_lines(caplog) == []
