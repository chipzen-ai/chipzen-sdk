"""The fighter Layer 2 spec's JSON examples, made executable (chipzen-sdk#137).

``docs/protocol/FIGHTER-GAME-STATE-PROTOCOL.md`` publishes every fighter wire
message a bot must handle. A spec example nobody runs is a spec example that
drifts, so this module reads the fenced JSON blocks straight out of the
document and holds them to what the document says about them:

* every block parses;
* every ``turn_request`` is a Layer 1 v1.0 envelope whose ``state`` carries the
  LAYER2-COMMON Rule 1 and Rule 2 fields with exactly the fighter values
  (empty card arrays, the five chip fields at ``0``, ``opponent_stacks`` of
  ``[0]``), and whose ``valid_actions`` match the call table in section 3.1;
* every ``turn_action`` is a closed Layer 1 bot message whose ``params`` have
  the section 4.4 shape, and answers a published ``turn_request``;
* the masks of section 5 hold in the published payloads;
* the provisional numbers in the document's front table are the numbers in the
  published ``game_config``, and ``timeout_ms`` is the deadline plus the bank;
* the size caps hold;
* **a stock SDK client that declares ``fighter`` parses every fighter message
  without throwing**, driven through the real session loop, and answers with
  well-formed ``turn_action`` frames. That is the LAYER2-COMMON section 3
  promise: the deployed parsers throw inside ``parseGameState``, before
  ``decide()``, so a single bad field would be a hard session kill.

The examples are also validated against the fighter wire schemas the server
checks messages against; that check runs where the schemas live. The matching
executable-example suite for the platform's mirror of this document lands with
the coordinated pair (chipzen-ai/chipzen-sdk#140).
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot
from chipzen.client import _run_session
from chipzen.conformance import _MockWebSocket
from chipzen.models import Action, GameState, RoundStart, TurnResult

DOC = Path(__file__).resolve().parents[3] / "docs" / "protocol" / "FIGHTER-GAME-STATE-PROTOCOL.md"

FIGHT_ACTIONS = [
    "idle",
    "move_in",
    "move_out",
    "block",
    "punch_high",
    "uppercut",
    "kick_high",
    "kick_low",
    "throw",
    "special",
]
#: Section 3.1: phase -> (call, valid_actions).
CALLS = {
    "body_shop": ("choose_body", ["choose_body"]),
    "auction": ("bid", ["bid"]),
    "fitting": ("fit", ["fit"]),
    "fight": ("act", FIGHT_ACTIONS),
}
#: LAYER2-COMMON Rules 1 and 2, with the fighter values (section 2).
LAYER2_COMPAT = {
    "board": [],
    "your_hole_cards": [],
    "pot": 0,
    "to_call": 0,
    "min_raise": 0,
    "max_raise": 0,
    "your_stack": 0,
    "opponent_stacks": [0],
}
#: Section 4.3, per call: the keys a turn_request.state carries beyond the compat set.
STATE_KEYS = {
    "choose_body": {"offer", "me", "opp", "lot_index", "lots_total", "history"},
    "bid": {"lot", "me", "opp", "lot_index", "lots_total", "history"},
    "fit": {"owned", "slots", "me", "opp", "lot_index", "lots_total", "history"},
    "act": {"tick", "round", "rounds_won", "me", "opp", "distance", "facing"},
}
TURN_REQUEST_ENVELOPE = {
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
TURN_ACTION_KEYS = {"type", "match_id", "request_id", "action", "params"}
MAX_BOT_MESSAGE_BYTES = 4096
MAX_SERVER_MESSAGE_BYTES = 8192


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^```json\n(.*?)\n```$", re.MULTILINE | re.DOTALL)


def _blocks() -> list[dict]:
    return [json.loads(body) for body in _FENCE.findall(DOC.read_text(encoding="utf-8"))]


def _of_type(kind: str) -> list[dict]:
    return [b for b in _blocks() if b.get("type") == kind]


def _requests() -> dict[str, dict]:
    """The published turn_request per call, keyed by call name."""
    found: dict[str, dict] = {}
    for request in _of_type("turn_request"):
        call = CALLS[request["state"]["phase"]][0]
        assert call not in found, f"two published {call} requests"
        found[call] = request
    return found


def _one(predicate) -> dict:
    matches = [b for b in _blocks() if predicate(b)]
    assert len(matches) == 1, f"expected one matching example, found {len(matches)}"
    return matches[0]


def _descriptor() -> dict:
    return _one(lambda b: b.get("state_shape") == "fighter")


def _game_config() -> dict:
    return _one(lambda b: "rule_set_id" in b)


def _round_start_state() -> dict:
    return _one(lambda b: "hand_number" in b and "rounds_won" in b and "winner_seats" not in b)


def _compact_size(message: dict) -> int:
    return len(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# The examples themselves
# ---------------------------------------------------------------------------


def test_the_document_exists_and_every_json_block_parses():
    blocks = _blocks()
    # 1.1, 4.1, 4.2, four requests, four answers, two turn_results, 4.6, 4.7.
    assert len(blocks) == 15


def test_every_call_has_one_published_request_and_one_answer():
    assert set(_requests()) == {"choose_body", "bid", "fit", "act"}
    calls_answered = {
        a["action"] if a["action"] in ("choose_body", "bid", "fit") else "act"
        for a in _of_type("turn_action")
    }
    assert calls_answered == {"choose_body", "bid", "fit", "act"}


@pytest.mark.parametrize("call", ["choose_body", "bid", "fit", "act"])
def test_turn_request_is_a_layer1_envelope_with_the_fighter_compat_fields(call):
    request = _requests()[call]
    assert TURN_REQUEST_ENVELOPE <= set(request)
    assert request["seat"] in (0, 1)
    assert isinstance(request["timeout_ms"], int) and request["timeout_ms"] >= 1

    state = request["state"]
    phase = state["phase"]
    assert CALLS[phase][0] == call
    assert request["valid_actions"] == CALLS[phase][1]

    for key, value in LAYER2_COMPAT.items():
        assert key in state, f"{call}: Rule 1/2 field {key!r} missing"
        assert state[key] == value, f"{call}: {key!r} is {state[key]!r}, not {value!r}"
        assert type(state[key]) is type(value)

    assert set(state) == {"phase", *LAYER2_COMPAT, *STATE_KEYS[call]}


@pytest.mark.parametrize("answer", _of_type("turn_action"), ids=lambda a: a["action"])
def test_turn_action_is_a_closed_layer1_message_answering_a_published_request(answer):
    assert set(answer) <= TURN_ACTION_KEYS
    assert {"type", "match_id", "request_id", "action"} <= set(answer)

    requests = {r["request_id"]: r for r in _of_type("turn_request")}
    request = requests[answer["request_id"]]
    assert answer["match_id"] == request["match_id"]
    assert answer["action"] in request["valid_actions"]

    params = answer.get("params", {})
    action = answer["action"]
    if action == "choose_body":
        assert set(params) == {"body"}
        assert params["body"] in [b["id"] for b in request["state"]["offer"]]
    elif action == "bid":
        assert set(params) == {"amount"}
        assert type(params["amount"]) is int
    elif action == "fit":
        assert set(params) == {"fit"}
        slots = request["state"]["slots"]
        owned = {p["id"]: p["slot"] for p in request["state"]["owned"]}
        for slot, part in params["fit"].items():
            assert re.fullmatch(r"0|[1-9][0-9]?", slot), "slot index is a decimal string"
            assert owned[part] == slots[int(slot)]
    else:
        assert action in FIGHT_ACTIONS
        assert params == {}


def test_game_descriptor_matches_the_call_table():
    descriptor = _descriptor()
    assert descriptor["game_type"] == "fighter"
    assert descriptor["variant"] == "fighter"
    assert descriptor["state_shape"] == "fighter"
    assert descriptor["phases"] == list(CALLS)
    assert descriptor["actions"] == ["choose_body", "bid", "fit", *FIGHT_ACTIONS]
    assert not {"fold", "check", "call", "raise"} & set(descriptor["actions"])


def test_game_config_names_the_rule_set_and_is_not_the_variant():
    config = _game_config()
    assert config["variant"] == "fighter"
    assert re.fullmatch(r"fighter-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", config["rule_set_id"])
    assert config["num_players"] == 2
    assert config["rule_set"]["gearup"]["auction_lots"] == _requests()["bid"]["state"]["lots_total"]


# ---------------------------------------------------------------------------
# Provisional values: the front table, the game_config and the requests agree
# ---------------------------------------------------------------------------


def test_the_provisional_table_matches_the_published_game_config():
    text = DOC.read_text(encoding="utf-8")
    table = text.split("## Overview", 1)[0]
    rule_set = _game_config()["rule_set"]
    deadline, forfeit = rule_set["deadline"], rule_set["forfeit"]

    assert "**100 ms** (provisional)" in table and deadline["deadline_ms"] == 100
    assert "**3 s per seat per round**" in table and deadline["time_bank_ms"] == 3000
    assert "**5 s** of missed ticks" in table and forfeit["grace_seconds"] == 5
    assert "**2** forfeited rounds" in table and forfeit["rounds_for_match"] == 2
    assert deadline["default_action_policy"] == "repeat_block_or_move"
    # Every row of the table carries the marker.
    rows = [line for line in table.splitlines() if line.startswith("> | ") and "D-0" in line]
    assert len(rows) == 5
    assert all("provisional" in row for row in rows)


def test_act_timeout_is_the_deadline_plus_an_untouched_bank():
    deadline = _game_config()["rule_set"]["deadline"]
    act = _requests()["act"]
    assert act["timeout_ms"] == deadline["deadline_ms"] + deadline["time_bank_ms"]


def test_the_grace_in_ticks_is_the_documented_fifty():
    rule_set = _game_config()["rule_set"]
    ticks = rule_set["forfeit"]["grace_seconds"] * rule_set["rounds"]["ticks_per_second"]
    assert ticks == 50
    assert "(50 ticks at 10 per second)" in DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Masks (section 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call", ["choose_body", "bid", "fit"])
def test_gear_up_masks_the_opponent_offer_and_fitting(call):
    state = _requests()[call]["state"]
    assert state["opp"]["offer"] == []
    assert all(slot is None for slot in state["opp"]["fitted"])
    assert all(slot is None for slot in state["me"]["fitted"])
    # A seat always sees its own offer.
    assert state["me"]["offer"]


def test_body_shop_hides_both_bodies_and_shows_the_own_offer_in_full():
    state = _requests()["choose_body"]["state"]
    assert state["me"]["body"] is None and state["opp"]["body"] is None
    assert [b["id"] for b in state["offer"]] == state["me"]["offer"]


def test_auction_history_never_carries_the_lot_being_sold():
    for call in ("bid", "fit"):
        state = _requests()[call]["state"]
        assert len(state["history"]) == state["lot_index"]
    bid = _requests()["bid"]["state"]
    assert "opp_bid" not in bid["lot"]


def test_a_winding_up_opponent_shows_only_attack():
    opp = _requests()["act"]["state"]["opp"]
    assert opp["phase"] == "startup"
    assert opp["action"] == "attack"
    assert opp["frames_left"] is None
    me = _requests()["act"]["state"]["me"]
    assert me["action"] != "attack" and me["frames_left"] is not None


def test_turn_result_shows_the_opponent_masked_too():
    fight = _one(lambda b: b.get("type") == "turn_result")
    assert fight["details"]["opponent_action"] == "attack"
    assert fight["seat"] in (0, 1)


# ---------------------------------------------------------------------------
# Size caps (section 4)
# ---------------------------------------------------------------------------


def test_size_caps_hold_for_every_published_message():
    for request in _of_type("turn_request"):
        assert _compact_size(request) <= MAX_SERVER_MESSAGE_BYTES
    for answer in _of_type("turn_action"):
        assert _compact_size(answer) <= MAX_BOT_MESSAGE_BYTES


# ---------------------------------------------------------------------------
# A stock client parses every fighter message without throwing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call", ["choose_body", "bid", "fit", "act"])
def test_stock_parser_reads_every_turn_request(call):
    request = _requests()[call]
    state = GameState.from_turn_request(request, your_seat=request["seat"])
    assert state.hole_cards == [] and state.board == []
    assert state.pot == 0 and state.to_call == 0 and state.opponent_stacks == [0]
    assert state.phase == request["state"]["phase"]
    assert state.valid_actions == request["valid_actions"]
    assert state.request_id == request["request_id"]


def test_stock_parser_reads_round_start_and_turn_result():
    round_start = {
        "type": "round_start",
        "match_id": "5b2f0c1e-8d4a-4c3e-9f6b-2a7d1e0c9b84",
        "seq": 30,
        "server_ts": "2026-09-29T12:00:00.000Z",
        "round_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "round_number": 1,
        "state": _round_start_state(),
    }
    parsed = RoundStart.from_message(round_start)
    assert parsed.hand_number == 1 and parsed.hole_cards == []

    result = TurnResult.from_message(_one(lambda b: b.get("type") == "turn_result"))
    assert result.action == "kick_low" and result.amount == 0


class _FighterCallBot(ChipzenBot):
    """Answers each fighter call with a legal-shaped action, from the SDK as shipped."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[GameState] = []

    def decide(self, state: GameState) -> Action:
        self.seen.append(state)
        if state.valid_actions == ["choose_body"]:
            return Action(action="choose_body", params={"body": "golem"})
        if state.valid_actions == ["bid"]:
            return Action(action="bid", params={"amount": 0})
        if state.valid_actions == ["fit"]:
            return Action(action="fit", params={"fit": {"0": "capacitor"}})
        return Action(action="idle")


