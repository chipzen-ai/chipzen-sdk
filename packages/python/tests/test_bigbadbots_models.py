"""Big Bad Bots typed models and the ``state_shape`` router (chipzen-sdk#139).

Every fixture is a JSON example published in
``docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md``, read straight out of the
document, so the models are held to the same payloads the spec is:

* each published ``turn_request`` parses into the model for its call, and
  serialises back to the exact ``state`` it came from;
* each published ``turn_action`` is what the matching ``Action`` factory emits;
* a missing or mistyped required field raises, naming the field;
* ``parse_turn_request`` routes on ``state_shape`` and refuses one it does not
  know, with no fallback to the poker parser (LAYER2-COMMON section 2).
"""

import copy
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
from chipzen.bigbadbots import (
    FIGHT_ACTIONS,
    AuctionState,
    BigBadBotsGameConfig,
    Body,
    BodyShopState,
    Fighter,
    FightState,
    FittingState,
    Part,
    Side,
    SoldLot,
    StateParseError,
    parse_bigbadbots_turn_request,
)
from chipzen.models import (
    Action,
    GameState,
    UnsupportedStateShapeError,
    parse_turn_request,
    state_shape_of,
)

DOC = (
    Path(__file__).resolve().parents[3] / "docs" / "protocol" / "BIGBADBOTS-GAME-STATE-PROTOCOL.md"
)
_FENCE = re.compile(r"^```json\n(.*?)\n```$", re.MULTILINE | re.DOTALL)

PHASE_CALL = {"body_shop": "choose_body", "auction": "bid", "fitting": "fit", "fight": "act"}
MODEL = {
    "choose_body": BodyShopState,
    "bid": AuctionState,
    "fit": FittingState,
    "act": FightState,
}
CALLS = list(MODEL)


def _blocks() -> list[dict]:
    return [json.loads(body) for body in _FENCE.findall(DOC.read_text(encoding="utf-8"))]


def _request(call: str) -> dict:
    """A fresh deep copy of the published ``turn_request`` for ``call``."""
    (found,) = [
        b
        for b in _blocks()
        if b.get("type") == "turn_request" and PHASE_CALL[b["state"]["phase"]] == call
    ]
    return copy.deepcopy(found)


def _answers() -> list[dict]:
    return [b for b in _blocks() if b.get("type") == "turn_action"]


def _game_config() -> dict:
    (found,) = [b for b in _blocks() if "rule_set_id" in b]
    return found


def _descriptor() -> dict:
    (found,) = [b for b in _blocks() if "state_shape" in b]
    return found


# ---------------------------------------------------------------------------
# Round trip: published payload -> model -> the same payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call", CALLS)
def test_each_published_request_parses_into_its_calls_model(call):
    request = _request(call)
    parsed = parse_bigbadbots_turn_request(request)

    assert type(parsed) is MODEL[call]
    assert parsed.phase == request["state"]["phase"]
    assert parsed.request_id == request["request_id"]
    assert parsed.seat == request["seat"]
    assert parsed.timeout_ms == request["timeout_ms"]
    assert parsed.valid_actions == request["valid_actions"]


@pytest.mark.parametrize("call", CALLS)
def test_each_published_state_round_trips_exactly(call):
    request = _request(call)
    state = parse_bigbadbots_turn_request(request).to_state()

    assert state == request["state"]
    # Numbers keep their JSON type: 226.0 stays a float, 21 stays an int.
    assert json.dumps(state, sort_keys=True) == json.dumps(request["state"], sort_keys=True)


def test_body_shop_offer_is_typed():
    state = parse_bigbadbots_turn_request(_request("choose_body"))
    assert isinstance(state, BodyShopState)

    assert [body.id for body in state.offer] == ["warden", "golem", "brawler", "scrapper"]
    golem = state.offer[1]
    assert isinstance(golem, Body)
    assert golem.cost == 550
    assert golem.slots == ["core"]
    assert golem.special == "stone_skin"
    assert golem.stats["health"] == 80
    assert isinstance(state.me, Side) and state.me.body is None and state.me.fitted == []
    assert state.opp.offer == []
    assert state.history == []


