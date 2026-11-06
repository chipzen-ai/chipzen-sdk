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
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import chipzen.external as external
from chipzen import Action, Bot, ChipzenConfig, RetryPolicy, run_external_bot
from chipzen.client import BotDecisionError

LOBBY_URL = "wss://staging.chipzen.ai/ws/external/bot/test-bot-uuid"

#: The real ``asyncio.sleep``: the scripted transport below stubs it, so a test
#: step that must let the other tasks run keeps this reference.
_real_sleep = asyncio.sleep


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


def test_resolve_gateway_url_passes_through_same_origin_absolute_url():
    # An absolute URL on the SAME origin as the lobby is honored as-is.
    full = "wss://staging.chipzen.ai/ws/external/match/m1/p1"
    assert external.resolve_gateway_url("wss://staging.chipzen.ai/x", full) == full


def test_resolve_gateway_url_rejects_cross_origin():
    # A server-supplied absolute URL on a DIFFERENT host must be refused —
    # the bot token must not follow a redirect to another host.
    with pytest.raises(ValueError, match="cross-origin"):
        external.resolve_gateway_url(
            "wss://staging.chipzen.ai/x", "wss://attacker.example/ws/external/match/m1/p1"
        )


def test_resolve_gateway_url_rejects_wss_to_ws_downgrade():
    # Same host but ws:// (cleartext) when the lobby was wss:// — refused.
    with pytest.raises(ValueError, match="cross-origin or insecure"):
        external.resolve_gateway_url(
            "wss://staging.chipzen.ai/x", "ws://staging.chipzen.ai/ws/external/match/m1/p1"
        )


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
# Slow decision must NOT block the shared session loop (chipzen-ai/Chipzen#3904)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_decide_does_not_block_the_session_loop(monkeypatch):
    """Regression for #3904: a decision outstanding for the whole clock must
    not pin the session event loop.

    The External-API session runs the lobby WS (keepalive) and every match on
    ONE shared loop. Before the fix, ``bot.decide()`` was called inline on that
    loop, so a slow decision (an LLM agent, or the MCP bridge waiting on an
    ``act`` call) blocked the loop entirely — starving the lobby heartbeat and
    any co-scheduled work until the decision returned.

    The probe: a ``releaser`` coroutine scheduled on the SAME loop must be able
    to run and set ``released`` WHILE ``decide()`` is outstanding. With the fix
    (``decide`` dispatched to a worker thread) the loop keeps running, the
    releaser fires within milliseconds, and ``decide``'s bounded wait returns
    released=True. On the old inline code the loop is pinned, the releaser never
    runs during the block, and ``decide``'s wait times out (released=False) —
    failing this test deterministically (no infinite hang).
    """
    released = threading.Event()
    decide_started = threading.Event()

    class _SlowBot(Bot):
        def __init__(self):
            self.released_in_time: bool | None = None

        def decide(self, state):
            decide_started.set()
            # Block up to 2s waiting for the loop to release us. This stands in
            # for a slow agent / the MCP bridge's threading.Event.wait().
            self.released_in_time = released.wait(timeout=2.0)
            return Action.check() if "check" in state.valid_actions else Action.fold()

    calls = _install_transport(monkeypatch, [_server_hello_lobby(), _matched()])

    async def releaser():
        # If the loop is alive while decide() blocks, this runs and releases it
        # near-instantly. If the loop is pinned by an inline decide(), this can
        # only run AFTER decide() already returned — too late.
        for _ in range(400):  # bounded (~2s) so the old code fails, never hangs
            if decide_started.is_set():
                released.set()
                return
            await asyncio.sleep(0.005)

    bot = _SlowBot()
    task = asyncio.create_task(releaser())
    started = time.monotonic()
    results = await run_external_bot(bot, url=LOBBY_URL, token="cz_extbot_x", max_matches=1)
    await task
    elapsed = time.monotonic() - started

    # The loop kept running while decide() was outstanding: the releaser fired
    # and decide() returned because it was released, not because it timed out.
    assert bot.released_in_time is True
    # And it happened promptly — nowhere near decide()'s 2s fallback timeout.
    assert elapsed < 1.5
    # Turn handoff intact: the match played through to a clean match_end.
    assert len(results) == 1
    assert results[0]["end"]["reason"] == "complete"
    assert results[0]["match_id"] == "m1"