def _session_script() -> list[dict]:
    match_id = _requests()["act"]["match_id"]
    envelope = {"match_id": match_id, "server_ts": "2026-09-29T12:00:00.000Z"}
    seq = iter(range(1, 100))

    def msg(kind: str, **fields) -> dict:
        return {"type": kind, **envelope, "seq": next(seq), **fields}

    requests = _requests()
    return [
        msg(
            "hello",
            supported_versions=["1.0"],
            selected_version="1.0",
            game_type="fighter",
            capabilities=["reconnect"],
            game=_descriptor(),
        ),
        msg(
            "match_start",
            seats=[
                {"seat": 0, "participant_id": "p_a", "display_name": "A", "is_self": False},
                {"seat": 1, "participant_id": "p_b", "display_name": "B", "is_self": True},
            ],
            game_config=_game_config(),
            turn_timeout_ms=1000,
        ),
        requests["choose_body"],
        msg("phase_change", state={"phase": "auction", "board": []}),
        requests["bid"],
        msg("phase_change", state={"phase": "fitting", "board": []}),
        requests["fit"],
        msg("phase_change", state={"phase": "fight", "board": []}),
        msg(
            "round_start",
            round_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            round_number=1,
            state=_round_start_state(),
        ),
        requests["act"],
        _one(lambda b: b.get("type") == "turn_result"),
        msg("action_timeout", request_id="req_00030", auto_action="block"),
        msg(
            "round_result",
            round_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            round_number=1,
            result=_one(lambda b: "winner_seats" in b),
        ),
        msg(
            "match_end",
            reason="complete",
            results=[
                {"seat": 0, "participant_id": "p_a", "rank": 2, "score": 0},
                {"seat": 1, "participant_id": "p_b", "rank": 1, "score": 2},
            ],
        ),
    ]