def test_auction_lot_and_sides_are_typed():
    state = parse_bigbadbots_turn_request(_request("bid"))
    assert isinstance(state, AuctionState)

    assert isinstance(state.lot, Part)
    assert state.lot.id == "spring"
    assert state.lot.bonus == {"speed": 25, "stamina": -10}
    assert state.lot.effects == {}
    assert state.me.credits == 450 and state.me.body == "golem"
    assert state.opp.body == "wisp" and state.opp.fitted == [None, None, None]


def test_fitting_owned_slots_and_history_are_typed():
    state = parse_bigbadbots_turn_request(_request("fit"))
    assert isinstance(state, FittingState)

    assert [part.id for part in state.owned] == ["capacitor"]
    assert state.slots == ["core"]
    assert state.lot_index == state.lots_total == 6
    assert all(isinstance(lot, SoldLot) for lot in state.history)
    assert [lot.winner for lot in state.history] == ["opp", None, "opp", "opp", None, "me"]
    assert state.history[-1].my_bid == 96 and state.history[-1].opp_bid == 36


def test_fight_fighters_are_typed_and_the_wind_up_mask_survives():
    state = parse_bigbadbots_turn_request(_request("act"))
    assert isinstance(state, FightState)

    assert (state.tick, state.round, state.rounds_won) == (21, 1, [0, 0])
    assert state.distance == 1.798 and state.facing == -1
    assert state.walls == [0.0, 12.0]
    assert isinstance(state.me, Fighter) and isinstance(state.opp, Fighter)
    assert state.me.action is None and state.me.frames_left == 0
    assert state.me.last_attack == "kick_high"
    # Section 5: a wind-up shows as "attack" with no frames left.
    assert (state.opp.action, state.opp.phase, state.opp.frames_left) == (
        "attack",
        "startup",
        None,
    )
    assert state.opp.attrs["speed"] == 100


def test_the_models_are_frozen():
    state = parse_bigbadbots_turn_request(_request("act"))
    with pytest.raises(AttributeError):
        state.tick = 22  # type: ignore[misc]


def test_game_config_parses_and_round_trips():
    config = BigBadBotsGameConfig.from_match_start({"game_config": _game_config()})

    assert config.variant == "bigbadbots"
    assert config.rule_set_id == "bigbadbots-1.0"
    assert config.num_players == 2
    assert config.rule_set["deadline"]["deadline_ms"] == 100
    assert config.to_dict() == _game_config()


def test_game_config_for_another_variant_fails_loudly():
    with pytest.raises(StateParseError, match="variant"):
        BigBadBotsGameConfig.from_dict({**_game_config(), "variant": "nlhe"})


def test_game_config_missing_its_rule_set_fails_loudly():
    config = _game_config()
    del config["rule_set"]
    with pytest.raises(StateParseError, match="rule_set"):
        BigBadBotsGameConfig.from_dict(config)


# ---------------------------------------------------------------------------
# Actions: the factories emit the published answers
# ---------------------------------------------------------------------------


def _factory_for(answer: dict) -> Action:
    action, params = answer["action"], answer.get("params", {})
    if action == "choose_body":
        return Action.choose_body(params["body"])
    if action == "bid":
        return Action.bid(params["amount"])
    if action == "fit":
        return Action.fit({int(slot): part for slot, part in params["fit"].items()})
    return Action.act(action)


@pytest.mark.parametrize("answer", _answers(), ids=lambda a: a["action"])
def test_each_published_answer_is_what_the_factory_emits(answer):
    wire = _factory_for(answer).to_wire()
    # Section 4.4: a fight action's params may be omitted or {}.
    assert wire == {"action": answer["action"], "params": answer.get("params", {})}


def test_bid_of_zero_and_an_empty_fitting_still_carry_their_params():
    assert Action.bid(0).to_wire() == {"action": "bid", "params": {"amount": 0}}
    assert Action.fit().to_wire() == {"action": "fit", "params": {"fit": {}}}
    assert Action.fit({}).to_wire() == {"action": "fit", "params": {"fit": {}}}


