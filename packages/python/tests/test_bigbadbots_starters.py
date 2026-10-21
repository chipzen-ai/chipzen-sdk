"""Tests for the Big Bad Bots starter bots (chipzen-sdk#142).

``packages/python/starters/bigbadbots/`` ships three starters, one folder each:
``rusher``, ``zoner`` and ``turtle``. Each must:

* answer every call of the protocol doc's scripted session (body shop, one
  auction lot, fitting, one fight tick) with one of that call's
  ``valid_actions``, inside the per-tick deadline, through the real session
  loop with ``safe_mode=False`` so any exception fails the test;
* answer legally across a sweep of gear-up and fight states built on an
  illustrative full rule set (the doc's ``game_config`` is abridged, so the
  session above only exercises the no-frame-data path);
* be found by ``chipzen run-external`` and pointed at staging by ``--env
  staging`` or ``CHIPZEN_ENV=staging``, at prod by default (no network: the
  socket is stubbed);
* ship the same IP-protected, non-root Docker packaging as the poker starter.
"""

import copy
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from test_bigbadbots_protocol_doc import _requests, _session_script
from test_run_external import (
    _bypass_config_discovery,
    _clear_chipzen_env,
    _install_connect_stub,
)

from chipzen import Action, BigBadBotsBot
from chipzen.bigbadbots import (
    FIGHT_ACTIONS,
    BigBadBotsGameConfig,
    parse_bigbadbots_turn_request,
)
from chipzen.client import _run_session
from chipzen.conformance import _MockWebSocket
from chipzen.run_external import _find_bot_subclasses, _load_bot_module, run_external_cli

STARTERS_DIR = Path(__file__).parent.parent / "starters" / "bigbadbots"
STARTERS = {"rusher": "Rusher", "zoner": "Zoner", "turtle": "Turtle"}

#: The provisional per-tick deadline (protocol section 3.4). Every answer must
#: come back well inside it.
DEADLINE_MS = 100


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_bbb_starter_{name}", STARTERS_DIR / name / "bot.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bot(name: str) -> BigBadBotsBot:
    return getattr(_load(name), STARTERS[name])()


# ---------------------------------------------------------------------------
# An illustrative full rule set. The numbers are made up for the test; real
# matches send the real table in match_start.game_config.
# ---------------------------------------------------------------------------


def _move(kind, startup, recovery, stamina, damage, reach=1.0, rng=None, active=1):
    return {
        "kind": kind,
        "startup": startup,
        "active": active,
        "recovery": recovery,
        "stamina": stamina,
        "damage": damage,
        "reach": reach,
        "range": rng,
    }


RULE_SET = {
    "gearup": {"budget": 1000, "body_offer_size": 4, "auction_lots": 6},
    "arena": {"width": 12.0, "min_separation": 1.0},
    "rounds": {"ticks_per_second": 10, "round_ticks": 600, "timeout_tiebreak": "damage"},
    "deadline": {"deadline_ms": DEADLINE_MS, "time_bank_ms": 3000},
    "bodies": {
        "golem": {"slots": ["core"], "special": "stone_skin"},
        "warden": {"slots": ["arms", "core", "head"], "special": "counter"},
        "brawler": {"slots": ["legs", "arms", "core", "head"], "special": "frenzy"},
        "scrapper": {"slots": ["legs", "legs", "arms", "arms", "core"], "special": "overdrive"},
        "wisp": {"slots": ["legs", "legs", "head"], "special": "blink"},
    },
    "parts": {
        "spring": {"slot": "legs", "bonus": {"speed": 20, "stamina": -5}, "effects": {}},
        "hammer": {"slot": "arms", "bonus": {"power": 15}, "effects": {}},
        "long_arm": {"slot": "arms", "bonus": {"reach": 15}, "effects": {}},
        "plated": {"slot": "core", "bonus": {"health": 8, "guard": 12}, "effects": {}},
        "capacitor": {"slot": "core", "bonus": {"stamina": 15}, "effects": {}},
        "stabilizer": {"slot": "head", "bonus": {}, "effects": {"recovery": -1}},
        "whip": {"slot": "arms", "bonus": {"reach": 30, "power": -70}, "effects": {}},
    },
    "moves": {
        "punch_high": _move("strike", 2, 3, 4, 5),
        "uppercut": _move("strike", 4, 6, 12, 12, reach=0.9),
        "kick_high": _move("strike", 5, 6, 12, 11, reach=1.25),
        "kick_low": _move("strike", 3, 4, 7, 6, reach=1.1),
        "throw": _move("throw", 3, 5, 9, 9, rng=1.2),
    },
    "specials": {
        "stone_skin": _move("utility", 0, 0, 40, 0),
        "counter": _move("utility", 0, 2, 40, 0, active=10),
        "frenzy": _move("utility", 0, 0, 40, 0),
        "overdrive": _move("utility", 0, 0, 40, 0),
        "blink": _move("utility", 1, 1, 25, 0),
        "lance": _move("strike", 6, 6, 40, 12, reach=2.0),
    },
}

