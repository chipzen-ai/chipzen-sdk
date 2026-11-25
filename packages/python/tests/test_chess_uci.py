"""Tests for the UCI bridge (preview), against a tiny fake UCI engine written in Python."""

from __future__ import annotations

import copy
import dataclasses
import sys
import time
from pathlib import Path

import pytest

from chipzen.chess import ChessGameConfig, parse_chess_turn_request
from chipzen.chess_uci import ENGINE_ENV, UciBridgeBot, UciEngine, UciEngineError
from chipzen.chess_validate import load_fixtures

FAKE = Path(__file__).resolve().parent / "fake_uci_engine.py"
FIXTURES = load_fixtures()


def _state(index: int = 5, window_ms: int = 1000, bank_ms: int = 60000):
    request = copy.deepcopy(FIXTURES["pairs"][index]["request"])
    request["state"]["clock"] = {
        "window_ms": window_ms,
        "my_bank_ms": bank_ms,
        "opp_bank_ms": bank_ms,
    }
    request["timeout_ms"] = window_ms + bank_ms
    return parse_chess_turn_request(request)


def _bridge(mode: str, bestmove: str, log: Path | None = None, **settings) -> UciBridgeBot:
    args = [str(FAKE), mode, bestmove] + ([str(log)] if log else [])

    attrs = {
        "engine_path": sys.executable,
        "engine_args": tuple(args),
        "uci_options": {"Hash": 16, "Ponder": False},
        "send_margin_ms": 50,
        "engine_margin_ms": 50,
        "startup_timeout_ms": 5000,
        **settings,
    }
    cls = type("FakeBridge", (UciBridgeBot,), attrs)
    return cls()


@pytest.fixture(autouse=True)
def _no_env_engine(monkeypatch):
    monkeypatch.delenv(ENGINE_ENV, raising=False)


def _not_first(state) -> str:
    """A legal move the fallback would not play, so a test can tell them apart."""
    return state.legal_moves[-1]


def test_plays_the_engines_bestmove_and_speaks_uci(tmp_path):
    state = _state()
    log = tmp_path / "uci.log"
    bot = _bridge("good", _not_first(state), log)
    try:
        bot.on_match_start(copy.deepcopy(FIXTURES["match_start"]))
        assert bot.decide(state).params["move"] == _not_first(state)
    finally:
        bot.on_match_end({})
    commands = log.read_text("utf-8").splitlines()
    assert commands[0] == "uci"
    assert "setoption name Hash value 16" in commands
    assert "setoption name Ponder value false" in commands
    assert "ucinewgame" in commands
    position = f"position fen {state.start_fen}"
    if state.moves:
        position += " moves " + " ".join(state.moves)
    assert position in commands
    go = [c for c in commands if c.startswith("go ")]
    assert go == [f"go movetime {bot.movetime_ms(state)}"]
    assert commands[-1] == "quit"


def test_movetime_spends_the_window_and_a_share_of_the_bank():
    bot = _bridge("good", "e2e4")
    assert bot.movetime_ms(_state(window_ms=1000, bank_ms=60000)) == 1000 + 2000 - 100
    assert bot.movetime_ms(_state(window_ms=1000, bank_ms=0)) == 900
    assert bot.movetime_ms(_state(window_ms=50, bank_ms=0)) == bot.min_movetime_ms


def test_movetime_shares_the_bank_over_the_moves_left_before_the_ply_cap():
    bot = _bridge("good", "e2e4")
    config = ChessGameConfig.from_match_start(FIXTURES["match_start"])
    assert config.ply_cap == 300
    state = dataclasses.replace(_state(window_ms=1000, bank_ms=60000), game_config=config)
    # Far from the cap: the usual moves_to_go (30).
    early = dataclasses.replace(state, ply=10)
    assert early.own_moves_left == 145
    assert bot.movetime_ms(early) == 1000 + 60000 // 30 - 100
    # Ten own moves left before the draw at ply 300: a tenth of the bank.
    late = dataclasses.replace(state, ply=280)
    assert late.own_moves_left == 10
    assert bot.movetime_ms(late) == 1000 + 60000 // 10 - 100
    # The last move before the cap may spend the whole bank, less the margins.
    last = dataclasses.replace(state, ply=299)
    assert last.own_moves_left == 1
    assert bot.movetime_ms(last) == 1000 + 60000 - 100
    # No game_config: the ply cap is unknown, so moves_to_go alone.
    assert bot.movetime_ms(dataclasses.replace(state, game_config=None, ply=299)) == 2900


