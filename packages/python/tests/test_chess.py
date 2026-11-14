"""Tests for chess (preview): models, ChessBot, the session loop, validate and the starters."""

from __future__ import annotations

import ast
import asyncio
import copy
import dataclasses
import json
import logging
import threading
import time
from pathlib import Path

import pytest

import chipzen
from chipzen import scaffold
from chipzen.chess import (
    REJECTION_CODES,
    ChessClock,
    ChessGameConfig,
    ChessState,
    ChessStateParseError,
    first_legal_move,
    is_uci_move,
    parse_chess_turn_request,
)
from chipzen.chess_bot import ChessBot, fallback_action, is_move_answer
from chipzen.chess_validate import (
    judge_answer,
    judge_turn_action,
    load_fixtures,
    run_chess_checks,
)
from chipzen.client import BotDecisionError, _run_session
from chipzen.models import Action, GameState, parse_turn_request
from chipzen.scaffold import scaffold_bot
from chipzen.validate import validate_bot

PACKAGE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PACKAGE_DIR / "src" / "chipzen"
STARTERS = PACKAGE_DIR / "starters" / "chess"
MATCH_ID = "chess-test-match"
FIXTURES = load_fixtures()
PAIRS = FIXTURES["pairs"]


def _requests() -> list[dict]:
    return [copy.deepcopy(p["request"]) for p in PAIRS]


def _request(source_part: str = "", side: str | None = None) -> dict:
    for pair in PAIRS:
        state = pair["request"]["state"]
        if source_part in pair["source"] and (side is None or state["side"] == side):
            return copy.deepcopy(pair["request"])
    raise LookupError(source_part)


def _with_clock(request: dict, window_ms: int, bank_ms: int) -> dict:
    request = copy.deepcopy(request)
    request["state"]["clock"] = {
        "window_ms": window_ms,
        "my_bank_ms": bank_ms,
        "opp_bank_ms": bank_ms,
    }
    request["timeout_ms"] = window_ms + bank_ms
    return request


# ---------------------------------------------------------------------------
# No engine, no GPL library, anywhere in the SDK
# ---------------------------------------------------------------------------


def test_sdk_imports_no_chess_library():
    """The SDK ships no rules: no module imports python-chess or any other chess library."""
    for path in SRC_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                assert top not in {"chess", "chess_engine", "stockfish"}, (path, name)


def test_starters_import_no_chess_library():
    for path in STARTERS.rglob("*.py"):
        text = path.read_text("utf-8")
        assert "import chess\n" not in text and "from chess " not in text, path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_fixtures_cover_both_sides_and_the_special_moves():
    sides = {p["request"]["state"]["side"] for p in PAIRS}
    assert sides == {"white", "black"}
    sources = " ".join(p["source"] for p in PAIRS)
    for kind in ("promotion", "en_passant", "castling"):
        assert kind in sources
    codes = {a.get("code") for p in PAIRS for a in p["answers"]}
    assert codes - {None} == set(REJECTION_CODES)
    plies = [p["request"]["state"]["ply"] for p in PAIRS]
    assert min(plies) == 0 and max(plies) >= 290


@pytest.mark.parametrize("pair", PAIRS, ids=[p["request"]["request_id"] for p in PAIRS])
def test_every_fixture_request_parses_and_round_trips(pair):
    request = pair["request"]
    state = parse_chess_turn_request(request)
    assert state.to_state() == request["state"]
    assert state.valid_actions == ["move"]
    assert state.timeout_ms == state.clock.budget_ms
    assert state.legal_moves == sorted(state.legal_moves)
    assert all(is_uci_move(m) for m in state.legal_moves)
    assert len(state.moves) == state.ply
    assert len(state.my_moves) + len(state.opponent_moves) == len(state.moves)


@pytest.mark.parametrize("pair", PAIRS, ids=[p["request"]["request_id"] for p in PAIRS])
def test_judge_matches_every_engine_label(pair):
    """The SDK's judge agrees with the engine's verdict on every recorded answer."""
    for answer in pair["answers"]:
        verdict, code = judge_turn_action(pair["request"], answer["answer"])
        assert verdict == answer["expect"], answer
        assert code == answer.get("code"), answer