GAME_CONFIG = {
    "variant": "bigbadbots",
    "rule_set_id": "bigbadbots-1.2",
    "num_players": 2,
    "rule_set": RULE_SET,
}


def _with_rules(bot: BigBadBotsBot) -> BigBadBotsBot:
    bot.on_match_start({"game_config": GAME_CONFIG})
    return bot


def _state(call: str, **state_overrides):
    """A doc request for ``call`` with ``state`` fields replaced, parsed."""
    request = copy.deepcopy(_requests()[call])
    for key, value in state_overrides.items():
        if key in ("me", "opp") and isinstance(value, dict):
            request["state"][key].update(value)
        else:
            request["state"][key] = value
    return parse_bigbadbots_turn_request(request)


def _timed(fn, state):
    start = time.perf_counter()
    action = fn(state)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < DEADLINE_MS, f"{fn.__name__} took {elapsed_ms:.1f} ms"
    return action


# ---------------------------------------------------------------------------
# Layout and discovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STARTERS)
def test_starter_has_its_files(name):
    expected = {"bot.py", "requirements.txt", "Dockerfile", ".dockerignore", "README.md"}
    actual = {p.name for p in (STARTERS_DIR / name).iterdir() if p.is_file()}
    assert expected <= actual, f"{name} is missing {expected - actual}"


@pytest.mark.parametrize("name", STARTERS)
def test_starter_defines_one_bigbadbots_bot_and_a_main(name):
    module = _load_bot_module(STARTERS_DIR / name / "bot.py")
    found = _find_bot_subclasses(module)
    assert [cls.__name__ for cls in found] == [STARTERS[name]]
    assert issubclass(found[0], BigBadBotsBot)
    assert callable(module.main), "the Dockerfile ENTRYPOINT calls bot.main()"


# ---------------------------------------------------------------------------
# The protocol doc's scripted session, through the real loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STARTERS)
async def test_starter_answers_every_call_of_the_doc_session(name):
    bot = _bot(name)
    decide = bot.decide
    timings: list[float] = []

    def timed_decide(state):
        start = time.perf_counter()
        try:
            return decide(state)
        finally:
            timings.append((time.perf_counter() - start) * 1000)

    bot.decide = timed_decide  # type: ignore[method-assign]
    script = _session_script()
    ws = _MockWebSocket(script)
    end = await _run_session(
        ws,
        bot,
        match_id=script[0]["match_id"],
        token="t",
        ticket=None,
        client_name=f"bigbadbots-starter-{name}",
        client_version="0.0.0",
        safe_mode=False,
        supported_games=None,
    )
    assert end is not None and end["type"] == "match_end"

    sent = [json.loads(frame) for frame in ws.sent]
    hello = next(frame for frame in sent if frame["type"] == "hello")
    assert hello["supported_games"] == ["bigbadbots"]

    requests = _requests()
    calls = ("choose_body", "bid", "fit", "act")
    answers = [frame for frame in sent if frame["type"] == "turn_action"]
    assert [a["request_id"] for a in answers] == [requests[c]["request_id"] for c in calls]
    for call, answer in zip(calls, answers):
        assert answer["action"] in requests[call]["valid_actions"], (call, answer)

    offer = {b["id"] for b in requests["choose_body"]["state"]["offer"]}
    assert answers[0]["params"]["body"] in offer
    amount = answers[1]["params"]["amount"]
    assert isinstance(amount, int) and 0 <= amount <= requests["bid"]["state"]["me"]["credits"]
    assert set(answers[2]["params"]["fit"].values()) <= {"capacitor"}

    assert len(timings) == 4
    assert max(timings) < DEADLINE_MS, timings


