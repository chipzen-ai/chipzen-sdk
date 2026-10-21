"""Tests for ``chipzen validate`` on Big Bad Bots bots (chipzen-sdk#143).

Three parts:

* the conformance fixtures: real ``turn_request`` / ``turn_action`` pairs,
  wire messages only, and the SDK's answer checker agrees with the label the
  reference engine gave every one of them;
* one test per malformed-answer class the engine hardening issues F0-04 to
  F0-07 found (fit shapes, non-finite and non-integer bids, a bot that mutates
  its state, a bot that exits or crashes every tick), each named after it;
* the three starter bots pass ``validate``, and poker bots are validated
  exactly as before.
"""

import json
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen import Action, BigBadBotsBot
from chipzen.bigbadbots import FIGHT_ACTIONS
from chipzen.bigbadbots_validate import (
    CALLS,
    FIXTURE_DIR,
    MAX_BOT_MESSAGE_BYTES,
    answer_text,
    check_answer,
    check_turn_action,
    load_fixtures,
    run_bigbadbots_checks,
)
from chipzen.validate import validate_bot, validate_cli

STARTERS_DIR = Path(__file__).parent.parent / "starters" / "bigbadbots"
STARTERS = ("rusher", "zoner", "turtle")
PAIR_KEYS = {"source", "request", "answer", "expect"}


def _request(call: str) -> dict:
    """The first fixture request of ``call`` whose example answer is ok."""
    return next(p["request"] for p in load_fixtures(call) if p["expect"] == "ok")


def _judge(call: str, params=None, action: str | None = None, **request_overrides):
    request = _request(call)
    request.update(request_overrides)
    message = {
        "type": "turn_action",
        "match_id": request["match_id"],
        "request_id": request["request_id"],
        "action": action or call,
    }
    if params is not None:
        message["params"] = params
    return check_turn_action(request, json.dumps(message))


class _Idle(BigBadBotsBot):
    def act(self, state):
        return Action.act("idle")


def _run(**handlers):
    """Run the fixture checks on a bot that idles, with ``handlers`` patched in."""
    bot_class = type("PatchedBot", (_Idle,), handlers)
    return run_bigbadbots_checks(bot_class())


def _entries(results, name):
    return [(sev, msg) for sev, n, msg in results if n == name]


def _fails(results, name) -> bool:
    return any(sev == "fail" for sev, _ in _entries(results, name))


def _no_fails(results):
    return [(n, m) for sev, n, m in results if sev == "fail"] == []


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


def test_fixtures_cover_all_four_calls():
    for call in CALLS:
        pairs = load_fixtures(call)
        assert pairs, f"no fixtures for {call}"
        assert any(p["expect"] == "ok" for p in pairs)
    assert any(p["expect"] == "malformed" for p in load_fixtures())


def test_fixtures_hold_wire_messages_only():
    """Each pair is a turn_request and a turn_action, nothing else (the SDK is public)."""
    assert sorted(p.name for p in FIXTURE_DIR.iterdir()) == sorted(
        [f"{call}.json" for call in CALLS] + ["README.md"]
    )
    for pair in load_fixtures():
        assert set(pair) == PAIR_KEYS
        assert pair["expect"] in ("ok", "corrected", "malformed")
        request = pair["request"]
        assert request["type"] == "turn_request"
        assert set(request) == {
            "type",
            "match_id",
            "seq",
            "server_ts",
            "seat",
            "request_id",
            "timeout_ms",
            "valid_actions",
            "state",
        }
        assert pair["answer"].startswith('{"type": "turn_action", ')


@pytest.mark.parametrize(
    "pair",
    load_fixtures(),
    ids=lambda p: f"{p['request']['request_id']}-{p['expect']}",
)
def test_checker_agrees_with_the_reference_label(pair):
    verdict = check_turn_action(pair["request"], pair["answer"])
    assert verdict.verdict == pair["expect"], verdict.reason


