"""``run_bot``'s retry budget against a real local websockets server (#244).

The counter used to reset on every successful connect, so a server that
accepted the socket and then closed it (1011, every time) made ``run_bot``
reconnect forever. It now resets only once a session made progress: the server
``hello`` plus a game frame.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from chipzen import Action, Bot, RetryPolicy, run_bot

#: Fail rather than hang if the budget is not enforced.
_GUARD_S = 10

_HELLO = {"type": "hello", "selected_version": "1.0", "game_type": "nlhe_6max"}
_MATCH_START = {
    "type": "match_start",
    "match_id": "m-live",
    "seats": [{"seat": 0, "is_self": True}, {"seat": 1, "is_self": False}],
}
_MATCH_END = {"type": "match_end", "match_id": "m-live", "reason": "complete"}


class _FoldBot(Bot):
    def decide(self, state):
        return Action.fold()


def _policy(attempts: int) -> RetryPolicy:
    return RetryPolicy(max_reconnect_attempts=attempts, initial_backoff_ms=1, max_backoff_ms=1)


async def _serve(scripts):
    """Serve connection ``i`` with ``scripts[i]`` (the last one repeats): send
    its frames after the client's authenticate, then close 1011 unless the
    script ends the match."""
    seen = [0]

    async def handler(ws):
        script = scripts[min(seen[0], len(scripts) - 1)]
        seen[0] += 1
        await ws.recv()  # authenticate
        for frame in script:
            await ws.send(json.dumps(frame))
            if frame["type"] == "hello" and len(script) > 1:
                await ws.recv()  # the client hello, before any game frame
        if not script or script[-1]["type"] != "match_end":
            await ws.close(1011, "internal error")
        else:
            await ws.wait_closed()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/ws/match/m-live/bot", seen


@pytest.mark.asyncio
async def test_a_server_that_accepts_then_closes_is_retried_only_the_configured_times():
    server, url, seen = await _serve([[]])  # accept, then 1011, every time
    try:
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(
                run_bot(url, _FoldBot(), retry_policy=_policy(2), token="t"), _GUARD_S
            )
    finally:
        server.close()
        await server.wait_closed()
    assert seen[0] == 3  # the first connect + 2 retries


@pytest.mark.asyncio
async def test_a_hello_alone_is_not_progress():
    server, url, seen = await _serve([[_HELLO]])  # hello, then 1011, every time
    try:
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(
                run_bot(url, _FoldBot(), retry_policy=_policy(2), token="t"), _GUARD_S
            )
    finally:
        server.close()
        await server.wait_closed()
    assert seen[0] == 3


@pytest.mark.asyncio
async def test_a_session_that_made_progress_gets_a_fresh_budget():
    # Two failed connects use the whole budget of 2. The third gets going
    # (hello + match_start) and then drops: that drop starts a fresh budget,
    # so one more failure and then a full match still fit. A budget that never
    # reset would give up on the third connect.
    server, url, seen = await _serve([[], [], [_HELLO, _MATCH_START], [], [_HELLO, _MATCH_END]])
    try:
        end = await asyncio.wait_for(
            run_bot(url, _FoldBot(), retry_policy=_policy(2), token="t"), _GUARD_S
        )
    finally:
        server.close()
        await server.wait_closed()
    assert end is not None and end["reason"] == "complete"
    assert seen[0] == 5
