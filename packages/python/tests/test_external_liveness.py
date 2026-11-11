"""Lobby liveness: a lobby that stops sending frames is reconnected (#136).

The server pings the lobby every 15s. When the host sleeps, the server times
the bot out and closes the lobby, but the local socket can stay "open" for a
long time after the host wakes, so the SDK never noticed and never reconnected.
``run_external_bot`` now treats total silence for ``lobby_silence_timeout``
seconds as a dead lobby and reconnects with the retry policy's backoff.

The silent-lobby test uses a raw TCP server that sends ``hello`` and then never
answers anything (not even a close frame) while keeping the TCP connection up.
The other socket tests use a real local ``websockets`` server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time

import pytest
from websockets.asyncio.server import serve

import chipzen.external as external
from chipzen import Action, Bot, RetryPolicy, run_external_bot

SILENCE_LINE = "nothing from the server for"

_FAST_POLICY = RetryPolicy(max_reconnect_attempts=3, initial_backoff_ms=10, max_backoff_ms=10)


class _FoldBot(Bot):
    def decide(self, state):
        return Action.fold()


def _silence_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if SILENCE_LINE in r.getMessage()]


@pytest.fixture
def fast_lobby_poll(monkeypatch):
    """Wake the lobby loop often so a short silence timeout is noticed quickly."""
    monkeypatch.setattr(external, "_LOBBY_RECV_TIMEOUT_S", 0.05)


# ---------------------------------------------------------------------------
# Real socket: a lobby that goes quiet is torn down and reconnected
# ---------------------------------------------------------------------------


#: RFC 6455 handshake GUID, for the hand-rolled server below.
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_text_frame(msg: dict) -> bytes:
    """One unmasked server-to-client text frame (payload under 126 bytes)."""
    payload = json.dumps(msg).encode()
    assert len(payload) < 126
    return bytes([0x81, len(payload)]) + payload


async def _accept_upgrade(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Complete the WebSocket opening handshake by hand."""
    request = await reader.readuntil(b"\r\n\r\n")
    key = next(
        line.split(b":", 1)[1].strip()
        for line in request.split(b"\r\n")
        if line.lower().startswith(b"sec-websocket-key:")
    )
    accept = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest())
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )


async def test_silent_lobby_is_reconnected(caplog, fast_lobby_poll):
    """A lobby that goes dead after its ``hello`` is dropped and reconnected.

    The first connection is served by a raw TCP server that completes the
    upgrade, sends ``hello`` and then never answers anything again: no pings,
    no pong, and no reply to a close frame, as a socket the server already
    closed while the host slept. A normal close would wait out the client's
    10s ``close_timeout``; the SDK aborts the socket instead.
    """
    caplog.set_level("INFO")
    connects = [0]
    first_dropped = asyncio.Event()

    async def handle(reader, writer):
        connects[0] += 1
        await _accept_upgrade(reader, writer)
        await reader.read(4096)  # the client's authenticate frame
        writer.write(_ws_text_frame({"type": "hello", "endpoint": "lobby"}))
        if connects[0] > 1:
            # The reconnect: end the session cleanly. Evict, a 1000 close,
            # wait for the client's close frame, then close TCP (server first).
            writer.write(_ws_text_frame({"type": "evict"}))
            writer.write(b"\x88\x02\x03\xe8")
            await writer.drain()
            await reader.read(4096)
            writer.close()
            return
        await writer.drain()
        # Dead from here: read and ignore everything until the client drops the
        # TCP connection.
        while await reader.read(4096):
            pass
        first_dropped.set()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        results = await asyncio.wait_for(
            run_external_bot(
                _FoldBot(),
                url=f"ws://127.0.0.1:{port}/ws/external/bot/b1",
                token="cz_extbot_x",
                retry_policy=_FAST_POLICY,
                lobby_silence_timeout=0.4,
            ),
            timeout=20,
        )
        elapsed = time.monotonic() - started
        await asyncio.wait_for(first_dropped.wait(), timeout=5)

    assert connects[0] == 2  # the silent lobby was dropped and reconnected
    assert results == []
    assert len(_silence_lines(caplog)) == 1
    # Aborted, not closed with a handshake the dead peer never answers, so the
    # reconnect does not wait out close_timeout (10s).
    assert elapsed < 5