def test_fit_slot_indexes_go_out_as_decimal_strings():
    wire = Action.fit({0: "capacitor", "2": "spring"}).to_wire()
    assert wire["params"] == {"fit": {"0": "capacitor", "2": "spring"}}


@pytest.mark.parametrize("amount", [12.0, True, "12", None])
def test_bid_refuses_a_non_integer_amount(amount):
    with pytest.raises(TypeError):
        Action.bid(amount)  # type: ignore[arg-type]


@pytest.mark.parametrize("slot", [-1, "01", "x", True, 1.5])
def test_fit_refuses_a_slot_that_is_not_an_index(slot):
    with pytest.raises(ValueError):
        Action.fit({slot: "capacitor"})  # type: ignore[dict-item]


@pytest.mark.parametrize("move", FIGHT_ACTIONS)
def test_act_takes_every_fight_action(move):
    assert Action.act(move).to_wire() == {"action": move, "params": {}}


@pytest.mark.parametrize("move", ["fold", "check", "choose_body", "attack", ""])
def test_act_refuses_anything_else(move):
    with pytest.raises(ValueError):
        Action.act(move)


# ---------------------------------------------------------------------------
# Missing or mistyped required fields fail loudly
# ---------------------------------------------------------------------------


def _state_keys(call: str) -> list[str]:
    return sorted(_request(call)["state"])


@pytest.mark.parametrize(
    ("call", "key"), [(call, key) for call in CALLS for key in _state_keys(call)]
)
def test_every_missing_state_field_fails_naming_it(call, key):
    request = _request(call)
    del request["state"][key]
    with pytest.raises(StateParseError, match=re.escape(repr(key))):
        parse_bigbadbots_turn_request(request)


@pytest.mark.parametrize("key", ["request_id", "seat", "timeout_ms", "valid_actions", "state"])
def test_a_missing_envelope_field_fails_naming_it(key):
    request = _request("act")
    del request[key]
    with pytest.raises(StateParseError, match=re.escape(repr(key))):
        parse_bigbadbots_turn_request(request)


@pytest.mark.parametrize(
    ("call", "path"),
    [
        ("choose_body", ["offer", 0, "cost"]),
        ("choose_body", ["me", "fitted"]),
        ("bid", ["lot", "effects"]),
        ("bid", ["opp", "credits"]),
        ("fit", ["owned", 0, "slot"]),
        ("fit", ["history", 5, "winner"]),
        ("act", ["me", "hp"]),
        ("act", ["opp", "frames_left"]),
        ("act", ["opp", "buffs"]),
    ],
)
def test_a_missing_nested_field_fails_naming_it(call, path):
    request = _request(call)
    parent = request["state"]
    for step in path[:-1]:
        parent = parent[step]
    del parent[path[-1]]
    with pytest.raises(StateParseError, match=re.escape(repr(path[-1]))):
        parse_bigbadbots_turn_request(request)


@pytest.mark.parametrize(
    ("call", "path", "value"),
    [
        # Section 4: 12.0 is not an integer, and bool is not one either.
        ("bid", ["lot_index"], 0.0),
        ("bid", ["pot"], False),
        ("bid", ["me", "credits"], 450.0),
        ("choose_body", ["offer", 0, "id"], 7),
        ("fit", ["history", 0, "winner"], "both"),
        ("fit", ["slots"], "core"),
        ("bid", ["opp", "fitted"], ["shield_arm", None, None]),
        # Every float is finite.
        ("act", ["me", "hp"], math.nan),
        ("act", ["distance"], math.inf),
        ("act", ["me", "special_used"], 0),
        ("act", ["opp", "frames_left"], 1.5),
        ("act", ["walls"], [0.0, "12"]),
        ("act", ["me"], None),
    ],
)
def test_a_mistyped_field_fails_loudly(call, path, value):
    request = _request(call)
    parent = request["state"]
    for step in path[:-1]:
        parent = parent[step]
    parent[path[-1]] = value
    with pytest.raises(StateParseError):
        parse_bigbadbots_turn_request(request)


