"""Tests for Fog of War (preview): models, FogOfWarBot, the session loop, validate, the starter.

The payloads come from two places: the examples in the platform's
``FOGOFWAR-GAME-STATE-PROTOCOL.md`` (copied verbatim below), and
``fixtures/fogofwar/move.json``, recorded from the platform's engine by
``scripts/extract_fogofwar_fixtures.py``. One test holds the two to each other.
"""

from __future__ import annotations

import ast
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
from chipzen.chess import REJECTION_CODES, ChessGameConfig, is_uci_move, parse_chess_turn_request
from chipzen.chess_bot import ChessBot, is_move_answer
from chipzen.chess_validate import judge_turn_action, load_fixtures, run_chess_checks
from chipzen.client import _run_session
from chipzen.fogofwar import (
    DIALECT,
    STATE_SHAPE,
    FogOfWarGameConfig,
    FogOfWarState,
    FogOfWarStateParseError,
    parse_fogofwar_turn_request,
)
from chipzen.fogofwar_bot import FogOfWarBot
from chipzen.models import Action, GameState, parse_turn_request
from chipzen.scaffold import scaffold_bot
from chipzen.validate import validate_bot

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STARTER = PACKAGE_DIR / "starters" / "fogofwar" / "legal-mover"
MATCH_ID = "fog-test-match"
FIXTURES = load_fixtures("fogofwar")
PAIRS = FIXTURES["pairs"]
CHESS_FIXTURES = load_fixtures("chess")

# ---------------------------------------------------------------------------
# The protocol document's examples, verbatim (FOGOFWAR-GAME-STATE-PROTOCOL.md)
# ---------------------------------------------------------------------------

#: Section 1.1: the ``game`` descriptor of the server ``hello``.
DOC_GAME = {
    "game_type": "fogofwar",
    "variant": "fogofwar",
    "actions": ["move"],
    "phases": ["move"],
    "state_shape": "fogofwar",
}

#: Section 3: ``match_start.game_config`` for a ``fogofwar-1.0`` match.
DOC_GAME_CONFIG = {
    "variant": "fogofwar",
    "rule_set_id": "fogofwar-1.0",
    "num_players": 2,
    "rule_set": {
        "rule_set_id": "fogofwar-1.0",
        "engine_line": "v1",
        "variant": "fog",
        "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "sides": "seat0_white",
        "ply_cap": 300,
        "threefold_repetition": "automatic",
        "fivefold_repetition": "off",
        "fifty_move": "automatic",
        "seventy_five_move": "off",
        "insufficient_material": "off",
        "timeout_vs_insufficient_material": "loss",
        "window_ms": 1000,
        "bank_ms": 60000,
        "illegal_answer": "reject",
        "clock_visibility": "public",
        "on_miss": "loss_on_time",
    },
    "engine_version": "1.0.0",
}