# ---------------------------------------------------------------------------
# Reconnect behavior (gateway mid-match drop + lobby drop)
# ---------------------------------------------------------------------------

#: Sentinel placed in a scripted lobby frame list to make the next ``recv``
#: raise (simulating a lobby socket drop).
_CLOSE = object()


class _ScriptedLobbyWS:
    """Lobby WS that replays scripted frames; a ``_CLOSE`` sentinel makes the
    next ``recv`` raise (drop), an async callable is a test step run between
    frames, and once frames are exhausted ``recv`` blocks."""

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
            if callable(frame):
                await frame()
                return await self.recv()
            return json.dumps(frame)
        # Block until the loop's wait_for cancels us. (Not asyncio.sleep — this
        # transport stubs asyncio.sleep to make backoff instant.)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _install_scripted_transport(monkeypatch, *, lobby_scripts, gateway_scripts):
    """Like _install_transport but pops a fresh script per lobby/gateway
    connect, so successive connects can behave differently (reconnect tests).

    A gateway script that does NOT end in ``match_end`` closes cleanly →
    ``_run_session`` returns ``None`` → ``_play_one_match`` reconnects. A script
    may also be an already-built gateway WS.
    ``asyncio.sleep`` is stubbed (recording delays) so backoff is instant.
    """
    lobby_iter = iter(lobby_scripts)
    gw_iter = iter(gateway_scripts)
    calls: dict = {"lobby": [], "gateway": [], "subprotocols": [], "sleeps": []}

    def _connect(url, *, max_size=None, user_agent_header=None, subprotocols=None):
        if "/ws/external/match/" in url:
            calls["gateway"].append(url)
            calls["subprotocols"].append(subprotocols)
            script = next(gw_iter)
            return _Conn(script if isinstance(script, _GatewayWS) else _GatewayWS(list(script)))
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


# ---------------------------------------------------------------------------
# A repeat ``matched`` for a match still being played (chipzen-ai/Chipzen#6629)
# ---------------------------------------------------------------------------
#
# On lobby connect the platform re-sends ``matched`` (``resume: True``) for every
# match the bot holds a seat in, so that a RESTARTED process can pick its match
# back up. A process that only lost its lobby socket is still playing that match
# on its own gateway socket; opening a second one gets ``duplicate_participant``.


class _HeldGatewayWS(_GatewayWS):
    """A healthy mid-match gateway: holds the socket open before its last frame
    until ``release`` is set."""

    def __init__(self, frames: list[dict], release: asyncio.Event):
        super().__init__(frames)
        self._release = release

    async def __anext__(self) -> str:
        if self._idx == len(self._rest) - 1:
            await self._release.wait()
        return await super().__anext__()


def _matched_for(match_id: str, **extra) -> dict:
    return {**_matched(f"/ws/external/match/{match_id}/p1"), "match_id": match_id, **extra}


def _counting_factory():
    made: list[_CollectBot] = []

    def factory() -> _CollectBot:
        made.append(_CollectBot())
        return made[-1]

    return made, factory


async def _let_tasks_run() -> None:
    for _ in range(50):
        await _real_sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_matches", [1, None], ids=["one-match", "long-running"])
