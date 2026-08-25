"""The client's ``supported_games`` declaration (chipzen-ai/Chipzen#4245, epic #4200).

A bot may declare the games it can actually play; the declaration rides the
Layer 1 client ``hello`` as ``supported_games`` -- a list of ``game_type``
strings. The platform reads an **absent** declaration as "poker only", which is
what every deployed bot relies on, so the whole point of this module is to pin
two things at once:

* declaring something puts exactly that list on the wire, under exactly that
  key, on the client ``hello`` (nowhere else); and
* declaring nothing leaves the ``hello`` frame **byte-identical** to the one
  the SDK has always sent -- same keys, same order, same JSON text.

The second is the load-bearing one. The server's handshake validator rejects
any key outside its allowlist by closing the socket, and its
``declared_client_games`` treats ``None`` (key absent) and ``[]`` (key present,
empty) as different answers: absent means "assume poker", empty means "supports
nothing". A default that emitted ``"supported_games": []`` would therefore be a
silent break of every bot in the fleet the first time a variant table existed.

Wire contract: ``docs/protocol/LAYER2-COMMON.md`` section 2. Server side:
``chipzen.transport.handshake.ALLOWED_HANDSHAKE_FIELDS`` and
``chipzen.services.extapi_game_capability.declared_client_games``.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen import external
from chipzen.bot import ChipzenBot
from chipzen.client import _run_session
from chipzen.external import run_external_bot
from chipzen.models import Action, GameState

MATCH_ID = "m_test_abc123"

#: The platform's handshake field allowlist, copied from
#: ``src/chipzen/transport/handshake.py`` (``ALLOWED_HANDSHAKE_FIELDS``) on
#: ``chipzen-ai/Chipzen@dev``. A ``hello`` carrying any key outside this set is
#: closed with ``CLOSE_VERSION_MISMATCH`` before the bot is seated, so the SDK
#: may only ever add keys that appear here.
SERVER_ALLOWED_HANDSHAKE_FIELDS = frozenset(
    {
        "type",
        "match_id",
        "ticket",
        "token",
        "supported_versions",
        "client_name",
        "client_version",
        "supported_games",
    }
)

#: The exact client ``hello`` text the SDK has always sent for these handshake
#: arguments. Frozen as a literal string rather than a dict so a change in key
#: ORDER (which json.dumps preserves) fails this test too.
FROZEN_HELLO_JSON = (
    '{"type": "hello", "match_id": "m_test_abc123", '
    '"supported_versions": ["1.0"], '
    '"client_name": "chipzen-sdk-test", "client_version": "0.2.0"}'
)


class _NoopBot(ChipzenBot):
    def decide(self, state: GameState) -> Action:  # pragma: no cover - never reached
        return Action.fold()


class _HandshakeOnlyWS:
    """Replays the server ``hello`` then ends the session immediately.

    Enough to drive the full Layer 1 handshake and capture every frame the
    client sent, without scripting a match.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return json.dumps(
            {
                "type": "hello",
                "match_id": MATCH_ID,
                "seq": 1,
                "supported_versions": ["1.0"],
                "selected_version": "1.0",
                "game_type": "nlhe_6max",
                "capabilities": [],
            }
        )

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration


async def _handshake(**kwargs) -> list[str]:
    """Run one handshake and return the raw frames the client sent."""
    ws = _HandshakeOnlyWS()
    await _run_session(
        ws,
        _NoopBot(),
        match_id=MATCH_ID,
        token="test_token",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.2.0",
        **kwargs,
    )
    return ws.sent


# ---------------------------------------------------------------------------
# Default: nothing declared -> nothing on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_hello_is_byte_identical_to_the_frozen_frame():
    """No declaration -> the client ``hello`` text is exactly what shipped before.

    This is the back-compat freeze. ``run_bot``/``_run_session`` gained a
    ``supported_games`` parameter; a caller that does not pass it must produce
    the same bytes as the SDK did before the parameter existed.
    """
    sent = await _handshake()
    hello = sent[1]
    assert hello == FROZEN_HELLO_JSON
    assert "supported_games" not in hello


@pytest.mark.asyncio
async def test_default_hello_omits_the_key_entirely_not_an_empty_list():
    """Absent and empty are different answers server-side; the default is absent.

    ``declared_client_games`` returns ``None`` for a missing key ("assume
    poker") and ``[]`` for a present-but-empty one ("supports nothing", which
    is rejected at a variant table). The default must be the former.
    """
    parsed = json.loads((await _handshake())[1])
    assert "supported_games" not in parsed
    assert parsed.get("supported_games") is None


