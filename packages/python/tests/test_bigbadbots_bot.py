"""Big Bad Bots bot base class, turn loop and safe fallback (chipzen-sdk#139).

The session tests drive the real ``_run_session`` with a scripted socket built
from the messages published in ``docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md``,
so no network is involved:

* ``BigBadBotsBot.decide`` routes each call to its phase method;
* a ``hello`` declaring ``state_shape: bigbadbots`` takes the Big Bad Bots
  path, and the answers sent are the doc's published ``turn_action`` examples;
* a raise, a timeout, a malformed answer or an unparseable payload sends the
  protocol's default (section 3.5), never a poker action;
* poker tables keep the path they always had.
"""

import asyncio
import copy
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
from chipzen.bigbadbots import (
    AuctionState,
    BigBadBotsGameConfig,
    BodyShopState,
    FightState,
    FittingState,
    parse_bigbadbots_turn_request,
)
from chipzen.bigbadbots_bot import BigBadBotsBot, fallback_action
from chipzen.bot import ChipzenBot
from chipzen.client import BotDecisionError, _run_session
from chipzen.models import Action, GameState

DOC = (
    Path(__file__).resolve().parents[3] / "docs" / "protocol" / "BIGBADBOTS-GAME-STATE-PROTOCOL.md"
)
_FENCE = re.compile(r"^```json\n(.*?)\n```$", re.MULTILINE | re.DOTALL)
PHASE_CALL = {"body_shop": "choose_body", "auction": "bid", "fitting": "fit", "fight": "act"}
MATCH_ID = "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84"


def _blocks() -> list[dict]:
    return [json.loads(body) for body in _FENCE.findall(DOC.read_text(encoding="utf-8"))]


def _request(call: str, **envelope) -> dict:
    """A fresh copy of the published ``turn_request`` for ``call``."""
    (found,) = [
        b
        for b in _blocks()
        if b.get("type") == "turn_request" and PHASE_CALL[b["state"]["phase"]] == call
    ]
    return {**copy.deepcopy(found), **envelope}


def _answer(request_id: str) -> dict:
    (found,) = [
        b for b in _blocks() if b.get("type") == "turn_action" and b["request_id"] == request_id
    ]
    return found


def _descriptor() -> dict:
    (found,) = [b for b in _blocks() if "state_shape" in b]
    return found


def _game_config() -> dict:
    (found,) = [b for b in _blocks() if "rule_set_id" in b]
    return found


def _fight_turn_result() -> dict:
    (found,) = [b for b in _blocks() if b.get("type") == "turn_result"]
    return copy.deepcopy(found)


def _server_hello(game: dict | None) -> dict:
    hello = {
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "server_ts": "2026-09-29T12:00:00.000Z",
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": game["game_type"] if game else "nlhe_6max",
        "capabilities": [],
    }
    if game is not None:
        hello["game"] = game
    return hello


def _match_start() -> dict:
    return {
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": 2,
        "server_ts": "2026-09-29T12:00:00.000Z",
        "seats": [
            {"seat": 0, "participant_id": "p0", "display_name": "Opp", "is_self": False},
            {"seat": 1, "participant_id": "p1", "display_name": "You", "is_self": True},
        ],
        "game_config": _game_config(),
        "turn_timeout_ms": 1000,
    }


def _round_start(round_number: int = 1) -> dict:
    return {
        "type": "round_start",
        "match_id": MATCH_ID,
        "round_id": f"r_{round_number}",
        "round_number": round_number,
        "state": {
            "hand_number": round_number,
            "round": round_number,
            "phase": "fight",
            "rounds_won": [0, 0],
            "board": [],
            "your_hole_cards": [],
        },
    }


def _phase_change(phase: str) -> dict:
    return {"type": "phase_change", "match_id": MATCH_ID, "state": {"phase": phase, "board": []}}


def _round_result() -> dict:
    return {
        "type": "round_result",
        "match_id": MATCH_ID,
        "round_id": "r_1",
        "result": {
            "hand_number": 1,
            "round": 1,
            "winner_seats": [1],
            "outcome": "ko",
            "forfeit": [],
            "rounds_won": [0, 1],
            "pot": 0,
            "payouts": [],
            "showdown": [],
            "action_history": [],
        },
    }


def _match_end() -> dict:
    return {
        "type": "match_end",
        "match_id": MATCH_ID,
        "reason": "complete",
        "results": [{"seat": 1, "score": 2}, {"seat": 0, "score": 0}],
    }