async def test_lobby_reconnect_mid_match_keeps_the_one_gateway(monkeypatch, max_matches):
    # The #6629 sequence: the lobby drops mid-match, the reconnected lobby
    # re-dispatches the match, and the bot must stay on the gateway it has.
    release = asyncio.Event()

    async def finish_the_match():
        release.set()
        await _let_tasks_run()

    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[
            [_server_hello_lobby(), _matched(), _CLOSE],
            [
                _server_hello_lobby(),
                _matched_for("m1", resume=True),
                finish_the_match,
                {"type": "evict"},
            ],
        ],
        gateway_scripts=[_HeldGatewayWS(_full_match(), release)],
    )
    made, factory = _counting_factory()
    results = await run_external_bot(
        factory, url=LOBBY_URL, token="cz_extbot_x", max_matches=max_matches
    )
    assert len(calls["lobby"]) == 2  # the lobby did reconnect
    assert len(calls["gateway"]) == 1  # ...and no second gateway opened
    assert len(made) == 1  # no bot instance built for the repeat frame
    assert len(results) == 1  # the ignored frame is not a played match
    assert results[0]["end"]["reason"] == "complete"
    assert made[0].events[-1] == "match_end"  # the original match played out


@pytest.mark.asyncio
async def test_repeat_matched_on_the_same_lobby_socket_is_ignored(monkeypatch):
    release = asyncio.Event()

    async def finish_the_match():
        release.set()
        await _let_tasks_run()

    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[[_server_hello_lobby(), _matched(), _matched(), finish_the_match]],
        gateway_scripts=[_HeldGatewayWS(_full_match(), release)],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1
    )
    assert len(calls["gateway"]) == 1
    assert len(results) == 1


@pytest.mark.asyncio
async def test_new_match_id_during_a_live_match_starts_normally(monkeypatch):
    # Overlapping matches are supported: only the SAME id is a repeat.
    release = asyncio.Event()

    async def finish_the_first_match():
        await _let_tasks_run()
        release.set()
        await _let_tasks_run()

    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[
            [
                _server_hello_lobby(),
                _matched_for("m1"),
                _matched_for("m2"),
                finish_the_first_match,
                {"type": "evict"},  # ends the session even if m2 was ignored
            ]
        ],
        gateway_scripts=[_HeldGatewayWS(_full_match(), release), _full_match()],
    )
    made, factory = _counting_factory()
    results = await run_external_bot(factory, url=LOBBY_URL, token="cz_extbot_x", max_matches=2)
    assert [url.rsplit("/match/", 1)[1] for url in calls["gateway"]] == ["m1/p1", "m2/p1"]
    assert len(made) == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_matched_after_the_task_gave_up_plays_again(monkeypatch):
    # "Still playing" means a live task. If this session abandoned the match
    # (reconnect budget spent) and the platform still holds the seat, a later
    # re-dispatch must get a fresh attempt, not be ignored.
    policy = RetryPolicy(max_reconnect_attempts=1, initial_backoff_ms=1, max_backoff_ms=1)
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[
            [
                _server_hello_lobby(),
                _matched(),
                _let_tasks_run,
                _matched_for("m1", resume=True),
                _let_tasks_run,
                {"type": "evict"},  # ends the session even if the repeat was ignored
            ],
        ],
        gateway_scripts=[
            [_server_hello(), _match_start()],  # drops
            [_server_hello(), _match_start()],  # retry drops: abandoned
            _full_match(),  # the re-dispatch plays to match_end
        ],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", retry_policy=policy, max_matches=2
    )
    assert len(calls["gateway"]) == 3
    assert [r["end"]["reason"] if r["end"] else None for r in results] == [None, "complete"]


# ---------------------------------------------------------------------------
# A match seat taken over by another connection of the same bot (#242)
# ---------------------------------------------------------------------------
#
# A newer socket for the same seat takes the match over (chipzen-ai/Chipzen#6734).
# The old socket gets an ``error`` frame (``replaced_by_new_connection``) and
# then a 4000 close. Reconnecting would take the seat back, and two live copies
# of one bot would trade it forever, so the match's task ends instead.

_SEAT_REPLACED_LINE = "seat taken over by another connection of this bot"