def test_load_fixtures_returns_copies():
    first = load_fixtures("bid")
    first[0]["request"]["state"]["me"]["credits"] = -1
    assert load_fixtures("bid")[0]["request"]["state"]["me"]["credits"] != -1


def test_the_sdk_factories_build_answers_the_checker_accepts():
    request = _request("choose_body")
    body = request["state"]["offer"][0]["id"]
    assert check_answer(request, Action.choose_body(body)).verdict == "ok"
    assert check_answer(_request("bid"), Action.bid(0)).verdict == "ok"
    assert check_answer(_request("fit"), Action.fit({})).verdict == "ok"
    for move in FIGHT_ACTIONS:
        assert check_answer(_request("act"), Action.act(move)).verdict == "ok"


# ---------------------------------------------------------------------------
# Corrected (well-shaped, not legal) and other malformed answers, section 3.7
# ---------------------------------------------------------------------------


def test_a_body_not_on_the_offer_is_corrected():
    assert _judge("choose_body", {"body": "not_offered"}).verdict == "corrected"


def test_a_fit_entry_that_does_not_fit_is_corrected():
    request = _request("fit")
    owned = request["state"]["owned"][0]
    slots = request["state"]["slots"]
    wrong_slot = next(i for i, s in enumerate(slots) if s != owned["slot"])
    for fit in (
        {"0": "not_owned"},
        {str(len(slots)): owned["id"]},
        {str(wrong_slot): owned["id"]},
    ):
        assert _judge("fit", {"fit": fit}).verdict == "corrected", fit


@pytest.mark.parametrize(
    ("call", "params", "action"),
    [
        ("bid", {"amount": 1}, "choose_body"),  # not one of valid_actions
        ("act", {}, "dance"),
        ("act", {"move": "idle"}, "idle"),  # act takes no params
        ("bid", {"amount": 1, "extra": 1}, None),  # params admits no other keys
        ("bid", None, None),  # params is required
        ("choose_body", {"body": 7}, None),
        ("fit", {"fit": {"00": "x"}}, None),
    ],
)
def test_malformed_answers_are_rejected(call, params, action):
    assert _judge(call, params, action).verdict == "malformed"


def test_an_oversized_answer_is_malformed():
    request = _request("choose_body")
    huge = Action(action="choose_body", params={"body": "a" * MAX_BOT_MESSAGE_BYTES})
    verdict = check_answer(request, huge)
    assert verdict.verdict == "malformed"
    assert "4096" in verdict.reason


def test_an_answer_that_is_not_an_action_is_malformed():
    assert check_answer(_request("act"), "idle").verdict == "malformed"
    assert check_answer(_request("act"), None).verdict == "malformed"


def test_answer_text_is_what_the_client_sends():
    request = _request("act")
    assert json.loads(answer_text(request, Action.act("block"))) == {
        "type": "turn_action",
        "match_id": request["match_id"],
        "request_id": request["request_id"],
        "action": "block",
        "params": {},
    }


# ---------------------------------------------------------------------------
# F0-04 (fighter-mvp#19): fit() returning a list, None, a string or a nested
# object. Each class in both forms a bot can produce it: the raw value
# returned from fit(), and an Action carrying it in params.
# ---------------------------------------------------------------------------


def _fit_forms(value):
    return (
        lambda self, state: value,
        lambda self, state: Action(action="fit", params={"fit": value}),
    )


@pytest.mark.parametrize("form", [0, 1])
def test_f0_04_fit_returns_a_list(form):
    assert _fails(_run(fit=_fit_forms([])[form]), "bigbadbots_fit")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_04_fit_returns_none(form):
    assert _fails(_run(fit=_fit_forms(None)[form]), "bigbadbots_fit")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_04_fit_returns_a_string(form):
    assert _fails(_run(fit=_fit_forms("0:piston")[form]), "bigbadbots_fit")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_04_fit_returns_a_nested_object(form):
    nested = {"0": {"id": "piston", "slot": "legs"}}
    assert _fails(_run(fit=_fit_forms(nested)[form]), "bigbadbots_fit")