async def test_pinging_lobby_is_not_reconnected(caplog, fast_lobby_poll):
    """A lobby that keeps pinging stays up well past the silence timeout."""
    caplog.set_level("INFO")
    connects = [0]
    pongs = [0]

    async def handler(ws):
        connects[0] += 1
        await ws.recv()  # authenticate
        await ws.send(json.dumps({"type": "hello", "endpoint": "lobby"}))
        for _ in range(8):  # 8 x 0.1s = 4x the 0.2s silence timeout
            await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "ping"}))
            if json.loads(await ws.recv()) == {"type": "pong"}:
                pongs[0] += 1
        await ws.send(json.dumps({"type": "evict"}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(
            run_external_bot(
                _FoldBot(),
                url=f"ws://127.0.0.1:{port}/ws/external/bot/b1",
                token="cz_extbot_x",
                retry_policy=_FAST_POLICY,
                lobby_silence_timeout=0.2,
            ),
            timeout=15,
        )

    assert connects[0] == 1
    assert pongs[0] == 8
    assert _silence_lines(caplog) == []


async def test_silence_check_can_be_turned_off(caplog, fast_lobby_poll):
    """``lobby_silence_timeout=None`` keeps the old behavior: a quiet lobby is
    left alone (the server ends this one with an evict after a quiet spell)."""
    caplog.set_level("INFO")
    connects = [0]

    async def handler(ws):
        connects[0] += 1
        await ws.recv()  # authenticate
        await ws.send(json.dumps({"type": "hello", "endpoint": "lobby"}))
        await asyncio.sleep(0.6)
        await ws.send(json.dumps({"type": "evict"}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(
            run_external_bot(
                _FoldBot(),
                url=f"ws://127.0.0.1:{port}/ws/external/bot/b1",
                token="cz_extbot_x",
                retry_policy=_FAST_POLICY,
                lobby_silence_timeout=None,
            ),
            timeout=15,
        )

    assert connects[0] == 1
    assert _silence_lines(caplog) == []


@pytest.mark.parametrize("bad", [0, -1, 0.0])
async def test_non_positive_silence_timeout_is_rejected(bad):
    with pytest.raises(ValueError, match="lobby_silence_timeout"):
        await run_external_bot(
            _FoldBot(),
            url="ws://127.0.0.1:1/ws/external/bot/b1",
            token="cz_extbot_x",
            lobby_silence_timeout=bad,
        )


def test_default_is_three_missed_heartbeats():
    # The server pings every 15s; the default must tolerate two missed pings.
    assert external.DEFAULT_LOBBY_SILENCE_TIMEOUT_S == 45.0


# ---------------------------------------------------------------------------
# _LobbyLiveness: host sleep and clock steps
# ---------------------------------------------------------------------------


class _Clocks:
    """Stand-ins for ``time.monotonic`` and ``time.time``."""

    def __init__(self, monkeypatch):
        self.mono = 1000.0
        self.wall = 1_700_000_000.0
        monkeypatch.setattr(external.time, "monotonic", lambda: self.mono)
        monkeypatch.setattr(external.time, "time", lambda: self.wall)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def test_liveness_expires_after_the_timeout(monkeypatch):
    clocks = _Clocks(monkeypatch)
    live = external._LobbyLiveness(45.0)
    clocks.advance(44.0)
    assert not live.expired()
    clocks.advance(1.0)
    assert live.expired()


def test_a_frame_resets_the_silence(monkeypatch):
    clocks = _Clocks(monkeypatch)
    live = external._LobbyLiveness(45.0)
    clocks.advance(40.0)
    live.mark()
    clocks.advance(40.0)
    assert not live.expired()
    assert live.silent_for() == pytest.approx(40.0)


def test_host_sleep_is_seen_through_the_wall_clock(monkeypatch):
    """macOS and Linux stop the monotonic clock while asleep; the wall clock
    keeps going, so the first check after waking sees the whole gap."""
    clocks = _Clocks(monkeypatch)
    live = external._LobbyLiveness(45.0)
    clocks.wall += 45 * 60  # 45 minutes asleep: monotonic did not move
    clocks.advance(2.0)  # the first poll after waking
    assert live.expired()
    assert live.silent_for() == pytest.approx(45 * 60 + 2.0)


def test_wall_clock_stepped_back_still_expires_on_monotonic(monkeypatch):
    clocks = _Clocks(monkeypatch)
    live = external._LobbyLiveness(45.0)
    clocks.wall -= 3600  # NTP steps the wall clock back an hour
    clocks.advance(45.0)
    assert live.expired()


def test_liveness_off_never_expires(monkeypatch):
    clocks = _Clocks(monkeypatch)
    live = external._LobbyLiveness(None)
    clocks.advance(10_000.0)
    assert not live.expired()


def test_abort_socket_is_best_effort():
    class _Transport:
        aborted = False

        def abort(self):
            self.aborted = True

    class _WS:
        transport = _Transport()

    ws = _WS()
    external._abort_socket(ws)
    assert ws.transport.aborted
    external._abort_socket(object())  # no transport: nothing to do, no error