#: Section 4: the request to White on its second move of a recorded game.
DOC_TURN_REQUEST = {
    "type": "turn_request",
    "match_id": "7470f0a0-0000-4000-8000-000000000001",
    "seq": 3,
    "server_ts": "2026-11-17T12:00:00.000Z",
    "request_id": "req_7470f0a00002",
    "seat": 0,
    "timeout_ms": 61000,
    "valid_actions": ["move"],
    "state": {
        "phase": "move",
        "board": [],
        "your_hole_cards": [],
        "pot": 0,
        "to_call": 0,
        "min_raise": 0,
        "max_raise": 0,
        "your_stack": 0,
        "opponent_stacks": [0],
        "call": "move",
        "side": "white",
        "variant": "fog",
        "ply": 2,
        "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "legal_moves": [
            "a2a3",
            "a2a4",
            "b1a3",
            "b1c3",
            "b2b3",
            "b2b4",
            "c2c3",
            "c2c4",
            "d1e2",
            "d1f3",
            "d1g4",
            "d1h5",
            "d2d3",
            "d2d4",
            "e1e2",
            "e4e5",
            "e4f5",
            "f1a6",
            "f1b5",
            "f1c4",
            "f1d3",
            "f1e2",
            "f2f3",
            "f2f4",
            "g1e2",
            "g1f3",
            "g1h3",
            "g2g3",
            "g2g4",
            "h2h3",
            "h2h4",
        ],  # fmt: skip
        "clock": {"window_ms": 1000, "my_bank_ms": 60000, "opp_bank_ms": 60000},
        "my_moves": ["e2e4"],
        "visible": [
            "a1",
            "b1",
            "c1",
            "d1",
            "e1",
            "f1",
            "g1",
            "h1",
            "a2",
            "b2",
            "c2",
            "d2",
            "e2",
            "f2",
            "g2",
            "h2",
            "a3",
            "b3",
            "c3",
            "d3",
            "f3",
            "g3",
            "h3",
            "a4",
            "b4",
            "c4",
            "d4",
            "e4",
            "f4",
            "g4",
            "h4",
            "b5",
            "e5",
            "f5",
            "h5",
            "a6",
        ],  # fmt: skip
        "pieces": {
            "a1": "R",
            "b1": "N",
            "c1": "B",
            "d1": "Q",
            "e1": "K",
            "f1": "B",
            "g1": "N",
            "h1": "R",
            "a2": "P",
            "b2": "P",
            "c2": "P",
            "d2": "P",
            "f2": "P",
            "g2": "P",
            "h2": "P",
            "e4": "P",
            "f5": "p",
        },  # fmt: skip
        "castling": "KQ",
    },
}

#: Section 5: White's answer to the request above.
DOC_TURN_ACTION = {
    "type": "turn_action",
    "match_id": "7470f0a0-0000-4000-8000-000000000001",
    "request_id": "req_7470f0a00002",
    "action": "move",
    "params": {"move": "d1h5"},
}


def _requests() -> list[dict]:
    return [copy.deepcopy(p["request"]) for p in PAIRS]


def _request(source_part: str) -> dict:
    for pair in PAIRS:
        if source_part in pair["source"]:
            return copy.deepcopy(pair["request"])
    raise LookupError(source_part)


def _with_clock(request: dict, window_ms: int, bank_ms: int) -> dict:
    request = copy.deepcopy(request)
    request["state"]["clock"] = {"window_ms": window_ms, "my_bank_ms": bank_ms, "opp_bank_ms": 0}
    request["timeout_ms"] = window_ms + bank_ms
    return request


# ---------------------------------------------------------------------------
# The documented payloads
# ---------------------------------------------------------------------------


def test_doc_turn_request_parses():
    state = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    assert state.to_state() == DOC_TURN_REQUEST["state"]
    assert state.side == "white" and state.is_white and state.seat == 0
    assert state.variant == "fog" and state.ply == 2
    assert state.my_moves == ["e2e4"]
    assert state.castling == "KQ"
    assert state.timeout_ms == state.clock.budget_ms == 61000
    assert state.piece_at("f5") == "p" and state.piece_at("e5") is None
    assert state.is_visible("e5") and not state.is_visible("e7")
    assert state.seen_opponent_pieces == {"f5": "p"}
    assert len(state.my_pieces) == 16
    assert state.captured_by("e4f5") == "p"
    assert state.captured_by("d1h5") is None
    assert not hasattr(state, "fen") and not hasattr(state, "moves")


def test_doc_game_config_parses():
    config = FogOfWarGameConfig.from_dict(DOC_GAME_CONFIG)
    assert FogOfWarGameConfig is ChessGameConfig
    assert config.variant == DIALECT == "fogofwar"
    assert config.rule_set_id == "fogofwar-1.0"
    assert config.rule_set["variant"] == "fog"
    assert (config.window_ms, config.bank_ms, config.ply_cap) == (1000, 60000, 300)
    assert config.engine_version == "1.0.0"
    assert config.to_dict() == DOC_GAME_CONFIG


def test_doc_turn_action_is_what_action_move_sends():
    state = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    action = Action.move(DOC_TURN_ACTION["params"]["move"])
    assert action.to_wire() == {"action": "move", "params": {"move": "d1h5"}}
    assert is_move_answer(action, state)
    verdict, code = judge_turn_action(DOC_TURN_REQUEST, json.dumps(DOC_TURN_ACTION))
    assert (verdict, code) == ("ok", None)