def test_judge_rejects_oversized_and_non_json():
    request = _requests()[0]
    assert judge_turn_action(request, "{not json") == ("rejected", "bad_shape")
    big = json.dumps({"action": "move", "params": {"move": "e2e4"}, "x": "y" * 5000})
    assert judge_turn_action(request, big) == ("rejected", "bad_shape")
    assert judge_answer(request, None) == ("rejected", "bad_shape")
    assert judge_answer(request, {"action": "move", "params": "e2e4"}) == (
        "rejected",
        "bad_shape",
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_side_moves_split():
    request = _request("scholars")
    state = parse_chess_turn_request(request)
    if state.side == "white":
        assert state.my_moves == state.moves[0::2]
        assert state.opponent_moves == state.moves[1::2]
    else:
        assert state.my_moves == state.moves[1::2]
        assert state.opponent_moves == state.moves[0::2]
    assert state.is_white == (state.side == "white")
    assert state.first_side == "white"


def test_black_to_start_position_splits_moves_the_other_way():
    request = _request()
    request["state"]["start_fen"] = "8/8/8/8/8/8/8/K6k b - - 0 1"
    request["state"]["moves"] = ["h1h2", "a1a2", "h2h3"]
    request["state"]["side"] = "white"
    state = parse_chess_turn_request(request)
    assert state.first_side == "black"
    assert state.my_moves == ["a1a2"]
    assert state.opponent_moves == ["h1h2", "h2h3"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["state"].pop("legal_moves"), "legal_moves"),
        (lambda r: r["state"].update(variant="fog"), "not supported"),
        (lambda r: r["state"].update(side="red"), "side"),
        (lambda r: r["state"]["clock"].update(my_bank_ms="1"), "my_bank_ms"),
        (lambda r: r.pop("state"), "state"),
        (lambda r: r.update(timeout_ms=True), "timeout_ms"),
    ],
)
def test_parse_is_strict(mutate, message):
    request = _requests()[0]
    mutate(request)
    with pytest.raises(ChessStateParseError, match=message):
        parse_chess_turn_request(request)


def test_parse_turn_request_routes_chess():
    request = _requests()[0]
    assert isinstance(parse_turn_request(request, state_shape="chess"), ChessState)


def test_action_move():
    assert Action.move("e7e8q").to_wire() == {"action": "move", "params": {"move": "e7e8q"}}
    for bad in ("E2E4", "e2-e4", "e2e4k", "e9e4", ""):
        with pytest.raises(ValueError):
            Action.move(bad)


def test_clock_and_game_config():
    clock = ChessClock(window_ms=1000, my_bank_ms=41250, opp_bank_ms=52800)
    assert clock.budget_ms == 42250
    assert ChessClock.from_dict(clock.to_dict()) == clock
    config = ChessGameConfig.from_match_start(FIXTURES["match_start"])
    assert config.rule_set_id == "chess-1.0"
    assert config.rule_set["window_ms"] == 1000
    assert ChessGameConfig.from_dict(config.to_dict()) == config
    with pytest.raises(ChessStateParseError):
        ChessGameConfig.from_match_start({"game_config": {"variant": "standard"}})
    with pytest.raises(ChessStateParseError):
        ChessGameConfig.from_match_start({})


def test_first_legal_move_reads_the_raw_message():
    request = _requests()[0]
    assert first_legal_move(request) == request["state"]["legal_moves"][0]
    assert first_legal_move({"state": {"legal_moves": ["x", 3]}}) is None
    assert first_legal_move({}) is None
    assert fallback_action({}) is None


# ---------------------------------------------------------------------------
# ChessBot
# ---------------------------------------------------------------------------


class Scripted(ChessBot):
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls = 0

    def choose_move(self, state):
        self.calls += 1
        return self.behaviour(state)


def test_decide_passes_a_legal_move_and_an_action():
    state = parse_chess_turn_request(_requests()[3])
    last = state.legal_moves[-1]
    assert Scripted(lambda s: last).decide(state) == Action.move(last)
    assert Scripted(lambda s: Action.move(last)).decide(state) == Action.move(last)


@pytest.mark.parametrize(
    "answer", ["a1a1", None, 42, Action.check(), Action(action="move", params={"move": "zz"})]
)
def test_decide_replaces_a_bad_answer_with_the_fallback(answer, caplog):
    state = parse_chess_turn_request(_requests()[3])
    with caplog.at_level(logging.ERROR, logger="chipzen"):
        action = Scripted(lambda s: answer).decide(state)
    assert action == Action.move(state.legal_moves[0])
    assert "not one of the" in caplog.text