def test_f0_04_fit_with_a_non_slot_key_is_malformed():
    for fit in ({"(0,)": "piston"}, {"1.5": "piston"}, {"9" * 30: "piston"}):
        assert _judge("fit", {"fit": fit}).verdict == "malformed", fit


# ---------------------------------------------------------------------------
# F0-05 (fighter-mvp#20): inf, -inf, NaN, bool and str bids are malformed (the
# server applies a 0 bid); a finite out-of-range integer is clamped, with a
# violation. Nothing escapes validate. Both forms: an Action carrying the value
# (it goes out as the client serialises it) and the Action.bid factory (which
# raises on a non-int, so the handler crashes).
# ---------------------------------------------------------------------------


def _bid_forms(value):
    return (
        lambda self, state: Action(action="bid", params={"amount": value}),
        lambda self, state: Action.bid(value),
    )


@pytest.mark.parametrize("form", [0, 1])
def test_f0_05_bid_of_positive_infinity(form):
    assert _fails(_run(bid=_bid_forms(float("inf"))[form]), "bigbadbots_bid")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_05_bid_of_negative_infinity(form):
    assert _fails(_run(bid=_bid_forms(float("-inf"))[form]), "bigbadbots_bid")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_05_bid_of_nan(form):
    assert _fails(_run(bid=_bid_forms(float("nan"))[form]), "bigbadbots_bid")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_05_bid_of_a_bool(form):
    assert _fails(_run(bid=_bid_forms(True)[form]), "bigbadbots_bid")


@pytest.mark.parametrize("form", [0, 1])
def test_f0_05_bid_of_a_string(form):
    assert _fails(_run(bid=_bid_forms("100")[form]), "bigbadbots_bid")


def test_f0_05_bid_of_a_non_integer_number():
    # 12.0 and 1e308 are finite, but not JSON integers.
    for value in (12.0, 12.7, 1e308):
        assert _fails(_run(bid=_bid_forms(value)[0]), "bigbadbots_bid"), value


def test_f0_05_bid_outside_int64():
    assert _fails(_run(bid=_bid_forms(2**63)[1]), "bigbadbots_bid")
    assert _fails(_run(bid=_bid_forms(10**5000)[1]), "bigbadbots_bid")


def test_f0_05_bid_of_a_finite_out_of_range_integer_is_clamped():
    for amount in (-5, 10**9):
        results = _run(bid=_bid_forms(amount)[1])
        entries = _entries(results, "bigbadbots_bid")
        assert [sev for sev, _ in entries] == ["warn"], amount
        assert "clamped" in entries[0][1]


def test_f0_05_the_wire_checker_rejects_non_finite_text():
    for text in ("NaN", "Infinity", "-Infinity"):
        request = _request("bid")
        message = (
            f'{{"type": "turn_action", "match_id": "{request["match_id"]}", '
            f'"request_id": "{request["request_id"]}", "action": "bid", '
            f'"params": {{"amount": {text}}}}}'
        )
        assert check_turn_action(request, message).verdict == "malformed", text


# ---------------------------------------------------------------------------
# F0-06 (fighter-mvp#21): a bot that mutates every structure it receives
# changes nothing the server sent, and validate tells it so.
# ---------------------------------------------------------------------------


def _vandal(self, state):
    for body in getattr(state, "offer", []):
        body.stats["power"] = 100
        body.slots.append("head")
    for part in getattr(state, "owned", []):
        part.bonus["power"] = 100
    lot = getattr(state, "lot", None)
    if lot is not None:
        lot.effects["recovery"] = -9
    if state.phase == "fight":
        state.me.attrs["power"] = 100
        state.opp.buffs["stunned"] = 99
    else:
        state.me.owned.append("stolen")
        state.history.clear()
    state.valid_actions.append("win")
    if self.game_config is not None:
        self.game_config.rule_set.clear()
    return BigBadBotsBot.decide(self, state)