def test_doc_example_is_the_recorded_fixture():
    """The fixtures render requests exactly as the document's real message does."""
    fixture = _request("fogofwar-quick-capture")
    for pair in PAIRS:
        state = pair["request"]["state"]
        if pair["source"] == "fogofwar-quick-capture" and state["ply"] == 2:
            fixture = pair["request"]
    assert fixture["state"] == DOC_TURN_REQUEST["state"]
    assert fixture["seat"] == DOC_TURN_REQUEST["seat"]
    assert fixture["timeout_ms"] == DOC_TURN_REQUEST["timeout_ms"]
    assert fixture["valid_actions"] == DOC_TURN_REQUEST["valid_actions"]
    assert FIXTURES["match_start"]["game_config"] == DOC_GAME_CONFIG


def test_doc_game_descriptor_shape():
    assert DOC_GAME["state_shape"] == STATE_SHAPE == "fogofwar"


# ---------------------------------------------------------------------------
# No engine, no chess library
# ---------------------------------------------------------------------------


def test_starter_imports_no_chess_library():
    tree = ast.parse((STARTER / "bot.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            assert name.split(".")[0] not in {"chess", "chess_engine", "stockfish"}, name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_fixtures_cover_both_sides_and_the_special_moves():
    assert {p["request"]["state"]["side"] for p in PAIRS} == {"white", "black"}
    sources = " ".join(p["source"] for p in PAIRS)
    for kind in ("king_capture", "promotion", "en_passant", "castling"):
        assert kind in sources
    codes = {a.get("code") for p in PAIRS for a in p["answers"]}
    assert codes - {None} == set(REJECTION_CODES)
    plies = [p["request"]["state"]["ply"] for p in PAIRS]
    assert min(plies) == 0 and max(plies) >= 290
    assert FIXTURES["rule_set_id"] == "fogofwar-1.0"


@pytest.mark.parametrize("pair", PAIRS, ids=[p["request"]["request_id"] for p in PAIRS])
def test_every_fixture_request_parses_and_round_trips(pair):
    request = pair["request"]
    state = parse_fogofwar_turn_request(request)
    assert state.to_state() == request["state"]
    assert "fen" not in request["state"] and "moves" not in request["state"]
    assert state.valid_actions == ["move"]
    assert state.timeout_ms == state.clock.budget_ms
    assert state.legal_moves == sorted(state.legal_moves)
    assert all(is_uci_move(m) for m in state.legal_moves)
    assert len(state.my_moves) == state.ply // 2  # own moves only, never the opponent's
    assert set(state.pieces) <= set(state.visible)
    # Every legal move starts on one of the seat's own pieces, which it sees.
    assert all(state.is_mine(state.pieces[m[:2]]) for m in state.legal_moves)


@pytest.mark.parametrize("pair", PAIRS, ids=[p["request"]["request_id"] for p in PAIRS])
def test_judge_matches_every_engine_label(pair):
    for answer in pair["answers"]:
        verdict, code = judge_turn_action(pair["request"], answer["answer"])
        assert verdict == answer["expect"], answer
        assert code == answer.get("code"), answer


def _drop(key):
    def mutate(r):
        del r["state"][key]

    return mutate


def _set(key, value):
    def mutate(r):
        r["state"][key] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_drop("visible"), "visible"),
        (_drop("pieces"), "pieces"),
        (_drop("my_moves"), "my_moves"),
        (_drop("castling"), "castling"),
        (_drop("legal_moves"), "legal_moves"),
        (_set("variant", "standard"), "not supported"),
        (_set("side", "red"), "side"),
        (_set("pieces", {"e4": "X"}), "pieces"),
        (_set("pieces", {"z9": "P"}), "pieces"),
        (_set("pieces", {"e4": 1}), "pieces"),
        (_set("visible", "e4"), "visible"),
        (_set("clock", {"window_ms": 1000}), "my_bank_ms"),
    ],
)
def test_parse_is_strict(mutate, message):
    request = copy.deepcopy(DOC_TURN_REQUEST)
    mutate(request)
    with pytest.raises(FogOfWarStateParseError, match=message):
        parse_fogofwar_turn_request(request)