@pytest.mark.asyncio
async def test_explicit_none_is_the_same_as_omitting_the_argument():
    assert await _handshake(supported_games=None) == await _handshake()


# ---------------------------------------------------------------------------
# Declaring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declaration_rides_the_client_hello_with_the_right_shape():
    sent = await _handshake(supported_games=["poker", "draw27"])
    authenticate, hello = json.loads(sent[0]), json.loads(sent[1])

    assert hello["type"] == "hello"
    assert hello["supported_games"] == ["poker", "draw27"]
    # A list of plain strings -- the server keeps only ``str`` entries.
    assert all(isinstance(entry, str) for entry in hello["supported_games"])

    # The declaration belongs to the hello, not the authenticate frame: the
    # server reads it off the client hello (``bot_ws.py`` hands ``client_hello``
    # to ``assert_client_supports_game``).
    assert "supported_games" not in authenticate


@pytest.mark.asyncio
async def test_declaration_is_appended_last_leaving_the_prior_keys_untouched():
    """The existing five keys keep their exact values and order; the
    declaration is strictly additive."""
    hello = json.loads((await _handshake(supported_games=["draw27"]))[1])
    frozen = json.loads(FROZEN_HELLO_JSON)
    assert list(hello) == [*frozen, "supported_games"]
    assert {k: hello[k] for k in frozen} == frozen


@pytest.mark.asyncio
async def test_hello_keys_stay_inside_the_server_field_allowlist():
    """A key the server does not allow closes the socket before seating.

    Runs for both the declared and the undeclared frame.
    """
    for kwargs in ({}, {"supported_games": ["poker", "draw27", "ofc"]}):
        hello = json.loads((await _handshake(**kwargs))[1])
        assert set(hello) <= SERVER_ALLOWED_HANDSHAKE_FIELDS


@pytest.mark.asyncio
async def test_empty_list_is_sent_verbatim_rather_than_dropped():
    """``[]`` means "I support nothing" and is passed through honestly.

    Silently rewriting it to "absent" would tell the server "assume poker" --
    the opposite of what the caller said.
    """
    hello = json.loads((await _handshake(supported_games=[]))[1])
    assert hello["supported_games"] == []


@pytest.mark.asyncio
async def test_the_caller_list_is_copied_not_aliased():
    """A later mutation of the caller's list must not retro-change the frame."""
    declared = ["poker"]
    sent = await _handshake(supported_games=declared)
    declared.append("draw27")
    assert json.loads(sent[1])["supported_games"] == ["poker"]


# ---------------------------------------------------------------------------
# The external-API path threads the same declaration to every match
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


class _LobbyWS:
    def __init__(self, frames: list[dict]):
        self._frames = [json.dumps(f) for f in frames]

    async def send(self, data: str) -> None:
        pass

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)  # cancelled by the loop's wait_for timeout
        raise AssertionError("unreachable")


def _install_lobby(monkeypatch, seen: list) -> None:
    """Script the lobby leg and stub the match session.

    Records the ``supported_games`` each per-match session was started with.
    """
    lobby_ws = _LobbyWS(
        [
            {"type": "hello", "endpoint": "lobby"},
            {
                "type": "matched",
                "match_id": "m1",
                "participant_id": "p1",
                "gateway_ws_url": "/ws/external/match/m1/p1",
                "rated": False,
            },
        ]
    )

    def _connect(url, *, max_size=None, user_agent_header=None, subprotocols=None):
        return _Conn(lobby_ws)

    async def _fake_run_session(ws, bot, **kwargs):
        seen.append(kwargs.get("supported_games"))
        return {"reason": "complete"}

    monkeypatch.setattr(external.websockets, "connect", _connect)
    monkeypatch.setattr(external, "_run_session", _fake_run_session)
    monkeypatch.setattr(external, "_LOBBY_RECV_TIMEOUT_S", 0.02)


@pytest.mark.asyncio
async def test_external_path_forwards_the_declaration_to_each_match(monkeypatch):
    seen: list = []
    _install_lobby(monkeypatch, seen)
    await run_external_bot(
        _NoopBot(),
        url="wss://example.test/ws/external/bot/b1",
        token="cz_extbot_x",
        max_matches=1,
        supported_games=["poker", "draw27"],
    )
    assert seen == [["poker", "draw27"]]


@pytest.mark.asyncio
async def test_external_path_declares_nothing_by_default(monkeypatch):
    seen: list = []
    _install_lobby(monkeypatch, seen)
    await run_external_bot(
        _NoopBot(),
        url="wss://example.test/ws/external/bot/b1",
        token="cz_extbot_x",
        max_matches=1,
    )
    assert seen == [None]