def test_f0_06_bot_mutates_the_state_it_receives():
    pristine = {
        call: json.loads((FIXTURE_DIR / f"{call}.json").read_text(encoding="utf-8"))["pairs"]
        for call in CALLS
    }
    results = _run(decide=_vandal)
    entries = _entries(results, "bigbadbots_state_mutation")
    assert [sev for sev, _ in entries] == ["warn"]
    # The answers are still judged on their own merits.
    assert _no_fails(results)
    # And nothing the "server" sent changed.
    for call in CALLS:
        assert load_fixtures(call) == pristine[call]


def test_f0_06_a_bot_that_mutates_nothing_passes_the_mutation_check():
    entries = _entries(_run(), "bigbadbots_state_mutation")
    assert [sev for sev, _ in entries] == ["pass"]


# ---------------------------------------------------------------------------
# F0-07 (fighter-mvp#22): SystemExit and other BaseExceptions from a handler,
# and a crash on every tick, end in a reported failure, not a traceback.
# ---------------------------------------------------------------------------


def test_f0_07_handler_calls_sys_exit():
    def act(self, state):
        sys.exit(3)

    results = _run(act=act)
    assert _fails(results, "bigbadbots_act")
    assert _fails(results, "bigbadbots_exit")


def test_f0_07_handler_raises_system_exit():
    def bid(self, state):
        raise SystemExit

    results = _run(bid=bid)
    assert _fails(results, "bigbadbots_bid")
    assert _fails(results, "bigbadbots_exit")


def test_f0_07_handler_raises_another_base_exception():
    class Bail(BaseException):
        pass

    def fit(self, state):
        raise Bail("out")

    results = _run(fit=fit)
    assert _fails(results, "bigbadbots_fit")
    assert _fails(results, "bigbadbots_exit")


def test_f0_07_crash_on_every_tick():
    def act(self, state):
        raise RuntimeError("boom")

    results = _run(act=act)
    assert _fails(results, "bigbadbots_act")
    assert _fails(results, "bigbadbots_crash_every_tick")
    assert not _entries(results, "bigbadbots_exit")


def test_f0_07_a_crash_on_some_ticks_is_not_every_tick():
    def act(self, state):
        if state.tick % 2:
            raise RuntimeError("boom")
        return Action.act("idle")

    results = _run(act=act)
    assert _fails(results, "bigbadbots_act")
    assert not _entries(results, "bigbadbots_crash_every_tick")


def test_f0_07_keyboard_interrupt_still_stops_validate():
    def act(self, state):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(act=act)


def test_on_match_start_raising_is_reported():
    def on_match_start(self, match_info):
        raise SystemExit

    assert _fails(_run(on_match_start=on_match_start), "bigbadbots_match_start")


def test_a_slow_act_is_reported():
    import time as _time

    def act(self, state):
        if state.request_id == load_fixtures("act")[0]["request"]["request_id"]:
            _time.sleep(0.15)
        return Action.act("idle")

    entries = _entries(_run(act=act), "bigbadbots_timing")
    assert [sev for sev, _ in entries] == ["warn"]


# ---------------------------------------------------------------------------
# validate_bot / the CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STARTERS)
def test_starter_passes_validate(name):
    results = validate_bot(STARTERS_DIR / name)
    failures = [(n, m) for sev, n, m in results if sev == "fail"]
    assert failures == [], failures
    names = {n for _, n, _ in results}
    for call in CALLS:
        assert f"bigbadbots_{call}" in names
    assert {"act_method", "smoke_test", "bigbadbots_state_mutation", "bigbadbots_timing"} <= names
    # The poker checks are not run on a Big Bad Bots bot.
    assert "decide_method" not in names and "timeout" not in names


def test_starter_passes_the_cli(capsys):
    validate_cli([str(STARTERS_DIR / "rusher"), "--no-color"])
    assert "All checks passed" in capsys.readouterr().out