# ---------------------------------------------------------------------------
# Gear-up with a full rule set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STARTERS)
def test_body_choice_is_on_the_offer_and_affordable(name):
    bot = _with_rules(_bot(name))
    for credits in (1000, 500, 400, 100):
        state = _state("choose_body", me={"credits": credits})
        action = _timed(bot.choose_body, state)
        offer = {b.id: b.cost for b in state.offer}
        assert action.action == "choose_body"
        chosen = action.params["body"]
        assert chosen in offer
        if any(cost <= credits for cost in offer.values()):
            assert offer[chosen] <= credits
        else:
            assert offer[chosen] == min(offer.values())


@pytest.mark.parametrize("name", STARTERS)
def test_bids_are_integers_in_range(name):
    bot = _with_rules(_bot(name))
    lots = [
        {"id": pid, "slot": p["slot"], "bonus": p["bonus"], "effects": p["effects"]}
        for pid, p in RULE_SET["parts"].items()
    ]
    for body in ("golem", "scrapper", "wisp"):
        for lot in lots:
            for lot_index in (0, 3, 5):
                for credits, opp_credits in ((450, 740), (30, 900), (0, 10), (600, 0)):
                    state = _state(
                        "bid",
                        lot=lot,
                        lot_index=lot_index,
                        me={"credits": credits, "body": body, "owned": ["capacitor"]},
                        opp={"credits": opp_credits},
                    )
                    action = _timed(bot.bid, state)
                    amount = action.params["amount"]
                    assert action.action == "bid"
                    assert isinstance(amount, int) and not isinstance(amount, bool)
                    assert 0 <= amount <= max(0, min(credits, opp_credits + 1))


@pytest.mark.parametrize("name", STARTERS)
def test_a_useless_lot_gets_no_bid_and_the_last_useful_lot_gets_a_real_one(name):
    bot = _with_rules(_bot(name))
    hammer = {"id": "hammer", "slot": "arms", "bonus": {"power": 15}, "effects": {}}
    # Golem has one core slot and no arms: an arm part is worth nothing to it.
    useless = _state("bid", lot=hammer, me={"body": "golem", "owned": []})
    assert bot.bid(useless).params["amount"] == 0
    last = _state(
        "bid", lot=hammer, lot_index=5, me={"body": "scrapper", "owned": [], "credits": 300}
    )
    assert bot.bid(last).params["amount"] == min(300, last.opp.credits + 1)


@pytest.mark.parametrize("name", STARTERS)
def test_fitting_names_owned_parts_in_matching_slots(name):
    bot = _with_rules(_bot(name))
    owned = [
        {"id": pid, "slot": p["slot"], "bonus": p["bonus"], "effects": p["effects"]}
        for pid, p in RULE_SET["parts"].items()
    ]
    for body, spec in RULE_SET["bodies"].items():
        state = _state("fit", owned=owned, slots=spec["slots"], me={"body": body})
        action = _timed(bot.fit, state)
        fit = action.params["fit"]
        assert action.action == "fit"
        assert len(set(fit.values())) == len(fit), "a part fitted twice"
        for slot, part_id in fit.items():
            index = int(slot)
            assert 0 <= index < len(spec["slots"])
            assert RULE_SET["parts"][part_id]["slot"] == spec["slots"][index]
        # A part that only subtracts (by every bot's taste) is never fitted.
        assert "whip" not in fit.values()


# ---------------------------------------------------------------------------
# The fight with a full rule set
# ---------------------------------------------------------------------------


def _fight(me=None, opp=None, **fields):
    return _state("act", me=me or {}, opp=opp or {}, **fields)