def _fight(tick: int = 21, round_number: int = 1, timeout_ms: int = 3100, **state) -> dict:
    request = _request("act", request_id=f"req_r{round_number}_t{tick}", timeout_ms=timeout_ms)
    request["state"].update(tick=tick, round=round_number, **state)
    return request


class MockWebSocket:
    """Replays a scripted server, records what the client sends."""

    def __init__(self, messages: list[dict]):
        self._messages = [json.dumps(m) for m in messages]
        self._index = 0
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        self._index += 1
        return self._messages[self._index - 1]

    async def recv(self) -> str:
        return await self.__anext__()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == kind]


async def _play(bot: ChipzenBot, messages: list[dict], **kwargs) -> tuple[MockWebSocket, dict]:
    ws = MockWebSocket(messages)
    end = await _run_session(
        ws,
        bot,
        match_id=MATCH_ID,
        token="",
        ticket=None,
        client_name="chipzen-sdk-test",
        client_version="0.0.0",
        **kwargs,
    )
    return ws, end


def _bbb_session(*turns: dict) -> list[dict]:
    return [_server_hello(_descriptor()), _match_start(), _round_start(), *turns, _match_end()]


class DocBot(BigBadBotsBot):
    """Plays the doc's published answers, and records every call it gets."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.hooks: list[str] = []

    def choose_body(self, state: BodyShopState) -> Action:
        self.calls.append(("choose_body", state))
        return Action.choose_body("golem")

    def bid(self, state: AuctionState) -> Action:
        self.calls.append(("bid", state))
        return Action.bid(0)

    def fit(self, state: FittingState) -> Action:
        self.calls.append(("fit", state))
        return Action.fit({0: "capacitor"})

    def act(self, state: FightState) -> Action:
        self.calls.append(("act", state))
        return Action.act("kick_low")

    def on_match_start(self, match_info: dict) -> None:
        super().on_match_start(match_info)
        self.hooks.append("match_start")

    def on_phase_change(self, message: dict) -> None:
        self.hooks.append(f"phase_change:{message['state']['phase']}")

    def on_round_start(self, message: dict) -> None:
        self.hooks.append("round_start")

    def on_turn_result(self, message: dict) -> None:
        self.hooks.append("turn_result")

    def on_round_result(self, message: dict) -> None:
        self.hooks.append("round_result")

    def on_match_end(self, results: dict) -> None:
        self.hooks.append("match_end")


class ScriptedFighter(BigBadBotsBot):
    """``act`` runs a callable per tick; counts how often it is called."""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.act_calls = 0

    def act(self, state: FightState) -> Action:
        self.act_calls += 1
        return self.behaviour(state)


def _raise(_state):
    raise RuntimeError("strategy bug")


def _sent_actions(ws: MockWebSocket) -> list[str]:
    return [m["action"] for m in ws.of_type("turn_action")]


# ---------------------------------------------------------------------------
# Phase routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "model"),
    [
        ("choose_body", BodyShopState),
        ("bid", AuctionState),
        ("fit", FittingState),
        ("act", FightState),
    ],
)
def test_decide_routes_each_phase_to_its_method(call, model):
    bot = DocBot()
    state = parse_bigbadbots_turn_request(_request(call))
    action = bot.decide(state)
    assert [name for name, _ in bot.calls] == [call]
    assert isinstance(bot.calls[0][1], model)
    request_id = state.request_id
    assert action.to_wire()["action"] == _answer(request_id)["action"]


def test_act_is_required():
    with pytest.raises(TypeError):
        BigBadBotsBot()  # type: ignore[abstract]


def test_default_gear_up_methods_are_legal():
    class FightOnly(BigBadBotsBot):
        def act(self, state: FightState) -> Action:
            return Action.act("idle")

    bot = FightOnly()
    body_shop = parse_bigbadbots_turn_request(_request("choose_body"))
    assert bot.decide(body_shop) == Action.choose_body("warden")  # first on the offer
    assert bot.decide(parse_bigbadbots_turn_request(_request("bid"))) == Action.bid(0)
    fitting = parse_bigbadbots_turn_request(_request("fit"))
    assert bot.decide(fitting) == Action.fit({0: "capacitor"})


def test_default_fit_uses_each_part_once_and_matches_slot_types():
    class FightOnly(BigBadBotsBot):
        def act(self, state: FightState) -> Action:
            return Action.act("idle")

    request = _request("fit")
    part = {"slot": "arms", "bonus": {}, "effects": {}}
    request["state"]["slots"] = ["arms", "legs", "arms", "core"]
    request["state"]["owned"] = [
        {**part, "id": "hammer"},
        {**part, "id": "spring", "slot": "legs"},
        {**part, "id": "shield_arm"},
        {**part, "id": "tactical", "slot": "head"},
    ]
    action = FightOnly().fit(parse_bigbadbots_turn_request(request))
    assert action.params == {"fit": {"0": "hammer", "1": "spring", "2": "shield_arm"}}


def test_decide_refuses_a_poker_state():
    poker = GameState.from_turn_request(
        {"request_id": "r", "valid_actions": ["fold"], "state": {"phase": "preflop"}}
    )
    with pytest.raises(TypeError):
        DocBot().decide(poker)


def test_bigbadbots_bot_is_exported_and_is_a_chipzen_bot():
    assert chipzen.BigBadBotsBot is BigBadBotsBot
    assert issubclass(BigBadBotsBot, chipzen.Bot)


# ---------------------------------------------------------------------------
# The protocol default (section 3.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("choose_body", Action.choose_body("warden")),
        ("bid", Action.bid(0)),
        ("fit", Action.fit({})),
        ("act", Action.act("idle")),
    ],
)
def test_fallback_is_the_servers_default_for_each_call(call, expected):
    assert fallback_action(_request(call), None) == expected


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        ("block", "block"),
        ("move_in", "move_in"),
        ("move_out", "move_out"),
        ("kick_low", "idle"),
        ("special", "idle"),
        ("idle", "idle"),
        (None, "idle"),
    ],
)
def test_fight_fallback_repeats_only_block_and_moves(previous, expected):
    assert fallback_action(_request("act"), previous) == Action.act(expected)


def test_fallback_has_no_answer_for_an_unknown_phase_or_missing_offer():
    unknown = _request("act")
    unknown["state"]["phase"] = "overtime"
    assert fallback_action(unknown, None) is None
    no_offer = _request("choose_body")
    no_offer["state"]["offer"] = []
    assert fallback_action(no_offer, None) is None
    assert fallback_action({"type": "turn_request"}, None) is None


# ---------------------------------------------------------------------------
# A scripted session, end to end, from the doc's messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scripted_session_from_the_protocol_doc():
    bot = DocBot()
    messages = [
        _server_hello(_descriptor()),
        _match_start(),
        _request("choose_body"),
        _phase_change("auction"),
        _request("bid"),
        _phase_change("fitting"),
        _request("fit"),
        _phase_change("fight"),
        _round_start(),
        _request("act"),
        _fight_turn_result(),
        _round_result(),
        _match_end(),
    ]
    ws, end = await _play(bot, messages)

    assert end == _match_end()
    (hello,) = ws.of_type("hello")
    assert hello["supported_games"] == ["bigbadbots"]

    sent = ws.of_type("turn_action")
    for request_id, message in zip(["req_00000", "req_00001", "req_00007", "req_00029"], sent):
        published = _answer(request_id)
        # ``params`` is optional for a fight action (section 4.4).
        assert message == {**published, "params": published.get("params", {})}
    assert len(sent) == 4

    assert [name for name, _ in bot.calls] == ["choose_body", "bid", "fit", "act"]
    assert isinstance(bot.game_config, BigBadBotsGameConfig)
    assert bot.game_config.rule_set_id == "bigbadbots-1.0"
    assert bot.hooks == [
        "match_start",
        "phase_change:auction",
        "phase_change:fitting",
        "phase_change:fight",
        "round_start",
        "turn_result",
        "round_result",
        "match_end",
    ]


@pytest.mark.asyncio
async def test_explicit_supported_games_wins_over_the_default():
    ws, _ = await _play(DocBot(), _bbb_session(), supported_games=["bigbadbots", "poker"])
    (hello,) = ws.of_type("hello")
    assert hello["supported_games"] == ["bigbadbots", "poker"]


@pytest.mark.asyncio
async def test_plain_bot_declaring_bigbadbots_gets_typed_states():
    """The routing is in the parser, not the base class: a plain ``Bot`` that
    declared the game receives the same models through ``decide``."""

    class OwnDispatch(ChipzenBot):
        def __init__(self) -> None:
            self.states: list[object] = []

        def decide(self, state):  # type: ignore[override]
            self.states.append(state)
            return Action.act("block")

    bot = OwnDispatch()
    ws, _ = await _play(bot, _bbb_session(_fight()), supported_games=["bigbadbots"])
    assert isinstance(bot.states[0], FightState)
    assert _sent_actions(ws) == ["block"]


@pytest.mark.asyncio
async def test_pending_request_on_reconnect_takes_the_bigbadbots_path():
    reconnected = {
        "type": "reconnected",
        "match_id": MATCH_ID,
        "round_number": 1,
        "seats": _match_start()["seats"],
        "game_config": _game_config(),
        "pending_request": _fight(tick=40),
    }
    bot = DocBot()
    ws, _ = await _play(bot, [_server_hello(_descriptor()), reconnected, _match_end()])
    assert _sent_actions(ws) == ["kick_low"]
    assert ws.of_type("turn_action")[0]["request_id"] == "req_r1_t40"
    assert bot.game_config is not None


# ---------------------------------------------------------------------------
# The fallback in the session loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raise_sends_the_default_not_a_fold(caplog):
    bot = ScriptedFighter(_raise)
    with caplog.at_level(logging.WARNING, logger="chipzen"):
        ws, end = await _play(bot, _bbb_session(_fight()))
    assert end is not None
    assert _sent_actions(ws) == ["idle"]
    assert "sending the protocol default" in caplog.text


@pytest.mark.asyncio
async def test_raise_repeats_the_applied_block_from_turn_result_within_the_round():
    result = _fight_turn_result()
    result["details"].update(action="block", round=1, tick=21)
    turns = [_fight(tick=21), result, _fight(tick=22)]
    calls = iter([lambda s: Action.act("punch_high"), _raise])
    bot = ScriptedFighter(lambda state: next(calls)(state))
    ws, _ = await _play(bot, _bbb_session(*turns))
    # The applied action (block, from turn_result) is repeated, not the
    # punch_high the bot sent.
    assert _sent_actions(ws) == ["punch_high", "block"]


@pytest.mark.asyncio
async def test_repeated_action_resets_every_round():
    turns = [_fight(tick=5), _round_start(2), _fight(tick=0, round_number=2)]
    calls = iter([lambda s: Action.act("move_in"), _raise])
    bot = ScriptedFighter(lambda state: next(calls)(state))
    ws, _ = await _play(bot, _bbb_session(*turns))
    assert _sent_actions(ws) == ["move_in", "idle"]


@pytest.mark.asyncio
async def test_raise_in_the_same_round_repeats_what_was_sent():
    turns = [_fight(tick=5), _fight(tick=6)]
    calls = iter([lambda s: Action.act("move_out"), _raise])
    bot = ScriptedFighter(lambda state: next(calls)(state))
    ws, _ = await _play(bot, _bbb_session(*turns))
    assert _sent_actions(ws) == ["move_out", "move_out"]


@pytest.mark.asyncio
async def test_raise_is_terminal_without_safe_mode():
    with pytest.raises(BotDecisionError):
        await _play(ScriptedFighter(_raise), _bbb_session(_fight()), safe_mode=False)


@pytest.mark.parametrize(
    "bad", [None, Action.check(), Action.bid(5), "kick_low"], ids=["none", "poker", "gearup", "str"]
)
@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_valid_action_is_replaced(bad, caplog):
    with caplog.at_level(logging.ERROR, logger="chipzen"):
        ws, _ = await _play(ScriptedFighter(lambda s: bad), _bbb_session(_fight()))
    assert _sent_actions(ws) == ["idle"]
    assert "not one of" in caplog.text


@pytest.mark.asyncio
async def test_an_invalid_answer_is_terminal_without_safe_mode():
    with pytest.raises(BotDecisionError):
        await _play(
            ScriptedFighter(lambda s: Action.check()), _bbb_session(_fight()), safe_mode=False
        )


@pytest.mark.asyncio
async def test_slow_answer_is_replaced_and_the_handler_is_not_stacked(caplog):
    """A decision still running near ``timeout_ms`` gets the default; while it
    runs, the next tick gets the default without a second call."""

    def slow(state):
        time.sleep(0.4)
        return Action.act("throw")

    bot = ScriptedFighter(slow)
    turns = [_fight(tick=1, timeout_ms=60), _fight(tick=2, timeout_ms=60)]
    with caplog.at_level(logging.WARNING, logger="chipzen"):
        started = time.monotonic()
        ws, end = await _play(bot, _bbb_session(*turns))
        elapsed = time.monotonic() - started
    assert end is not None
    assert _sent_actions(ws) == ["idle", "idle"]
    assert bot.act_calls == 1
    assert elapsed < 0.35  # answered on budget, not after the 0.4 s handler
    assert "no answer within 40ms" in caplog.text
    assert "still running" in caplog.text
    await asyncio.sleep(0.5)  # let the worker thread finish before teardown


@pytest.mark.asyncio
async def test_answer_within_budget_is_sent():
    def quick(state):
        time.sleep(0.01)
        return Action.act("uppercut")

    ws, _ = await _play(ScriptedFighter(quick), _bbb_session(_fight(timeout_ms=500)))
    assert _sent_actions(ws) == ["uppercut"]


@pytest.mark.asyncio
async def test_unparseable_payload_sends_the_default_and_never_calls_the_bot():
    broken = _fight()
    del broken["state"]["walls"]
    bot = ScriptedFighter(lambda s: Action.act("kick_high"))
    ws, _ = await _play(bot, _bbb_session(broken))
    assert _sent_actions(ws) == ["idle"]
    assert bot.act_calls == 0


@pytest.mark.asyncio
async def test_unparseable_payload_is_terminal_without_safe_mode():
    broken = _fight()
    del broken["state"]["walls"]
    with pytest.raises(BotDecisionError):
        await _play(DocBot(), _bbb_session(broken), safe_mode=False)


@pytest.mark.asyncio
async def test_rejected_answer_is_retried_once_with_the_default():
    rejection = {
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "request_id": "req_00001",
        "reason": "invalid_params",
        "message": "bad",
        "remaining_ms": 500,
        "valid_actions": ["bid"],
    }
    turns = [_request("bid"), rejection, dict(rejection)]
    ws, _ = await _play(DocBot(), _bbb_session(*turns))
    sent = ws.of_type("turn_action")
    assert [(m["action"], m["params"]) for m in sent] == [
        ("bid", {"amount": 0}),
        ("bid", {"amount": 0}),
    ]
    assert all(m["request_id"] == "req_00001" for m in sent)


@pytest.mark.asyncio
async def test_rejected_body_choice_retries_the_first_body_on_the_offer():
    rejection = {
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "request_id": "req_00000",
        "remaining_ms": 500,
        "valid_actions": ["choose_body"],
    }
    ws, _ = await _play(DocBot(), _bbb_session(_request("choose_body"), rejection))
    sent = ws.of_type("turn_action")
    assert [m["params"] for m in sent] == [{"body": "golem"}, {"body": "warden"}]


# ---------------------------------------------------------------------------
# state_shape routing in the handshake; poker unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_state_shape_ends_the_session_before_hello(caplog):
    game = {**_descriptor(), "game_type": "chess", "state_shape": "chess"}
    with caplog.at_level(logging.ERROR, logger="chipzen"):
        ws, end = await _play(DocBot(), [_server_hello(game), _match_start(), _match_end()])
    assert end is None
    assert ws.of_type("hello") == []
    assert "chess" in caplog.text


@pytest.mark.asyncio
async def test_malformed_game_descriptor_ends_the_session():
    game = {k: v for k, v in _descriptor().items() if k != "state_shape"}
    ws, end = await _play(DocBot(), [_server_hello(game), _match_end()])
    assert end is None
    assert ws.of_type("hello") == []


@pytest.mark.asyncio
async def test_bigbadbots_bot_refuses_a_poker_table():
    ws, end = await _play(DocBot(), [_server_hello(None), _match_end()])
    assert end is None
    assert ws.of_type("hello") == []


class PokerBot(ChipzenBot):
    def __init__(self) -> None:
        self.states: list[GameState] = []

    def decide(self, state: GameState) -> Action:
        self.states.append(state)
        raise RuntimeError("poker strategy bug")


def _poker_turn() -> dict:
    return {
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seat": 0,
        "request_id": "req_poker",
        "timeout_ms": 5,
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": 1,
            "phase": "preflop",
            "board": [],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 15,
            "to_call": 10,
            "min_raise": 20,
            "max_raise": 1000,
            "your_stack": 990,
            "opponent_stacks": [995],
        },
    }


@pytest.mark.parametrize("shape", [None, "27tripledraw", "pineapple"])
@pytest.mark.asyncio
async def test_poker_shapes_keep_the_poker_path(shape):
    """A poker table parses a ``GameState``, folds on a raise as it always did,
    applies no Big Bad Bots timeout (``timeout_ms`` here is 5) and declares
    nothing it was not asked to."""
    game = None if shape is None else {"game_type": "x", "state_shape": shape}
    bot = PokerBot()
    ws, end = await _play(bot, [_server_hello(game), _poker_turn(), _match_end()])
    assert end is not None
    assert isinstance(bot.states[0], GameState)
    assert _sent_actions(ws) == ["fold"]
    (hello,) = ws.of_type("hello")
    assert list(hello) == [
        "type",
        "match_id",
        "supported_versions",
        "client_name",
        "client_version",
    ]