def test_chess_and_fog_requests_do_not_cross_parse():
    with pytest.raises(FogOfWarStateParseError):
        parse_fogofwar_turn_request(CHESS_FIXTURES["pairs"][0]["request"])
    with pytest.raises(chipzen.chess.ChessStateParseError):
        parse_chess_turn_request(DOC_TURN_REQUEST)


def test_parse_turn_request_routes_fogofwar():
    state = parse_turn_request(DOC_TURN_REQUEST, state_shape="fogofwar")
    assert isinstance(state, FogOfWarState)


def test_fog_state_is_frozen():
    state = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.ply = 3  # type: ignore[misc]


def test_own_moves_left_needs_the_ply_cap():
    state = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    assert state.own_moves_left is None
    config = FogOfWarGameConfig.from_dict(DOC_GAME_CONFIG)
    assert dataclasses.replace(state, game_config=config).own_moves_left == 149


# ---------------------------------------------------------------------------
# FogOfWarBot
# ---------------------------------------------------------------------------


class Scripted(FogOfWarBot):
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls = 0

    def choose_move(self, state):
        self.calls += 1
        return self.behaviour(state)


class ChessScripted(ChessBot):
    def choose_move(self, state):
        return state.legal_moves[0]


def test_fog_bot_is_exported():
    assert chipzen.FogOfWarBot is FogOfWarBot
    assert FogOfWarBot.game_type == "fogofwar" and ChessBot.game_type == "chess"
    assert not issubclass(FogOfWarBot, ChessBot)


def test_decide_passes_a_legal_move_and_an_action():
    state = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    assert Scripted(lambda s: "d1h5").decide(state) == Action.move("d1h5")
    assert Scripted(lambda s: Action.move("e4f5")).decide(state) == Action.move("e4f5")


@pytest.mark.parametrize("answer", ["e1g1", None, 42, Action.check()])
def test_decide_replaces_a_bad_answer_with_the_fallback(answer, caplog):
    state = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    with caplog.at_level(logging.ERROR, logger="chipzen"):
        action = Scripted(lambda s: answer).decide(state)
    assert action == Action.move(state.legal_moves[0])
    assert "fogofwar request" in caplog.text and "not one of the" in caplog.text


def test_each_bot_refuses_the_other_games_state():
    fog = parse_fogofwar_turn_request(DOC_TURN_REQUEST)
    chess = parse_chess_turn_request(CHESS_FIXTURES["pairs"][0]["request"])
    with pytest.raises(TypeError, match="only plays fogofwar"):
        Scripted(lambda s: "e2e4").decide(chess)
    with pytest.raises(TypeError, match="only plays chess"):
        ChessScripted().decide(fog)
    with pytest.raises(TypeError):
        Scripted(lambda s: "e2e4").decide(GameState())


def test_game_config_reaches_the_state():
    seen = []
    bot = Scripted(lambda s: seen.append(s.game_config) or s.legal_moves[0])
    bot.on_match_start({"type": "match_start", "game_config": copy.deepcopy(DOC_GAME_CONFIG)})
    bot.decide(parse_fogofwar_turn_request(DOC_TURN_REQUEST))
    assert seen[0] is not None and seen[0].rule_set_id == "fogofwar-1.0"


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


def _hello(game: dict) -> dict:
    return {
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": game["game_type"],
        "game": game,
    }


def _session(*frames: dict, game: dict = DOC_GAME) -> list[dict]:
    start = {"type": "match_start", "match_id": MATCH_ID, "game_config": DOC_GAME_CONFIG}
    end = {"type": "match_end", "match_id": MATCH_ID, "reason": "complete", "results": []}
    return [_hello(game), start, *frames, end]


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


def _rejection(request: dict, remaining_ms: int = 900, code: str = "not_in_legal_set") -> dict:
    return {
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "request_id": request["request_id"],
        "reason": f"answer rejected: {code}",
        "message": f"answer rejected: {code}",
        "details": {"code": code},
        "submitted_action": "move",
        "remaining_ms": remaining_ms,
        "valid_actions": ["move"],
    }