def test_decide_refuses_a_poker_state():
    with pytest.raises(TypeError):
        Scripted(lambda s: "e2e4").decide(GameState())


def test_game_config_reaches_the_state():
    seen = []
    bot = Scripted(lambda s: seen.append(s.game_config) or s.legal_moves[0])
    bot.on_match_start(copy.deepcopy(FIXTURES["match_start"]))
    bot.decide(parse_chess_turn_request(_requests()[0]))
    assert seen[0] is not None and seen[0].rule_set_id == "chess-1.0"
    bot.on_reconnected({"game_config": {"broken": True}})
    assert bot.game_config is None
    bot.on_reconnected({})


def test_is_move_answer():
    state = parse_chess_turn_request(_requests()[0])
    assert is_move_answer(Action.move(state.legal_moves[0]), state)
    assert not is_move_answer(Action.check(), state)
    assert not is_move_answer("e2e4", state)


def test_chess_bot_is_exported():
    assert chipzen.ChessBot is ChessBot


# ---------------------------------------------------------------------------
# The session loop
# ---------------------------------------------------------------------------


class MockWebSocket:
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


def _hello(shape: str | None = "chess") -> dict:
    hello = {
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "chess",
    }
    if shape is not None:
        hello["game"] = {"game_type": "chess", "variant": "standard", "state_shape": shape}
    return hello


def _session(*frames: dict, shape: str | None = "chess") -> list[dict]:
    start = copy.deepcopy(FIXTURES["match_start"])
    start["match_id"] = MATCH_ID
    end = {"type": "match_end", "match_id": MATCH_ID, "reason": "complete", "results": []}
    return [_hello(shape), start, *frames, end]


async def _play(bot, messages, **kwargs):
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


def _moves_sent(ws) -> list[str]:
    return [m["params"]["move"] for m in ws.of_type("turn_action")]


async def test_session_plays_the_bots_move_and_declares_chess():
    request = _requests()[5]
    last = request["state"]["legal_moves"][-1]
    ws, end = await _play(Scripted(lambda s: last), _session(request))
    assert end is not None
    (hello,) = ws.of_type("hello")
    assert hello["supported_games"] == ["chess"]
    (action,) = ws.of_type("turn_action")
    assert action == {
        "type": "turn_action",
        "match_id": MATCH_ID,
        "request_id": request["request_id"],
        "action": "move",
        "params": {"move": last},
    }


async def test_session_sends_the_fallback_when_the_bot_is_too_slow():
    request = _with_clock(_requests()[5], window_ms=300, bank_ms=0)
    release = threading.Event()
    bot = Scripted(lambda s: release.wait(5) or s.legal_moves[-1])
    bot.send_margin_ms = 100
    started = time.monotonic()
    ws, _ = await _play(bot, _session(request, _with_clock(_requests()[6], 300, 0)))
    release.set()
    assert time.monotonic() - started < 2
    assert _moves_sent(ws) == [
        request["state"]["legal_moves"][0],
        _requests()[6]["state"]["legal_moves"][0],
    ]
    assert bot.calls == 1  # the second request got the fallback while the first still ran


async def test_session_falls_back_on_a_raise_and_on_a_parse_failure():
    good = _requests()[2]
    broken = _requests()[3]
    broken["state"].pop("fen")
    ws, _ = await _play(Scripted(lambda s: 1 / 0), _session(good, broken))
    assert _moves_sent(ws) == [
        good["state"]["legal_moves"][0],
        broken["state"]["legal_moves"][0],
    ]


async def test_session_without_safe_mode_raises():
    with pytest.raises(BotDecisionError):
        await _play(Scripted(lambda s: 1 / 0), _session(_requests()[2]), safe_mode=False)


async def test_session_uses_the_bots_fallback_move():
    request = _requests()[5]
    pick = request["state"]["legal_moves"][1]

    class OwnFallback(Scripted):
        def fallback_move(self, state):
            return pick

    ws, _ = await _play(OwnFallback(lambda s: 1 / 0), _session(request))
    assert _moves_sent(ws) == [pick]


