"""Tests for ``chipzen.external`` — the external-API remote-play entry point.

Covers the lobby -> matched -> gateway flow promoted from the reference client
(chipzen-ai/chipzen-sdk#42-46), the connection-resolution precedence, and the
safe_mode / max_matches knobs.

The websocket layer is faked: ``chipzen.external.websockets.connect`` is
monkeypatched to route by URL (``/ws/external/bot/`` -> lobby, otherwise ->
gateway) and replay scripted frames, so the whole path runs without a server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen.external as external
from chipzen import Action, Bot, ChipzenConfig, RetryPolicy, run_external_bot
from chipzen.client import BotDecisionError

LOBBY_URL = "wss://staging.chipzen.ai/ws/external/bot/test-bot-uuid"


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------


class _CollectBot(Bot):
    """Records lifecycle hooks + decision latencies; checks when it can."""

    def __init__(self):
        self.events: list[str] = []
        self.latencies: list[float] = []

    def decide(self, state):
        self.events.append("decide")
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()

    def on_match_start(self, match_info):
        self.events.append("match_start")

    def on_match_end(self, results):
        self.events.append("match_end")

    def on_decision_latency(self, latency_ms):
        self.latencies.append(latency_ms)


class _CrashBot(Bot):
    def decide(self, state):
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Fake websocket transport
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *args):
        return False


class _LobbyWS:
    """Replays scripted lobby frames; blocks once exhausted so the lobby
    loop falls through to its stop-check on the next ``wait_for`` timeout."""

    def __init__(self, frames: list[dict]):
        self.sent: list[dict] = []
        self._frames = [json.dumps(f) for f in frames]

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)  # cancelled by the loop's wait_for timeout
        raise AssertionError("unreachable")


class _GatewayWS:
    """Replays a scripted match: first ``recv`` is the server hello, then the
    rest of the frames are yielded by async iteration (matching _run_session)."""

    def __init__(self, frames: list[dict]):
        self.sent: list[dict] = []
        self._hello = json.dumps(frames[0])
        self._rest = [json.dumps(f) for f in frames[1:]]
        self._idx = 0

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return self._hello

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._idx < len(self._rest):
            frame = self._rest[self._idx]
            self._idx += 1
            return frame
        raise StopAsyncIteration


def _server_hello() -> dict:
    return {"type": "hello", "selected_version": "1.0", "game_type": "nlhe_6max"}


def _match_start() -> dict:
    return {
        "type": "match_start",
        "match_id": "m1",
        "seats": [{"seat": 0, "is_self": True}, {"seat": 1, "is_self": False}],
        "turn_timeout_ms": 5000,
    }


def _turn_request() -> dict:
    return {
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


def _match_end(reason: str = "complete") -> dict:
    return {"type": "match_end", "match_id": "m1", "reason": reason}


def _full_match() -> list[dict]:
    return [_server_hello(), _match_start(), _turn_request(), _match_end()]


def _install_transport(monkeypatch, lobby_frames, gateway_frames=None):
    """Patch ``websockets.connect`` to route lobby vs gateway by URL.

    Returns a ``calls`` dict recording lobby/gateway URLs, the UA, the
    gateway subprotocols, and the shared lobby WS (so tests can inspect what
    the bot sent on the lobby leg).
    """
    lobby_ws = _LobbyWS(lobby_frames)
    calls: dict = {"lobby": [], "gateway": [], "subprotocols": [], "ua": [], "lobby_ws": lobby_ws}

    def _connect(url, *, max_size=None, user_agent_header=None, subprotocols=None):
        calls["ua"].append(user_agent_header)
        if "/ws/external/match/" in url:
            calls["gateway"].append(url)
            calls["subprotocols"].append(subprotocols)
            return _Conn(_GatewayWS(list(gateway_frames or _full_match())))
        calls["lobby"].append(url)
        return _Conn(lobby_ws)

    monkeypatch.setattr(external.websockets, "connect", _connect)
    # Keep the lobby loop's idle re-check fast so tests don't wait 2s.
    monkeypatch.setattr(external, "_LOBBY_RECV_TIMEOUT_S", 0.02)
    return calls


def _matched(gateway_path: str = "/ws/external/match/m1/p1") -> dict:
    return {
        "type": "matched",
        "match_id": "m1",
        "participant_id": "p1",
        "gateway_ws_url": gateway_path,
        "rated": False,
    }


# ---------------------------------------------------------------------------
# URL helpers (pure)
# ---------------------------------------------------------------------------


def test_bot_token_subprotocols():
    assert external.bot_token_subprotocols("cz_extbot_x") == [
        external.BOT_TOKEN_SUBPROTOCOL,
        "cz_extbot_x",
    ]


def test_resolve_gateway_url_joins_path_to_lobby_origin():
    out = external.resolve_gateway_url(
        "wss://staging.chipzen.ai/ws/external/bot/abc", "/ws/external/match/m1/p1"
    )
    assert out == "wss://staging.chipzen.ai/ws/external/match/m1/p1"


def test_resolve_gateway_url_passes_through_absolute_url():
    full = "wss://other.example/ws/external/match/m1/p1"
    assert external.resolve_gateway_url("wss://staging.chipzen.ai/x", full) == full


# ---------------------------------------------------------------------------
# Bot factory normalization
# ---------------------------------------------------------------------------


def test_as_factory_instance_is_reused():
    bot = _CollectBot()
    factory = external._as_factory(bot)
    assert factory() is bot
    assert factory() is bot  # same instance every call


def test_as_factory_class_makes_fresh_instances():
    factory = external._as_factory(_CollectBot)
    a, b = factory(), factory()
    assert isinstance(a, _CollectBot) and isinstance(b, _CollectBot)
    assert a is not b


def test_as_factory_rejects_non_bot():
    with pytest.raises(TypeError):
        external._as_factory(object())


# ---------------------------------------------------------------------------
# Connection resolution + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_a_token():
    cfg = ChipzenConfig(path=Path("x"), token=None)
    with pytest.raises(ValueError, match="requires an external-API token"):
        await run_external_bot(_CollectBot(), url=LOBBY_URL, config=cfg)


@pytest.mark.asyncio
async def test_requires_url_or_bot_id():
    cfg = ChipzenConfig(path=Path("x"), token="cz_extbot_x", bot_id=None)
    with pytest.raises(ValueError, match="needs a lobby URL"):
        await run_external_bot(_CollectBot(), config=cfg)


@pytest.mark.asyncio
async def test_token_from_config_when_no_kwarg(monkeypatch):
    calls = _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    cfg = ChipzenConfig(path=Path("x"), token="cz_extbot_from_config")
    await run_external_bot(_CollectBot(), url=LOBBY_URL, config=cfg, max_matches=1)
    auth = calls["lobby_ws"].sent[0]
    assert auth["type"] == "authenticate"
    assert auth["token"] == "cz_extbot_from_config"


@pytest.mark.asyncio
async def test_explicit_token_overrides_config(monkeypatch):
    calls = _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    cfg = ChipzenConfig(path=Path("x"), token="cz_extbot_config")
    await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_explicit", config=cfg, max_matches=1
    )
    assert calls["lobby_ws"].sent[0]["token"] == "cz_extbot_explicit"


@pytest.mark.asyncio
async def test_bot_id_plus_env_builds_lobby_url(monkeypatch):
    calls = _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    cfg = ChipzenConfig(path=Path("x"), token="cz_extbot_x")
    await run_external_bot(_CollectBot(), bot_id="abc", env="staging", config=cfg, max_matches=1)
    assert calls["lobby"][0] == "wss://staging.chipzen.ai/ws/external/bot/abc"


# ---------------------------------------------------------------------------
# Lobby -> gateway happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plays_one_match_end_to_end(monkeypatch):
    calls = _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    bot = _CollectBot()
    results = await run_external_bot(bot, url=LOBBY_URL, token="cz_extbot_x", max_matches=1)

    # One match, captured with its match_end payload.
    assert len(results) == 1
    assert results[0]["end"]["reason"] == "complete"
    assert results[0]["match_id"] == "m1"

    # The bot ran the full lifecycle + the latency hook fired once.
    assert bot.events == ["match_start", "decide", "match_end"]
    assert len(bot.latencies) == 1

    # Gateway leg carried the token in the Sec-WebSocket-Protocol offer.
    assert calls["subprotocols"][0] == [external.BOT_TOKEN_SUBPROTOCOL, "cz_extbot_x"]
    # Default non-default UA.
    assert calls["ua"][0].startswith("chipzen-sdk-python/")


@pytest.mark.asyncio
async def test_lobby_answers_ping_with_pong(monkeypatch):
    calls = _install_transport(monkeypatch, [_server_hello_lobby(), {"type": "ping"}, _matched()])
    await run_external_bot(_CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1)
    sent_types = [m["type"] for m in calls["lobby_ws"].sent]
    assert "pong" in sent_types


@pytest.mark.asyncio
async def test_evict_ends_session_with_no_match(monkeypatch):
    _install_transport(monkeypatch, [_server_hello_lobby(), {"type": "evict"}])
    results = await run_external_bot(_CollectBot(), url=LOBBY_URL, token="cz_extbot_x")
    assert results == []


@pytest.mark.asyncio
async def test_max_matches_stops_after_one(monkeypatch):
    # Only one matched is scripted; without max_matches the daemon would block.
    _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1
    )
    assert len(results) == 1


# ---------------------------------------------------------------------------
# safe_mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_mode_false_propagates_bot_error(monkeypatch):
    _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    with pytest.raises(BotDecisionError):
        await run_external_bot(
            _CrashBot(), url=LOBBY_URL, token="cz_extbot_x", safe_mode=False, max_matches=1
        )


@pytest.mark.asyncio
async def test_safe_mode_true_folds_bot_error(monkeypatch):
    # The crash is folded; the match still reaches match_end and is recorded.
    _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])
    results = await run_external_bot(
        _CrashBot(), url=LOBBY_URL, token="cz_extbot_x", safe_mode=True, max_matches=1
    )
    assert len(results) == 1
    assert results[0]["end"]["reason"] == "complete"


def _server_hello_lobby() -> dict:
    """The lobby's server hello (endpoint=lobby); the client does NOT reply
    with a client hello on the lobby leg."""
    return {"type": "hello", "endpoint": "lobby"}


# ---------------------------------------------------------------------------
# Reconnect behavior (gateway mid-match drop + lobby drop)
# ---------------------------------------------------------------------------

#: Sentinel placed in a scripted lobby frame list to make the next ``recv``
#: raise (simulating a lobby socket drop).
_CLOSE = object()


class _ScriptedLobbyWS:
    """Lobby WS that replays scripted frames; a ``_CLOSE`` sentinel makes the
    next ``recv`` raise (drop), and once frames are exhausted ``recv`` blocks."""

    def __init__(self, frames: list):
        self.sent: list[dict] = []
        self._frames = list(frames)

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if self._frames:
            frame = self._frames.pop(0)
            if frame is _CLOSE:
                raise ConnectionError("simulated lobby drop")
            return json.dumps(frame)
        # Block until the loop's wait_for cancels us. (Not asyncio.sleep — this
        # transport stubs asyncio.sleep to make backoff instant.)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _install_scripted_transport(monkeypatch, *, lobby_scripts, gateway_scripts):
    """Like _install_transport but pops a fresh script per lobby/gateway
    connect, so successive connects can behave differently (reconnect tests).

    A gateway script that does NOT end in ``match_end`` closes cleanly →
    ``_run_session`` returns ``None`` → ``_play_one_match`` reconnects.
    ``asyncio.sleep`` is stubbed (recording delays) so backoff is instant.
    """
    lobby_iter = iter(lobby_scripts)
    gw_iter = iter(gateway_scripts)
    calls: dict = {"lobby": [], "gateway": [], "subprotocols": [], "sleeps": []}

    def _connect(url, *, max_size=None, user_agent_header=None, subprotocols=None):
        if "/ws/external/match/" in url:
            calls["gateway"].append(url)
            calls["subprotocols"].append(subprotocols)
            return _Conn(_GatewayWS(list(next(gw_iter))))
        calls["lobby"].append(url)
        return _Conn(_ScriptedLobbyWS(list(next(lobby_iter))))

    async def _record_sleep(delay):
        calls["sleeps"].append(delay)

    monkeypatch.setattr(external.websockets, "connect", _connect)
    monkeypatch.setattr(external, "_LOBBY_RECV_TIMEOUT_S", 0.02)
    monkeypatch.setattr(external.asyncio, "sleep", _record_sleep)
    return calls


@pytest.mark.asyncio
async def test_gateway_reconnects_and_resumes(monkeypatch):
    # First gateway connect drops mid-match (no match_end); the SDK reconnects
    # and the second connect plays to match_end. The same bot instance is reused.
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[[_server_hello_lobby(), _matched()]],
        gateway_scripts=[
            [_server_hello(), _match_start(), _turn_request()],  # drops, no match_end
            [_server_hello(), _match_end()],  # reconnect → completes
        ],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1
    )
    assert len(calls["gateway"]) == 2  # reconnected once
    assert len(results) == 1
    assert results[0]["end"]["reason"] == "complete"


@pytest.mark.asyncio
async def test_gateway_reconnect_budget_exhausted_abandons_match(monkeypatch):
    # Gateway never reaches match_end. After max_reconnect_attempts the match is
    # abandoned (result end=None) rather than hanging or looping forever.
    policy = RetryPolicy(max_reconnect_attempts=2, initial_backoff_ms=1, max_backoff_ms=1)
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[[_server_hello_lobby(), _matched()]],
        gateway_scripts=[
            [_server_hello(), _match_start()],  # initial
            [_server_hello(), _match_start()],  # retry 1
            [_server_hello(), _match_start()],  # retry 2
        ],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", retry_policy=policy, max_matches=1
    )
    assert len(calls["gateway"]) == 3  # initial + 2 retries, then give up
    assert results[0]["end"] is None
    # Backoff used the policy (capped at 1ms = 0.001s) for each reconnect.
    assert calls["sleeps"] == [pytest.approx(0.001), pytest.approx(0.001)]


@pytest.mark.asyncio
async def test_lobby_reconnects_after_close(monkeypatch):
    # The lobby socket drops after connecting; the SDK reconnects the lobby and
    # then plays the match it's dispatched on the second session.
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[
            [_server_hello_lobby(), _CLOSE],  # connects, then drops
            [_server_hello_lobby(), _matched()],  # reconnect → a match arrives
        ],
        gateway_scripts=[_full_match()],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1
    )
    assert len(calls["lobby"]) == 2  # lobby reconnected
    assert len(results) == 1
    assert results[0]["end"]["reason"] == "complete"