async def test_session_plays_the_bots_move_and_declares_fogofwar():
    ws, end = await _play(Scripted(lambda s: "d1h5"), _session(DOC_TURN_REQUEST))
    assert end is not None
    (hello,) = ws.of_type("hello")
    assert hello["supported_games"] == ["fogofwar"]
    (action,) = ws.of_type("turn_action")
    assert action == {**DOC_TURN_ACTION, "match_id": MATCH_ID}


async def test_session_sends_the_fallback_when_the_bot_is_too_slow():
    first = _with_clock(_requests()[2], window_ms=300, bank_ms=0)
    second = _with_clock(_requests()[4], window_ms=300, bank_ms=0)
    release = threading.Event()
    bot = Scripted(lambda s: release.wait(5) or s.legal_moves[-1])
    bot.send_margin_ms = 100
    started = time.monotonic()
    ws, _ = await _play(bot, _session(first, second))
    release.set()
    assert time.monotonic() - started < 2
    assert _moves_sent(ws) == [
        first["state"]["legal_moves"][0],
        second["state"]["legal_moves"][0],
    ]


async def test_session_falls_back_on_a_raise_and_on_a_parse_failure():
    good = _requests()[2]
    broken = _requests()[4]
    broken["state"].pop("visible")
    ws, _ = await _play(Scripted(lambda s: 1 / 0), _session(good, broken))
    assert _moves_sent(ws) == [
        good["state"]["legal_moves"][0],
        broken["state"]["legal_moves"][0],
    ]


async def test_session_retries_a_rejection_up_to_three_answers():
    request = _requests()[2]
    legal = request["state"]["legal_moves"]
    frames = [request, _rejection(request, 800), _rejection(request, 700), _rejection(request, 600)]
    ws, _ = await _play(Scripted(lambda s: legal[0]), _session(*frames))
    assert _moves_sent(ws) == [legal[0], legal[1], legal[2]]


async def test_session_retries_with_the_bots_fallback_move_first():
    request = _requests()[2]
    legal = request["state"]["legal_moves"]

    class OwnFallback(Scripted):
        def fallback_move(self, state):
            return legal[-1]

    ws, _ = await _play(OwnFallback(lambda s: legal[2]), _session(request, _rejection(request)))
    assert _moves_sent(ws) == [legal[2], legal[-1]]


async def test_a_fog_bot_will_not_play_chess_and_a_chess_bot_will_not_play_fog():
    chess_game = {**DOC_GAME, "game_type": "chess", "variant": "chess", "state_shape": "chess"}
    ws, end = await _play(Scripted(lambda s: "e2e4"), _session(game=chess_game))
    assert end is None and ws.of_type("hello") == []
    ws, end = await _play(ChessScripted(), _session())
    assert end is None and ws.of_type("hello") == []


async def test_a_plain_bot_can_play_fog_when_it_declares_it():
    class Plain(chipzen.Bot):
        def decide(self, state):
            assert isinstance(state, FogOfWarState)
            return Action.move(state.legal_moves[-1])

    ws, _ = await _play(Plain(), _session(DOC_TURN_REQUEST), supported_games=["fogofwar"])
    assert ws.of_type("hello")[0]["supported_games"] == ["fogofwar"]
    assert _moves_sent(ws) == [DOC_TURN_REQUEST["state"]["legal_moves"][-1]]


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _write_bot(tmp_path: Path, body: str) -> Path:
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text(
        f"from chipzen import FogOfWarBot\n\nclass MyBot(FogOfWarBot):\n{body}\n",
        encoding="utf-8",
    )
    return bot_dir


def _by_name(results):
    out: dict[str, list[tuple[str, str]]] = {}
    for severity, name, message in results:
        out.setdefault(name, []).append((severity, message))
    return out


def test_validate_passes_a_legal_fog_bot(tmp_path):
    bot_dir = _write_bot(
        tmp_path, "    def choose_move(self, state):\n        return state.legal_moves[-1]"
    )
    results = _by_name(validate_bot(bot_dir))
    assert results["choose_move_method"][0][0] == "pass"
    assert results["fogofwar_move"] == [
        ("pass", f"All {len(PAIRS)} answers are moves from legal_moves")
    ]
    assert "chess_move" not in results
    assert all(sev != "fail" for entries in results.values() for sev, _ in entries)