def _close(code: int, reason: str = "") -> Close:
    return Close(code, reason)


class _ClosingGatewayWS(_GatewayWS):
    """A gateway that, after its frames, raises the close a real socket would:
    ``rcvd`` is the server's close frame, or ``None`` for a drop with no close
    frame (1006)."""

    def __init__(self, frames: list[dict], rcvd: Close | None):
        super().__init__(frames)
        self._rcvd = rcvd

    async def __anext__(self) -> str:
        if self._idx < len(self._rest):
            return await super().__anext__()
        raise ConnectionClosedError(self._rcvd, None)


def _replaced_error_frame() -> dict:
    return {
        "type": "error",
        "code": "replaced_by_new_connection",
        "message": "a newer connection for this seat replaced this one",
    }


def _seat_replaced_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if _SEAT_REPLACED_LINE in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frames",
    [
        [_server_hello(), _match_start(), _turn_request()],
        [_server_hello(), _match_start(), _replaced_error_frame()],
    ],
    ids=["4000-close", "error-frame-then-close"],
)
async def test_seat_taken_over_ends_the_match_without_reconnecting(monkeypatch, caplog, frames):
    caplog.set_level("INFO")
    replaced = _ClosingGatewayWS(frames, _close(4000, "replaced_by_new_connection"))
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[[_server_hello_lobby(), _matched(), _let_tasks_run, {"type": "evict"}]],
        # A reconnect would need a second script; there is none.
        gateway_scripts=[replaced],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1
    )
    assert len(calls["gateway"]) == 1  # no reconnect
    assert calls["sleeps"] == []  # no backoff either
    assert results == []  # not a finished match, and no bogus result entry
    assert _seat_replaced_lines(caplog) == [f"match m1: {_SEAT_REPLACED_LINE}; not reconnecting"]
    assert not any("Server error" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rcvd",
    [None, _close(1011, "internal error"), _close(4001, "")],
    ids=["no-close-frame-1006", "1011", "4001"],
)
async def test_other_drops_still_reconnect(monkeypatch, caplog, rcvd):
    caplog.set_level("INFO")
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[[_server_hello_lobby(), _matched()]],
        gateway_scripts=[
            _ClosingGatewayWS([_server_hello(), _match_start(), _turn_request()], rcvd),
            [_server_hello(), _match_end()],
        ],
    )
    results = await run_external_bot(
        _CollectBot(), url=LOBBY_URL, token="cz_extbot_x", max_matches=1
    )
    assert len(calls["gateway"]) == 2  # reconnected once
    assert results[0]["end"]["reason"] == "complete"
    assert _seat_replaced_lines(caplog) == []


@pytest.mark.asyncio
async def test_after_a_takeover_a_new_match_plays_and_the_old_one_is_not_retaken(
    monkeypatch, caplog
):
    caplog.set_level("INFO")
    calls = _install_scripted_transport(
        monkeypatch,
        lobby_scripts=[
            [
                _server_hello_lobby(),
                _matched_for("m1"),
                _let_tasks_run,
                # The lobby re-sends matched for m1 (the bot still holds that
                # seat, on the other connection): taking it would steal it back.
                _matched_for("m1", resume=True),
                _matched_for("m2"),
                _let_tasks_run,
                {"type": "evict"},
            ]
        ],
        gateway_scripts=[
            _ClosingGatewayWS([_server_hello(), _match_start()], _close(4000)),
            _full_match(),
        ],
    )
    made, factory = _counting_factory()
    results = await run_external_bot(factory, url=LOBBY_URL, token="cz_extbot_x", max_matches=1)
    assert [url.rsplit("/match/", 1)[1] for url in calls["gateway"]] == ["m1/p1", "m2/p1"]
    assert len(made) == 2  # no bot built for the ignored re-dispatch
    assert len(results) == 1
    assert results[0]["end"]["reason"] == "complete"
    assert len(_seat_replaced_lines(caplog)) == 1