@pytest.mark.parametrize("phase", ["preflop", "draw1", "", "FIGHT"])
def test_a_phase_that_is_not_big_bad_bots_fails_loudly(phase):
    request = _request("act")
    request["state"]["phase"] = phase
    with pytest.raises(StateParseError, match="phase"):
        parse_bigbadbots_turn_request(request)


@pytest.mark.parametrize("call", CALLS)
def test_a_new_key_is_ignored(call):
    """LAYER2-COMMON rule 5: the spec only adds keys, so an unknown one is not an error."""
    request = _request(call)
    request["state"]["added_in_a_later_engine"] = {"anything": 1}
    request["state"]["me"]["added_too"] = True
    parsed = parse_bigbadbots_turn_request(request)
    assert type(parsed) is MODEL[call]
    assert "added_in_a_later_engine" not in parsed.to_state()


# ---------------------------------------------------------------------------
# Routing on state_shape
# ---------------------------------------------------------------------------

NLHE_REQUEST = {
    "type": "turn_request",
    "match_id": "m1",
    "seq": 7,
    "server_ts": "2026-09-29T12:00:00.000Z",
    "seat": 0,
    "request_id": "req_00003",
    "timeout_ms": 30000,
    "valid_actions": ["fold", "call", "raise"],
    "state": {
        "hand_number": 3,
        "phase": "flop",
        "board": ["Ah", "Kd", "7c"],
        "your_hole_cards": ["Qs", "Qh"],
        "pot": 120,
        "your_stack": 940,
        "opponent_stacks": [940],
        "to_call": 40,
        "min_raise": 80,
        "max_raise": 940,
        "action_history": [],
    },
}


def test_state_shape_of_a_hello_with_no_game_descriptor_is_poker():
    assert state_shape_of({"type": "hello", "selected_version": "1.0"}) is None


def test_state_shape_of_the_published_descriptor_is_bigbadbots():
    assert state_shape_of({"type": "hello", "game": _descriptor()}) == "bigbadbots"


@pytest.mark.parametrize("game", [{"game_type": "bigbadbots"}, {"state_shape": 3}, "bigbadbots"])
def test_a_game_descriptor_without_a_state_shape_fails_loudly(game):
    with pytest.raises(UnsupportedStateShapeError):
        state_shape_of({"type": "hello", "game": game})


@pytest.mark.parametrize("call", CALLS)
def test_bigbadbots_routes_to_the_bigbadbots_models(call):
    parsed = parse_turn_request(_request(call), state_shape="bigbadbots")
    assert type(parsed) is MODEL[call]


def test_no_state_shape_still_routes_to_the_poker_parser():
    parsed = parse_turn_request(NLHE_REQUEST, your_seat=0, dealer_seat=1)
    assert isinstance(parsed, GameState)
    assert parsed == GameState.from_turn_request(NLHE_REQUEST, your_seat=0, dealer_seat=1)
    assert parsed.to_call == 40


@pytest.mark.parametrize("shape", ["27tripledraw", "pineapple"])
def test_the_poker_variant_shapes_route_to_the_poker_parser(shape):
    assert isinstance(parse_turn_request(NLHE_REQUEST, state_shape=shape), GameState)


@pytest.mark.parametrize("shape", ["fighter", "chess", "nlhe-v2", "", "BIGBADBOTS"])
def test_an_unknown_state_shape_fails_loudly_with_no_fallback(shape):
    """LAYER2-COMMON section 2: an unfamiliar state_shape is "I cannot parse
    this table", never "drop the keys I do not recognise". The payload here is
    one the poker parser reads happily, so a silent fallback would pass."""
    with pytest.raises(UnsupportedStateShapeError) as raised:
        parse_turn_request(NLHE_REQUEST, state_shape=shape)
    assert raised.value.state_shape == shape


def test_a_bigbadbots_payload_with_a_missing_field_fails_through_the_router():
    request = _request("act")
    del request["state"]["walls"]
    with pytest.raises(StateParseError, match="'walls'"):
        parse_turn_request(request, state_shape="bigbadbots")


def test_the_router_is_exported_from_the_package():
    for name in ("parse_turn_request", "state_shape_of", "UnsupportedStateShapeError"):
        assert name in chipzen.__all__
        assert getattr(chipzen, name) is not None