@pytest.mark.asyncio
async def test_a_stock_client_declaring_fighter_plays_every_published_message():
    """The issue's acceptance test: a stock client that declares ``fighter``
    parses a fighter ``turn_request`` without throwing. ``safe_mode=False``,
    so any exception inside parsing, ``decide()`` or a hook fails the test
    instead of being swallowed."""
    script = _session_script()
    ws = _MockWebSocket(script)
    bot = _FighterCallBot()
    end = await _run_session(
        ws,
        bot,
        match_id=script[0]["match_id"],
        token="t",
        ticket=None,
        client_name="fighter-doc-test",
        client_version="0.0.0",
        safe_mode=False,
        supported_games=["fighter"],
    )
    assert end is not None and end["type"] == "match_end"

    sent = [json.loads(frame) for frame in ws.sent]
    hello = next(frame for frame in sent if frame["type"] == "hello")
    assert hello["supported_games"] == ["fighter"]

    answers = [frame for frame in sent if frame["type"] == "turn_action"]
    requests = _requests()
    assert [a["request_id"] for a in answers] == [
        requests[call]["request_id"] for call in ("choose_body", "bid", "fit", "act")
    ]
    for answer in answers:
        assert set(answer) <= TURN_ACTION_KEYS
    assert answers[0]["params"] == {"body": "golem"}
    assert answers[1]["params"] == {"amount": 0}
    assert answers[2]["params"] == {"fit": {"0": "capacitor"}}
    assert answers[3]["action"] == "idle" and answers[3]["params"] == {}

    assert [s.phase for s in bot.seen] == ["body_shop", "auction", "fitting", "fight"]