OPP_PHASES = [
    {"action": None, "phase": None, "frames_left": 0},
    {"action": "attack", "phase": "startup", "frames_left": None},
    {"action": "kick_high", "phase": "active", "frames_left": 1},
    {"action": "uppercut", "phase": "recovery", "frames_left": 6},
    {"action": "punch_high", "phase": "recovery", "frames_left": 1},
    {"action": "hitstun", "phase": "hitstun", "frames_left": 2},
    {"action": "dazed", "phase": "dazed", "frames_left": 12},
]


@pytest.mark.parametrize("name", STARTERS)
def test_every_fight_answer_is_a_fight_action(name):
    bot = _with_rules(_bot(name))
    for opp in OPP_PHASES:
        for distance in (1.0, 1.4, 1.9, 2.5, 6.0):
            for stamina in (0.0, 10.0, 50.0, 100.0):
                for special_used in (False, True):
                    for facing, x in ((1, 0.2), (-1, 11.8), (1, 6.0)):
                        for tick in (5, 500):
                            state = _fight(
                                me={
                                    "stamina": stamina,
                                    "special_used": special_used,
                                    "x": x,
                                    "hp": 120.0,
                                },
                                opp=opp,
                                distance=distance,
                                facing=facing,
                                tick=tick,
                            )
                            action = _timed(bot.act, state)
                            assert isinstance(action, Action)
                            assert action.action in FIGHT_ACTIONS
                            assert action.params == {}


@pytest.mark.parametrize("name", STARTERS)
def test_a_busy_fighter_idles(name):
    bot = _with_rules(_bot(name))
    state = _fight(me={"action": "kick_high", "phase": "recovery", "frames_left": 3})
    assert bot.act(state).action == "idle"


@pytest.mark.parametrize("name", STARTERS)
def test_every_starter_punishes_a_recovering_opponent_in_reach(name):
    bot = _with_rules(_bot(name))
    state = _fight(
        opp={"action": "uppercut", "phase": "recovery", "frames_left": 6},
        distance=1.2,
        me={"stamina": 100.0},
    )
    assert bot.act(state).action in ("punch_high", "uppercut", "kick_high", "kick_low", "throw")


@pytest.mark.parametrize("name", STARTERS)
def test_every_starter_answers_without_a_rule_set(name):
    """Before match_start (or after a reconnect without game_config) there is
    no frame data. The bot must still answer every call."""
    bot = _bot(name)
    assert bot.game_config is None
    for call in ("choose_body", "bid", "fit"):
        request = _requests()[call]
        action = bot.decide(parse_bigbadbots_turn_request(request))
        assert action.action in request["valid_actions"]
    for opp in OPP_PHASES:
        assert bot.act(_fight(opp=opp)).action in FIGHT_ACTIONS


def test_rusher_walks_in_from_far_away():
    bot = _with_rules(_bot("rusher"))
    state = _fight(
        opp={"action": None, "phase": None, "frames_left": 0}, distance=6.0, me={"stamina": 100.0}
    )
    assert bot.act(state).action == "move_in"


def test_rusher_strikes_first_against_a_wind_up():
    bot = _with_rules(_bot("rusher"))
    state = _fight(distance=1.2, me={"stamina": 100.0, "special_used": True})
    assert state.opp.phase == "startup"
    assert bot.act(state).action == "punch_high"  # its fastest move that reaches


def test_zoner_backs_off_when_crowded_and_steps_out_of_a_wind_up():
    bot = _with_rules(_bot("zoner"))
    crowded = _fight(
        opp={"action": None, "phase": None, "frames_left": 0},
        distance=1.0,
        facing=-1,
        me={"x": 6.0, "stamina": 100.0},
    )
    assert bot.act(crowded).action == "move_out"
    wind_up = _fight(distance=1.8, facing=-1, me={"x": 6.0})
    assert bot.act(wind_up).action == "move_out"


def test_zoner_blocks_a_wind_up_with_its_back_to_the_wall():
    bot = _with_rules(_bot("zoner"))
    # facing -1: the opponent is to the left, so our back is to the right wall.
    state = _fight(distance=1.8, facing=-1, me={"x": 12.0}, walls=[0.0, 12.0])
    assert bot.act(state).action == "block"