def test_check_connectivity_is_skipped_for_big_bad_bots():
    results = validate_bot(STARTERS_DIR / "turtle", check_connectivity=True)
    assert _no_fails(results)
    assert [sev for sev, _ in _entries(results, "connectivity")] == ["warn"]


def _write(tmp_path, code: str) -> Path:
    (tmp_path / "main.py").write_text(textwrap.dedent(code))
    return tmp_path


def test_a_big_bad_bots_bot_without_act_fails(tmp_path):
    _write(
        tmp_path,
        """\
        from chipzen import BigBadBotsBot

        class Lazy(BigBadBotsBot):
            pass
        """,
    )
    results = validate_bot(tmp_path)
    assert _fails(results, "act_method")


def test_a_malformed_starter_style_bot_fails_validate(tmp_path):
    _write(
        tmp_path,
        """\
        from chipzen import Action, BigBadBotsBot

        class Bad(BigBadBotsBot):
            def fit(self, state):
                return []

            def act(self, state):
                return Action.act("idle")
        """,
    )
    results = validate_bot(tmp_path)
    assert _fails(results, "bigbadbots_fit")
    with pytest.raises(SystemExit):
        validate_cli([str(tmp_path), "--no-color"])


def test_game_flag_runs_big_bad_bots_checks_on_a_decide_bot(tmp_path):
    _write(
        tmp_path,
        """\
        from chipzen import Action, Bot

        class Raw(Bot):
            def decide(self, state):
                if state.phase == "fight":
                    return Action.act("block")
                if state.phase == "auction":
                    return Action.bid(0)
                if state.phase == "fitting":
                    return Action.fit({})
                return Action.choose_body(state.offer[0].id)
        """,
    )
    results = validate_bot(tmp_path, game="bigbadbots")
    assert _no_fails(results)
    assert "decide_method" in {n for _, n, _ in results}
    assert "bigbadbots_act" in {n for _, n, _ in results}
    # Without the flag it is a poker bot, validated as before.
    poker = validate_bot(tmp_path)
    assert "bigbadbots_act" not in {n for _, n, _ in poker}


def test_f0_07_a_bot_that_exits_at_import_fails_validate(tmp_path):
    _write(
        tmp_path,
        """\
        import sys
        from chipzen import Action, BigBadBotsBot

        class Quitter(BigBadBotsBot):
            def __init__(self):
                sys.exit(1)

            def act(self, state):
                return Action.act("idle")
        """,
    )
    assert _fails(validate_bot(tmp_path), "smoke_test")


def test_an_unknown_game_fails(tmp_path):
    assert validate_bot(tmp_path, game="chess")[0][:2] == ("fail", "game")


def test_poker_bots_are_validated_exactly_as_before(tmp_path):
    _write(
        tmp_path,
        """\
        from chipzen import Bot, GameState, Action

        class MyBot(Bot):
            def decide(self, state: GameState) -> Action:
                return Action.fold()
        """,
    )
    names = [n for _, n, _ in validate_bot(tmp_path)]
    assert names == [
        "size",
        "file_structure",
        "syntax",
        "imports",
        "bot_class",
        "decide_method",
        "requirements",
        "smoke_test",
        "timeout",
    ]


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        (
            "chipzen-bot @ https://github.com/chipzen-ai/chipzen-sdk/archive/refs/heads/main"
            ".tar.gz#subdirectory=packages/python",
            True,
        ),
        ("numpy>=1.20; python_version >= '3.10'", True),
        ("evil @ https://example.com/evil.tar.gz", False),
    ],
)
def test_requirements_accept_direct_references_by_package_name(tmp_path, line, ok):
    from chipzen.validate import _check_requirements

    (tmp_path / "requirements.txt").write_text(line + "\n")
    [(sev, _, _)] = _check_requirements(tmp_path / "requirements.txt")
    assert (sev == "pass") is ok