@pytest.mark.parametrize("mode", ["illegal", "none", "crash"])
def test_a_bad_engine_answer_gets_the_fallback(mode):
    state = _state()
    bot = _bridge(mode, _not_first(state))
    try:
        assert bot.choose_move(state) == state.legal_moves[0]
    finally:
        bot.close_engine()


def test_a_slow_engine_is_stopped_and_its_answer_used():
    state = _state(window_ms=300, bank_ms=0)
    bot = _bridge("slow", _not_first(state))
    try:
        started = time.monotonic()
        assert bot.choose_move(state) == _not_first(state)
        assert time.monotonic() - started < 0.3
    finally:
        bot.close_engine()


def test_a_late_bestmove_is_dropped_before_the_next_search():
    first = _state(index=5, window_ms=300, bank_ms=0)
    second = _state(index=6, window_ms=2000, bank_ms=0)
    bot = _bridge("late", _not_first(second))
    try:
        assert bot.choose_move(first) == first.legal_moves[0]  # timed out: fallback
        # The engine prints its late "bestmove h7h8" now; the bridge must not
        # take it as the answer to the next request.
        assert bot.choose_move(second) == _not_first(second)
    finally:
        bot.close_engine()


def test_a_crashed_engine_is_restarted(tmp_path):
    state = _state()
    bot = _bridge("crash", _not_first(state))
    try:
        assert bot.choose_move(state) == state.legal_moves[0]
        type(bot).engine_args = (str(FAKE), "good", _not_first(state))
        assert bot.choose_move(state) == _not_first(state)
    finally:
        bot.close_engine()


def test_an_engine_that_never_says_uciok_gets_the_fallback():
    state = _state()
    bot = _bridge("mute", _not_first(state), startup_timeout_ms=200)
    assert bot.choose_move(state) == state.legal_moves[0]


def test_a_missing_engine_gets_the_fallback(tmp_path):
    state = _state()
    bot = _bridge("good", "e2e4", engine_path=str(tmp_path / "no-such-engine"), engine_args=())
    assert bot.choose_move(state) == state.legal_moves[0]
    unconfigured = type("Bare", (UciBridgeBot,), {})()
    with pytest.raises(UciEngineError, match=ENGINE_ENV):
        unconfigured.engine_command()
    assert unconfigured.choose_move(state) == state.legal_moves[0]


def test_the_environment_names_the_engine(monkeypatch):
    monkeypatch.setenv(ENGINE_ENV, "/opt/engine")
    bot = _bridge("good", "e2e4", engine_args=("--x",))
    assert bot.engine_command() == ["/opt/engine", "--x"]


def test_match_start_warm_up_runs_off_the_caller():
    state = _state()
    bot = _bridge("good", _not_first(state))
    try:
        started = time.monotonic()
        bot.on_match_start(copy.deepcopy(FIXTURES["match_start"]))
        assert time.monotonic() - started < 0.2
        assert bot.choose_move(state) == _not_first(state)
    finally:
        bot.close_engine()


def test_start_engine_before_connecting(tmp_path):
    state = _state()
    log = tmp_path / "uci.log"
    bot = _bridge("good", _not_first(state), log)
    try:
        assert bot.start_engine() is True
        commands = log.read_text("utf-8").splitlines()
        assert commands[0] == "uci" and commands[-1] == "isready"
        assert bot.choose_move(state) == _not_first(state)
    finally:
        bot.close_engine()


def test_start_engine_reports_a_missing_engine(tmp_path):
    bot = _bridge("good", "e2e4", engine_path=str(tmp_path / "missing"), engine_args=())
    assert bot.start_engine() is False


def test_engine_read_until_handles_end_of_output():
    engine = UciEngine([sys.executable, "-c", "print('id name x')"])
    engine.start()
    assert engine.read_until("uciok", time.monotonic() + 2) is None
    assert engine.read_until("uciok", time.monotonic() + 0.1) is None
    engine.send("uci")  # the process may already be gone; must not raise
    engine.close()
    engine.close()
    assert not engine.alive