async def test_session_retries_a_rejection_with_another_listed_move():
    request = _requests()[5]
    legal = request["state"]["legal_moves"]
    rejection = {
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "request_id": request["request_id"],
        "reason": "not legal",
        "message": "not legal",
        "details": {"code": "not_in_legal_set"},
        "submitted_action": "move",
        "remaining_ms": 900,
        "valid_actions": ["move"],
    }
    stale = {**rejection, "request_id": "req_unknown"}
    ws, _ = await _play(Scripted(lambda s: legal[0]), _session(request, rejection, stale))
    assert _moves_sent(ws) == [legal[0], legal[1]]


async def test_a_chess_bot_will_not_play_another_game():
    ws, end = await _play(Scripted(lambda s: "e2e4"), _session(shape="bigbadbots"))
    assert end is None
    assert ws.of_type("hello") == []


async def test_reconnected_pending_request_is_answered():
    request = _requests()[4]
    reconnected = {
        "type": "reconnected",
        "match_id": MATCH_ID,
        "round_number": 1,
        "pending_request": request,
        "game_config": FIXTURES["match_start"]["game_config"],
    }
    ws, _ = await _play(Scripted(lambda s: s.legal_moves[-1]), _session(reconnected))
    assert _moves_sent(ws) == [request["state"]["legal_moves"][-1]]


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _write_bot(tmp_path: Path, body: str, base: str = "ChessBot") -> Path:
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text(
        f"from chipzen import ChessBot\nfrom chipzen.chess_uci import UciBridgeBot\n\n"
        f"class MyBot({base}):\n{body}\n",
        encoding="utf-8",
    )
    return bot_dir


def _by_name(results):
    out: dict[str, list[tuple[str, str]]] = {}
    for severity, name, message in results:
        out.setdefault(name, []).append((severity, message))
    return out


def test_validate_passes_a_legal_bot(tmp_path):
    bot_dir = _write_bot(
        tmp_path, "    def choose_move(self, state):\n        return state.legal_moves[-1]"
    )
    results = _by_name(validate_bot(bot_dir))
    assert results["choose_move_method"][0][0] == "pass"
    assert results["chess_move"] == [
        ("pass", f"All {len(PAIRS)} answers are moves from legal_moves")
    ]
    assert all(sev != "fail" for entries in results.values() for sev, _ in entries)


def test_validate_fails_an_illegal_bot(tmp_path):
    bot_dir = _write_bot(tmp_path, "    def choose_move(self, state):\n        return 'a1a1'")
    results = _by_name(validate_bot(bot_dir))
    (entry,) = results["chess_move"]
    assert entry[0] == "fail" and "would be rejected" in entry[1]


def test_validate_fails_a_raising_and_a_mutating_bot(tmp_path):
    body = (
        "    def choose_move(self, state):\n"
        "        state.legal_moves.reverse()\n"
        "        if state.ply == 0:\n"
        "            raise RuntimeError('boom')\n"
        "        return state.legal_moves[0]"
    )
    results = _by_name(validate_bot(_write_bot(tmp_path, body)))
    assert any(sev == "fail" and "raised" in msg for sev, msg in results["chess_move"])
    assert results["chess_state_mutation"][0][0] == "fail"


def test_validate_fails_a_chess_bot_without_choose_move(tmp_path):
    results = _by_name(validate_bot(_write_bot(tmp_path, "    pass")))
    assert results["choose_move_method"][0][0] == "fail"


def test_validate_reports_a_slow_bot(tmp_path):
    fixtures = copy.deepcopy(FIXTURES)
    fixtures["pairs"] = [copy.deepcopy(PAIRS[0])]
    req = fixtures["pairs"][0]["request"]
    req["state"]["clock"]["window_ms"] = 1
    slow = Scripted(lambda s: time.sleep(0.02) or s.legal_moves[0])
    results = _by_name(run_chess_checks(slow, fixtures=fixtures))
    assert results["chess_timing"][0][0] == "warn"
    req["timeout_ms"] = 100
    results = _by_name(run_chess_checks(slow, fixtures=fixtures))
    assert results["chess_timing"][0][0] == "fail"


def test_validate_judges_a_plain_bot_through_decide():
    class Plain(chipzen.Bot):
        def decide(self, state):
            return Action.move(state.legal_moves[0])

    results = _by_name(run_chess_checks(Plain()))
    assert results["chess_move"][0][0] == "pass"