def test_zoner_kicks_from_the_edge_of_its_range():
    bot = _with_rules(_bot("zoner"))
    state = _fight(
        opp={"action": None, "phase": None, "frames_left": 0},
        distance=1.95,
        me={"stamina": 100.0},
    )
    assert state.me.strike_range == pytest.approx(1.615)
    assert bot.act(state).action == "kick_high"


def test_turtle_blocks_a_wind_up_and_raises_a_counter_stance_if_it_has_one():
    bot = _with_rules(_bot("turtle"))
    assert bot.act(_fight(distance=1.5)).action == "block"  # golem: no stance
    warden = _fight(distance=1.5, me={"special": "counter", "stamina": 100.0})
    assert bot.act(warden).action == "special"


def test_turtle_waits_then_loses_patience():
    bot = _with_rules(_bot("turtle"))
    quiet = {"action": None, "phase": None, "frames_left": 0}
    # special_used: its armour special would otherwise go up first.
    me = {"stamina": 100.0, "special_used": True}
    state = _fight(opp=quiet, distance=1.9, me=me, tick=10)
    answers = [bot.act(state).action for _ in range(100)]
    assert answers[0] == "idle"
    assert answers[-1] in ("punch_high", "uppercut", "kick_high", "kick_low", "throw", "move_in")


def test_turtle_attacks_when_behind_late_in_the_round():
    bot = _with_rules(_bot("turtle"))
    quiet = {"action": None, "phase": None, "frames_left": 0, "hp": 180.0, "max_hp": 182.8}
    me = {"stamina": 100.0, "hp": 120.0, "special_used": True}
    behind = _fight(opp=quiet, distance=1.9, me=me, tick=500)
    assert bot.act(behind).action != "idle"
    early = _fight(opp=quiet, distance=1.9, me=me, tick=10)
    assert _with_rules(_bot("turtle")).act(early).action == "idle"


# ---------------------------------------------------------------------------
# Prod by default, staging on request (no network: the socket is stubbed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STARTERS)
@pytest.mark.parametrize(
    ("flag", "env_var", "expected_host"),
    [
        ("staging", None, "staging.chipzen.ai"),
        (None, "staging", "staging.chipzen.ai"),
        (None, None, "chipzen.ai"),
    ],
    ids=["flag", "env-var", "default-prod"],
)
def test_starter_runs_against_staging_or_prod(
    name, flag, env_var, expected_host, tmp_path, monkeypatch
):
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)
    if env_var is not None:
        monkeypatch.setenv("CHIPZEN_ENV", env_var)
    stub = _install_connect_stub(monkeypatch)

    args = [str(STARTERS_DIR / name / "bot.py"), "--bot-id", "bbb-bot", "--token", "cz_extbot_t"]
    if flag is not None:
        args += ["--env", flag]
    run_external_cli(args)

    assert stub.urls == [f"wss://{expected_host}/ws/external/bot/bbb-bot"]


# ---------------------------------------------------------------------------
# Packaging: the poker starter's IP-protected, non-root image
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STARTERS)
def test_dockerfile_compiles_then_strips_source_and_runs_as_non_root(name):
    text = (STARTERS_DIR / name / "Dockerfile").read_text(encoding="utf-8")
    assert "cythonize -i bot.py && rm bot.py" in text
    assert "COPY --from=builder /build/*.so /bot/" in text
    assert "USER 10001" in text and "useradd" in text
    assert 'ENTRYPOINT ["python", "-c", "from bot import main; main()"]' in text


@pytest.mark.parametrize("name", STARTERS)
def test_readme_shows_staging_and_prod(name):
    text = (STARTERS_DIR / name / "README.md").read_text(encoding="utf-8")
    assert "--env staging" in text
    assert "CHIPZEN_ENV=staging" in text
    assert "—" not in text, "no em-dashes in user-facing copy"


def test_game_config_used_by_these_tests_parses():
    assert BigBadBotsGameConfig.from_dict(GAME_CONFIG).rule_set_id == "bigbadbots-1.2"
