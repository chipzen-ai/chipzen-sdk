"""The lobby client ``hello`` (chipzen-ai/chipzen-sdk#279).

After the server's lobby ``hello``, ``run_external_bot`` sends one optional
client ``hello`` naming the client, its version and its lobby capabilities
(``docs/EXTERNAL-API-BOT-PROTOCOL.md`` section 3, step 4). It is additive:

* ``authenticate`` stays exactly ``{type, token}``, because the server checks
  that frame's fields strictly and closes with 4001 on one it does not know;
* it is sent once per lobby connection, including after a reconnect;
* a server that predates it drops every non-``pong`` lobby frame, and the bot
  still connects, stays connected and plays.

The first tests fake the transport; the old-server test runs a real local
``websockets`` server for both the lobby and the match gateway.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.frames import Close

import chipzen.external as external
from chipzen import Action, Bot, RetryPolicy, __version__, run_external_bot
from chipzen.external import BOT_TOKEN_SUBPROTOCOL

LOBBY_URL = "wss://staging.chipzen.ai/ws/external/bot/test-bot-uuid"
TOKEN = "cz_extbot_x"

_SERVER_HELLO = {"type": "hello", "endpoint": "lobby"}
_EVICT = {"type": "evict"}
_DROP = object()  # in a lobby script: the next recv raises (a lobby drop)

_FAST_POLICY = RetryPolicy(max_reconnect_attempts=3, initial_backoff_ms=1, max_backoff_ms=1)


class _FoldBot(Bot):
    def decide(self, state):
        return Action.check() if "check" in state.valid_actions else Action.fold()


def _expected_hello(client_name: str = "chipzen-sdk-python", client_version: str = __version__):
    return {
        "type": "hello",
        "client_name": client_name,
        "client_version": client_version,
        "capabilities": [],
    }


# ---------------------------------------------------------------------------
# Frame shape
# ---------------------------------------------------------------------------


def test_frame_shape_matches_the_protocol_doc():
    frame = external._lobby_client_hello("chipzen-sdk-python", "9.9.9")
    assert frame == {
        "type": "hello",
        "client_name": "chipzen-sdk-python",
        "client_version": "9.9.9",
        "capabilities": [],
    }
    # Serializes to plain JSON (a list, not the module's tuple).
    assert json.loads(json.dumps(frame)) == frame


def test_no_capability_is_declared_until_the_sdk_can_handle_it():
    # ``inbound_challenges`` waits for the ``on_challenge`` hook (#280):
    # declaring it would let the server push frames this SDK cannot answer.
    assert external._LOBBY_CAPABILITIES == ()
    assert "inbound_challenges" not in external._lobby_client_hello("n", "v")["capabilities"]


def test_each_frame_gets_its_own_capabilities_list():
    first = external._lobby_client_hello("n", "v")
    first["capabilities"].append("mutated")
    assert external._lobby_client_hello("n", "v")["capabilities"] == []


# ---------------------------------------------------------------------------
# When it is sent (faked transport)
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *args):
        return False


class _LobbyWS:
    """Replays one scripted lobby connection and records what the bot sent.

    ``_DROP`` makes the next ``recv`` raise like a dropped socket. Once the
    script is used up, ``recv`` blocks until the lobby loop's timeout.
    """

    def __init__(self, frames: list):
        self.sent: list[dict] = []
        self._frames = list(frames)

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if self._frames:
            frame = self._frames.pop(0)
            if frame is _DROP:
                raise ConnectionError("simulated lobby drop")
            return json.dumps(frame)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _install_lobbies(monkeypatch, *scripts: list) -> list[_LobbyWS]:
    """One ``_LobbyWS`` per lobby connect, in order. Backoff sleeps are instant."""
    lobbies = [_LobbyWS(s) for s in scripts]
    pending = iter(lobbies)

    def _connect(url, *, max_size=None, user_agent_header=None, subprotocols=None):
        assert "/ws/external/bot/" in url, "these tests never reach a gateway"
        return _Conn(next(pending))

    async def _instant_sleep(delay):
        return None

    monkeypatch.setattr(external.websockets, "connect", _connect)
    monkeypatch.setattr(external, "_LOBBY_RECV_TIMEOUT_S", 0.02)
    monkeypatch.setattr(external.asyncio, "sleep", _instant_sleep)
    return lobbies


def _hellos(lobby: _LobbyWS) -> list[dict]:
    return [m for m in lobby.sent if m.get("type") == "hello"]


async def test_sent_once_right_after_the_server_hello(monkeypatch):
    (lobby,) = _install_lobbies(monkeypatch, [_SERVER_HELLO, _EVICT])
    await run_external_bot(_FoldBot(), url=LOBBY_URL, token=TOKEN)
    assert lobby.sent == [{"type": "authenticate", "token": TOKEN}, _expected_hello()]


async def test_authenticate_carries_no_identity_or_capability_field(monkeypatch):
    (lobby,) = _install_lobbies(monkeypatch, [_SERVER_HELLO, _EVICT])
    await run_external_bot(
        _FoldBot(), url=LOBBY_URL, token=TOKEN, client_name="my-bot", client_version="1.2.3"
    )
    auth = lobby.sent[0]
    assert auth["type"] == "authenticate"
    assert set(auth) == {"type", "token"}


async def test_not_sent_before_the_server_hello(monkeypatch):
    # A ping that arrives first is answered; the hello waits for the server's.
    (lobby,) = _install_lobbies(monkeypatch, [{"type": "ping"}, _SERVER_HELLO, _EVICT])
    await run_external_bot(_FoldBot(), url=LOBBY_URL, token=TOKEN)
    assert [m["type"] for m in lobby.sent] == ["authenticate", "pong", "hello"]


async def test_not_sent_when_the_server_never_says_hello(monkeypatch):
    (lobby,) = _install_lobbies(monkeypatch, [{"type": "ping"}, _EVICT])
    await run_external_bot(_FoldBot(), url=LOBBY_URL, token=TOKEN)
    assert _hellos(lobby) == []


async def test_a_repeated_server_hello_does_not_send_a_second_one(monkeypatch):
    (lobby,) = _install_lobbies(
        monkeypatch, [_SERVER_HELLO, {"type": "ping"}, _SERVER_HELLO, _EVICT]
    )
    await run_external_bot(_FoldBot(), url=LOBBY_URL, token=TOKEN)
    assert _hellos(lobby) == [_expected_hello()]


async def test_sent_again_on_each_reconnect(monkeypatch):
    lobbies = _install_lobbies(
        monkeypatch,
        [_SERVER_HELLO, _DROP],
        [_SERVER_HELLO, _DROP],
        [_SERVER_HELLO, _EVICT],
    )
    await run_external_bot(_FoldBot(), url=LOBBY_URL, token=TOKEN, retry_policy=_FAST_POLICY)
    assert [_hellos(lobby) for lobby in lobbies] == [[_expected_hello()]] * 3


@pytest.mark.parametrize(
    "exc", [ConnectionClosedOK(Close(1000, ""), None), ConnectionResetError("reset")]
)
async def test_a_socket_closed_after_the_server_hello_still_ends_as_before(monkeypatch, exc):
    """The server closed the lobby right after its ``hello`` and ``evict``.

    Before the client hello existed, this session read hello, then evict, and
    stopped. ``authenticate`` went out before the close; the hello send then
    raises, and that must not turn the eviction into a reconnect.
    """
    lobby = _LobbyWS([_SERVER_HELLO, _EVICT])
    lobby.send_attempts = 0  # type: ignore[attr-defined]
    connects = [0]

    async def _send(data: str) -> None:
        lobby.send_attempts += 1  # type: ignore[attr-defined]
        if lobby.send_attempts > 1:  # type: ignore[attr-defined]
            raise exc
        lobby.sent.append(json.loads(data))

    lobby.send = _send  # type: ignore[method-assign]

    def _connect(url, *, max_size=None, user_agent_header=None, subprotocols=None):
        connects[0] += 1
        return _Conn(lobby)

    monkeypatch.setattr(external.websockets, "connect", _connect)
    monkeypatch.setattr(external, "_LOBBY_RECV_TIMEOUT_S", 0.02)
    results = await run_external_bot(_FoldBot(), url=LOBBY_URL, token=TOKEN)
    assert results == []
    assert connects[0] == 1  # evicted, not reconnected
    assert lobby.send_attempts == 2  # authenticate, then the attempted hello
    assert lobby.sent == [{"type": "authenticate", "token": TOKEN}]


async def test_carries_the_callers_client_name_and_version(monkeypatch):
    # chipzen-mcp passes client_name="chipzen-mcp"; the lobby hello names it too.
    (lobby,) = _install_lobbies(monkeypatch, [_SERVER_HELLO, _EVICT])
    await run_external_bot(
        _FoldBot(), url=LOBBY_URL, token=TOKEN, client_name="chipzen-mcp", client_version="0.4.0"
    )
    assert _hellos(lobby) == [_expected_hello("chipzen-mcp", "0.4.0")]


# ---------------------------------------------------------------------------
# A server that predates the client hello (real sockets)
# ---------------------------------------------------------------------------


def _select_token_subprotocol(connection, subprotocols):
    """Echo the token sentinel on the gateway leg; the lobby offers none."""
    return BOT_TOKEN_SUBPROTOCOL if BOT_TOKEN_SUBPROTOCOL in subprotocols else None


async def _play_gateway(ws, actions: list[dict]) -> None:
    """A minimal match on the gateway: one turn, then ``match_end``."""
    await ws.recv()  # authenticate
    await ws.send(
        json.dumps({"type": "hello", "selected_version": "1.0", "game_type": "nlhe_6max"})
    )
    await ws.send(
        json.dumps(
            {
                "type": "match_start",
                "match_id": "m1",
                "seats": [{"seat": 0, "is_self": True}, {"seat": 1, "is_self": False}],
                "turn_timeout_ms": 5000,
            }
        )
    )
    await ws.send(
        json.dumps(
            {
                "type": "turn_request",
                "match_id": "m1",
                "request_id": "req_1",
                "valid_actions": ["fold", "call", "raise"],
                "state": {
                    "phase": "preflop",
                    "board": [],
                    "your_hole_cards": ["Ah", "Kd"],
                    "pot": 15,
                    "to_call": 5,
                    "min_raise": 20,
                    "max_raise": 995,
                },
            }
        )
    )
    while True:  # the bot's match hello, then its action
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if frame.get("type") == "turn_action":
            actions.append(frame)
            break
    await ws.send(json.dumps({"type": "match_end", "match_id": "m1", "reason": "complete"}))
    await ws.wait_closed()


async def test_a_server_that_drops_the_hello_still_plays():
    """The lobby before Chipzen #6678 reads only ``pong`` and drops every
    other frame. The bot must answer its heartbeats on the one connection,
    take its ``matched`` and play the match, exactly as before."""
    lobby_connects = [0]
    lobby_auth: list[dict] = []
    pongs = [0]
    dropped: list[dict] = []
    actions: list[dict] = []

    async def handler(ws):
        if ws.request.path.startswith("/ws/external/match/"):
            await _play_gateway(ws, actions)
            return
        lobby_connects[0] += 1
        lobby_auth.append(json.loads(await ws.recv()))
        await ws.send(json.dumps(_SERVER_HELLO))
        for _ in range(2):  # two heartbeat rounds, each read like the old server
            await ws.send(json.dumps({"type": "ping"}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if frame.get("type") == "pong":
                    pongs[0] += 1
                    break
                dropped.append(frame)  # the old lobby ignores anything else
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
        try:
            async for raw in ws:  # until the bot closes after its one match
                dropped.append(json.loads(raw))
        except ConnectionClosed:
            pass

    async with serve(
        handler, "127.0.0.1", 0, select_subprotocol=_select_token_subprotocol
    ) as server:
        port = server.sockets[0].getsockname()[1]
        results = await asyncio.wait_for(
            run_external_bot(
                _FoldBot(),
                url=f"ws://127.0.0.1:{port}/ws/external/bot/b1",
                token=TOKEN,
                retry_policy=_FAST_POLICY,
                max_matches=1,
            ),
            timeout=15,
        )

    assert lobby_connects[0] == 1  # stayed connected: no reconnect
    assert lobby_auth == [{"type": "authenticate", "token": TOKEN}]
    assert pongs[0] == 2
    assert dropped == [_expected_hello()]  # sent once, ignored, harmless
    assert len(actions) == 1
    assert [r["end"]["reason"] for r in results] == ["complete"]