def test_validate_reports_a_broken_match_start():
    class Broken(Scripted):
        def on_match_start(self, match_info):
            raise SystemExit(2)

    results = _by_name(run_chess_checks(Broken(lambda s: s.legal_moves[0])))
    assert results["chess_match_start"][0][0] == "fail"


def test_validate_game_flag_runs_chess_for_a_plain_bot(tmp_path):
    bot_dir = tmp_path / "plain"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text(
        "from chipzen import Action, Bot\n\n"
        "class MyBot(Bot):\n"
        "    def decide(self, state):\n"
        "        return Action.move(state.legal_moves[0])\n",
        encoding="utf-8",
    )
    results = _by_name(validate_bot(bot_dir, game="chess"))
    assert results["chess_move"][0][0] == "pass"


# ---------------------------------------------------------------------------
# The starters and init
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["chess", "chess-uci"])
def test_init_writes_the_chess_starter_byte_for_byte(tmp_path, template):
    spec = scaffold.TEMPLATES[template]
    folder = PACKAGE_DIR / "starters" / spec.source
    on_disk = {
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert on_disk == set(spec.files)
    bot_dir = scaffold_bot("chess_bot", parent_dir=tmp_path, template=template)
    for name in spec.files:
        assert (bot_dir / name).read_bytes() == (folder / name).read_bytes(), name


def test_init_cli_template_flag(tmp_path, capsys):
    scaffold.init_cli(["cli_chess", "--dir", str(tmp_path), "--template", "chess"])
    assert (tmp_path / "cli_chess" / "bot.py").read_text("utf-8").count("class LegalMover")
    assert "choose_move()" in capsys.readouterr().out
    with pytest.raises(ValueError):
        scaffold_bot("x", parent_dir=tmp_path, template="go")


@pytest.mark.parametrize("folder", ["legal-mover", "uci-bridge"])
def test_starter_passes_validate(folder, monkeypatch):
    monkeypatch.delenv("CHIPZEN_UCI_ENGINE", raising=False)
    results = validate_bot(STARTERS / folder)
    failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
    assert failures == []
    assert any(name == "chess_move" and sev == "pass" for sev, name, _ in results)


def _load_starter(folder: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"starter_{folder}", STARTERS / folder / "bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legal_mover_prefers_the_biggest_capture():
    module = _load_starter("legal-mover")
    request = _requests()[0]
    request["state"].update(
        side="white",
        fen="4k3/8/8/3q1p2/4P3/8/8/4K3 w - - 0 1",
        legal_moves=["e1d1", "e1e2", "e4d5", "e4e5", "e4f5"],
    )
    bot = module.LegalMover()
    assert bot.choose_move(parse_chess_turn_request(request)) == "e4d5"


def test_legal_mover_sees_en_passant_and_promotion():
    module = _load_starter("legal-mover")
    bot = module.LegalMover()
    request = _requests()[0]
    request["state"].update(
        side="white",
        fen="4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1",
        legal_moves=["e1d1", "e5d6", "e5e6"],
    )
    assert bot.choose_move(parse_chess_turn_request(request)) == "e5d6"
    request["state"].update(
        side="black",
        fen="4k3/8/8/8/8/8/p7/4K3 b - - 0 1",
        legal_moves=["a2a1b", "a2a1n", "a2a1q", "a2a1r", "e8d8"],
    )
    assert bot.choose_move(parse_chess_turn_request(request)) == "a2a1q"


@pytest.mark.parametrize("pair", PAIRS[:12], ids=[p["request"]["request_id"] for p in PAIRS[:12]])
def test_legal_mover_always_answers_from_the_list(pair):
    module = _load_starter("legal-mover")
    state = parse_chess_turn_request(pair["request"])
    assert module.LegalMover().choose_move(state) in state.legal_moves


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


def test_chess_state_is_frozen():
    state = parse_chess_turn_request(_requests()[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.side = "black"  # type: ignore[misc]


def test_session_loop_handles_a_bot_slower_than_the_loop():
    # A sanity guard on the event-loop use: decide() runs off the loop.
    request = _requests()[1]
    bot = Scripted(lambda s: time.sleep(0.05) or s.legal_moves[0])
    ws, _ = asyncio.run(_play(bot, _session(request)))
    assert _moves_sent(ws) == [request["state"]["legal_moves"][0]]