def test_validate_fails_an_illegal_and_a_mutating_fog_bot(tmp_path):
    body = "    def choose_move(self, state):\n        state.pieces.clear()\n        return 'a1a1'"
    results = _by_name(validate_bot(_write_bot(tmp_path, body)))
    assert results["fogofwar_move"][0][0] == "fail"
    assert results["fogofwar_state_mutation"][0][0] == "fail"


def test_validate_fails_a_fog_bot_without_choose_move(tmp_path):
    results = _by_name(validate_bot(_write_bot(tmp_path, "    pass")))
    assert results["choose_move_method"][0][0] == "fail"


def test_validate_game_flag_runs_fogofwar_for_a_plain_bot(tmp_path):
    bot_dir = tmp_path / "plain"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text(
        "from chipzen import Action, Bot\n\n"
        "class MyBot(Bot):\n"
        "    def decide(self, state):\n"
        "        return Action.move(state.legal_moves[0])\n",
        encoding="utf-8",
    )
    results = _by_name(validate_bot(bot_dir, game="fogofwar"))
    assert results["fogofwar_move"][0][0] == "pass"


def test_run_checks_reports_a_broken_match_start():
    class Broken(Scripted):
        def on_match_start(self, match_info):
            raise RuntimeError("boom")

    results = _by_name(run_chess_checks(Broken(lambda s: s.legal_moves[0]), game="fogofwar"))
    assert results["fogofwar_match_start"][0][0] == "fail"


# ---------------------------------------------------------------------------
# The starter and init
# ---------------------------------------------------------------------------


def test_init_writes_the_fog_starter_byte_for_byte(tmp_path):
    spec = scaffold.TEMPLATES["fogofwar"]
    folder = PACKAGE_DIR / "starters" / spec.source
    assert folder == STARTER
    on_disk = {
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert on_disk == set(spec.files)
    bot_dir = scaffold_bot("fog_bot", parent_dir=tmp_path, template="fogofwar")
    for name in spec.files:
        assert (bot_dir / name).read_bytes() == (folder / name).read_bytes(), name


def test_wheel_mapping_carries_the_fog_starter():
    text = (PACKAGE_DIR / "pyproject.toml").read_text("utf-8")
    spec = scaffold.TEMPLATES["fogofwar"]
    for name in spec.files:
        line = f'"starters/{spec.source}/{name}" = "chipzen/{spec.packaged}/{name}"'
        assert line in text, line


def test_init_cli_fog_template(tmp_path, capsys):
    scaffold.init_cli(["cli_fog", "--dir", str(tmp_path), "--template", "fogofwar"])
    assert "class FogLegalMover" in (tmp_path / "cli_fog" / "bot.py").read_text("utf-8")
    assert "choose_move()" in capsys.readouterr().out


def test_starter_passes_validate():
    results = validate_bot(STARTER)
    failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
    assert failures == []
    assert any(name == "fogofwar_move" and sev == "pass" for sev, name, _ in results)


def _load_starter():
    import importlib.util

    spec = importlib.util.spec_from_file_location("starter_fog_legal_mover", STARTER / "bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_starter_takes_the_king_when_it_sees_it():
    bot = _load_starter().FogLegalMover()
    request = _request("king_capture")
    state = parse_fogofwar_turn_request(request)
    move = bot.choose_move(state)
    assert state.captured_by(move) == ("k" if state.is_white else "K")


def test_starter_takes_the_biggest_visible_capture_and_promotes():
    bot = _load_starter().FogLegalMover()
    assert bot.choose_move(parse_fogofwar_turn_request(DOC_TURN_REQUEST)) == "e4f5"
    state = parse_fogofwar_turn_request(_request("promotion"))
    move = bot.choose_move(state)
    assert state.captured_by(move) is not None or move.endswith("q")


@pytest.mark.parametrize("pair", PAIRS, ids=[p["request"]["request_id"] for p in PAIRS])
def test_starter_always_answers_from_the_list(pair):
    bot = _load_starter().FogLegalMover()
    state = parse_fogofwar_turn_request(pair["request"])
    assert bot.choose_move(state) in state.legal_moves
