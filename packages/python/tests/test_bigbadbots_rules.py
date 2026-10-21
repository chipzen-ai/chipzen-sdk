"""Versioned Big Bad Bots rule sets: ``state.rules`` and the bundled files (chipzen-sdk#144).

* the bundled files hold only published numbers, and agree with
  ``docs/RULESETS.md`` (ids, engines, dates, sha256) and with the protocol
  doc's ``game_config`` example;
* ``RuleSet`` reads ``match_start.game_config`` into a typed, read-only view;
* the same bot runs unchanged on two rule sets with different numbers, and
  every phase's state carries the match's rule set;
* an unknown ``rule_set_id``, or a value that differs from the published one,
  logs a warning and the bot plays on the server's numbers.
"""

import copy
import dataclasses
import json
import logging
import re
from pathlib import Path

import pytest
from test_bigbadbots_bot import (
    MATCH_ID,
    _descriptor,
    _game_config,
    _match_end,
    _match_start,
    _phase_change,
    _play,
    _request,
    _round_start,
    _sent_actions,
    _server_hello,
)

from chipzen.bigbadbots import (
    AuctionState,
    BodyShopState,
    FightState,
    FittingState,
    StateParseError,
    parse_bigbadbots_turn_request,
)
from chipzen.bigbadbots_bot import BigBadBotsBot
from chipzen.bigbadbots_rules import (
    RULESET_DIR,
    RULESETS_URL,
    PublishedRuleSet,
    RuleSet,
    published_rule_set,
    published_rule_set_ids,
    rule_set_warnings,
)
from chipzen.models import Action

REPO = Path(__file__).resolve().parents[3]
RULESETS_MD = REPO / "docs" / "RULESETS.md"
PUBLISHED_SECTIONS = {"gearup", "arena", "rounds", "forfeit", "deadline"}


def _published_table(rule_set_id: str) -> dict:
    """The bundled published sections of a rule set, in wire form, plus the
    other sections a real table has (with made-up numbers)."""
    entry = json.loads((RULESET_DIR / f"{rule_set_id}.json").read_text(encoding="utf-8"))
    return {**entry["rule_set"], "moves": {"punch_high": {"startup": 2, "damage": 5}}}


def _config(rule_set_id: str, rule_set: dict) -> dict:
    return {
        "variant": "bigbadbots",
        "rule_set_id": rule_set_id,
        "num_players": 2,
        "rule_set": rule_set,
    }


def _match_start_with(config: dict) -> dict:
    return {**_match_start(), "game_config": config}


def _session(config: dict) -> list[dict]:
    return [
        _server_hello(_descriptor()),
        _match_start_with(config),
        _request("choose_body"),
        _phase_change("auction"),
        _request("bid"),
        _phase_change("fitting"),
        _request("fit"),
        _phase_change("fight"),
        _round_start(),
        _request("act"),
        _match_end(),
    ]


def _rule_set_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "rule set" in r.getMessage()]


class RulesReader(BigBadBotsBot):
    """Reads every number it uses from ``state.rules``; never changes between rule sets."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, type, str | None]] = []

    def _see(self, call: str, state) -> None:
        self.seen.append((call, type(state), state.rules.id if state.rules else None))

    def choose_body(self, state: BodyShopState) -> Action:
        self._see("choose_body", state)
        return super().choose_body(state)

    def bid(self, state: AuctionState) -> Action:
        self._see("bid", state)
        share = state.rules.gearup.budget // (2 * state.rules.gearup.auction_lots)
        return Action.bid(min(share, state.me.credits))

    def fit(self, state: FittingState) -> Action:
        self._see("fit", state)
        return super().fit(state)

    def act(self, state: FightState) -> Action:
        self._see("act", state)
        # Walls that close on a stall punish waiting; without them, block.
        return Action.act("move_in" if state.rules.rounds.closing_walls else "block")


# ---------------------------------------------------------------------------
# The bundled published rule sets
# ---------------------------------------------------------------------------


def _changelog() -> dict[str, str]:
    """RULESETS.md section 7, one entry per rule-set id."""
    text = RULESETS_MD.read_text(encoding="utf-8").split("## 7. Changelog", 1)[1]
    text = text.split("\n## ", 1)[0]
    parts = re.split(r"^### `([^`]+)`.*$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def test_bundles_every_released_bigbadbots_rule_set_and_nothing_else():
    released = [i for i in _changelog() if i.startswith("bigbadbots-")]
    assert published_rule_set_ids() == tuple(sorted(released))
    assert published_rule_set_ids() == ("bigbadbots-1.0", "bigbadbots-1.1", "bigbadbots-1.2")
    assert sorted(p.stem for p in RULESET_DIR.glob("*.json")) == list(published_rule_set_ids())


@pytest.mark.parametrize("rule_set_id", ["bigbadbots-1.0", "bigbadbots-1.1", "bigbadbots-1.2"])
def test_bundled_metadata_matches_the_changelog(rule_set_id):
    entry = published_rule_set(rule_set_id)
    assert isinstance(entry, PublishedRuleSet)
    text = _changelog()[rule_set_id]
    assert f"`{entry.sha256}`" in text
    assert f"engine `{entry.engine}`" in text
    assert f"on {entry.frozen_on}." in text
    assert f"**State:** `{entry.state}`" in text


@pytest.mark.parametrize("rule_set_id", ["bigbadbots-1.0", "bigbadbots-1.1", "bigbadbots-1.2"])
def test_bundled_files_hold_only_the_published_sections(rule_set_id):
    entry = published_rule_set(rule_set_id)
    assert set(entry.sections) == PUBLISHED_SECTIONS
    # And they are complete enough to read as a rule set.
    assert RuleSet.from_rule_set(rule_set_id, _thawed(entry)).gearup.budget == 1000


def _thawed(entry: PublishedRuleSet) -> dict:
    return json.loads((RULESET_DIR / f"{entry.id}.json").read_text(encoding="utf-8"))["rule_set"]


def test_bundled_1_0_is_the_protocol_docs_published_game_config():
    """Drift guard: the protocol doc's section 4.1 example is rule set 1.0's
    published sections, verbatim."""
    published = _game_config()
    assert published["rule_set_id"] == "bigbadbots-1.0"
    assert _thawed(published_rule_set("bigbadbots-1.0")) == published["rule_set"]


def test_bundled_1_1_and_1_2_follow_the_changelog():
    base = _thawed(published_rule_set("bigbadbots-1.0"))
    one_one = _thawed(published_rule_set("bigbadbots-1.1"))
    one_two = _thawed(published_rule_set("bigbadbots-1.2"))
    walls = {"stall_close_after": 50, "stall_close_rate": 0.06, "stall_min_width": 2.0}
    assert one_one["rounds"] == {**base["rounds"], **walls, "timeout_tiebreak": "damage"}
    assert one_two["rounds"] == {**base["rounds"], "timeout_tiebreak": "damage"}
    for other in ("gearup", "arena", "forfeit", "deadline"):
        assert one_one[other] == one_two[other] == base[other]


def test_an_unknown_id_has_no_published_entry():
    assert published_rule_set("bigbadbots-9.0") is None
    assert published_rule_set("bigbadbots-1.1-dev") is None
    assert published_rule_set("fighter-1.0") is None


def test_published_entries_are_read_only():
    entry = published_rule_set("bigbadbots-1.2")
    with pytest.raises(TypeError):
        entry.sections["gearup"]["budget"] = 5000  # type: ignore[index]
    with pytest.raises(AttributeError):
        entry.state = "live"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RuleSet: the typed, read-only view of match_start's table
# ---------------------------------------------------------------------------


def test_rule_set_reads_the_published_game_config():
    config = _game_config()
    rules = RuleSet.from_rule_set(config["rule_set_id"], config["rule_set"])
    assert rules.id == "bigbadbots-1.0"
    assert rules.gearup.budget == 1000
    assert rules.gearup.auction_lots == 6
    assert rules.arena.start_x == (4.0, 8.0)
    assert rules.arena.min_separation == 1.15
    assert rules.rounds.round_ticks == 600
    assert rules.rounds.timeout_tiebreak == "hp_pct"  # left out in 1.0
    assert not rules.rounds.closing_walls
    assert rules.rounds.stall_close_after is None
    assert rules.forfeit.rounds_for_match == 2
    assert rules.deadline.deadline_ms == 100
    assert rules.deadline.miss_counts_toward_forfeit is True
    assert rules["gearup"]["budget"] == 1000
    assert "arena" in rules and "nope" not in rules
    assert rules.to_dict() == config["rule_set"]
    assert rules.published is published_rule_set("bigbadbots-1.0")


def test_rule_set_reads_closing_walls_and_the_damage_tiebreak():
    rules = RuleSet.from_rule_set("bigbadbots-1.1", _published_table("bigbadbots-1.1"))
    assert rules.rounds.closing_walls
    assert rules.rounds.stall_close_after == 50
    assert rules.rounds.stall_close_rate == 0.06
    assert rules.rounds.stall_min_width == 2.0
    assert rules.rounds.timeout_tiebreak == "damage"
    assert rules["moves"]["punch_high"]["startup"] == 2


def test_rule_set_is_read_only_and_does_not_alias_the_wire_table():
    table = _published_table("bigbadbots-1.2")
    rules = RuleSet.from_rule_set("bigbadbots-1.2", table)
    with pytest.raises(TypeError):
        rules.params["gearup"]["budget"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        rules["moves"]["punch_high"] = {}
    with pytest.raises(AttributeError):
        rules.id = "bigbadbots-2.0"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        rules.gearup.budget = 1  # type: ignore[misc]
    table["gearup"]["budget"] = 1
    assert rules.gearup.budget == 1000
    assert rules["gearup"]["budget"] == 1000


def test_rule_set_with_a_mistyped_or_missing_section_fails_loudly():
    table = _published_table("bigbadbots-1.2")
    table["deadline"] = {**table["deadline"], "deadline_ms": 100.0}
    with pytest.raises(StateParseError, match="deadline_ms"):
        RuleSet.from_rule_set("bigbadbots-1.2", table)
    del table["forfeit"]
    with pytest.raises(StateParseError, match="forfeit"):
        RuleSet.from_rule_set("bigbadbots-1.2", table)


def test_no_warnings_for_a_known_rule_set_with_its_published_numbers():
    for rule_set_id in published_rule_set_ids():
        rules = RuleSet.from_rule_set(rule_set_id, _published_table(rule_set_id))
        assert rule_set_warnings(rules) == []


def test_unknown_rule_set_warning_names_it_and_links_the_spec():
    rules = RuleSet.from_rule_set("bigbadbots-9.0", _published_table("bigbadbots-1.2"))
    (warning,) = rule_set_warnings(rules)
    assert "'bigbadbots-9.0'" in warning
    assert "bigbadbots-1.2" in warning  # what it does know
    assert RULESETS_URL in warning


def test_a_value_that_differs_from_the_published_one_is_reported():
    table = _published_table("bigbadbots-1.2")
    table["deadline"]["deadline_ms"] = 80
    rules = RuleSet.from_rule_set("bigbadbots-1.2", table)
    (warning,) = rule_set_warnings(rules)
    assert "rule_set.deadline.deadline_ms is 80 (published: 100)" in warning
    # The server's value is the one played.
    assert rules.deadline.deadline_ms == 80


def test_a_published_key_the_server_left_out_is_reported():
    table = _published_table("bigbadbots-1.1")
    del table["rounds"]["stall_close_after"]
    rules = RuleSet.from_rule_set("bigbadbots-1.1", table)
    (warning,) = rule_set_warnings(rules)
    assert "rule_set.rounds.stall_close_after is missing (published: 50)" in warning
    assert not rules.rounds.closing_walls


# ---------------------------------------------------------------------------
# state.rules, end to end through the session loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule_set_id", "expected_act"),
    [("bigbadbots-1.1", "move_in"), ("bigbadbots-1.2", "block")],
)
async def test_the_same_bot_runs_unchanged_on_two_rule_sets(rule_set_id, expected_act, caplog):
    bot = RulesReader()
    with caplog.at_level(logging.WARNING, logger="chipzen"):
        ws, _ = await _play(bot, _session(_config(rule_set_id, _published_table(rule_set_id))))

    assert _rule_set_warnings(caplog) == []
    assert bot.rules is not None and bot.rules.id == rule_set_id
    # Every phase's state carries the match's rule set.
    assert bot.seen == [
        ("choose_body", BodyShopState, rule_set_id),
        ("bid", AuctionState, rule_set_id),
        ("fit", FittingState, rule_set_id),
        ("act", FightState, rule_set_id),
    ]
    assert _sent_actions(ws) == ["choose_body", "bid", "fit", expected_act]


@pytest.mark.asyncio
async def test_different_numbers_change_the_answers_not_the_code(caplog):
    """Two test rule sets that differ only in their numbers: the bot's bid
    follows the budget each one sends."""
    bids = {}
    for rule_set_id, budget in (("bigbadbots-1.1-test", 1200), ("bigbadbots-1.2-test", 600)):
        table = _published_table("bigbadbots-1.2")
        table["gearup"]["budget"] = budget
        ws, _ = await _play(RulesReader(), _session(_config(rule_set_id, table)))
        (bid,) = [m for m in ws.of_type("turn_action") if m["action"] == "bid"]
        bids[rule_set_id] = bid["params"]["amount"]
    assert bids["bigbadbots-1.1-test"] != bids["bigbadbots-1.2-test"]


@pytest.mark.asyncio
async def test_an_unknown_rule_set_warns_and_plays_on_the_servers_numbers(caplog):
    table = _published_table("bigbadbots-1.2")
    table["rounds"]["stall_close_after"] = 30  # new in the unknown version
    table["deadline"]["deadline_ms"] = 75
    bot = RulesReader()
    with caplog.at_level(logging.WARNING, logger="chipzen"):
        ws, end = await _play(bot, _session(_config("bigbadbots-1.3", table)))

    assert end == _match_end()
    (warning,) = _rule_set_warnings(caplog)
    assert "'bigbadbots-1.3' is not one this SDK release knows" in warning
    # Never substituted: the bot plays the numbers match_start sent.
    assert bot.rules.id == "bigbadbots-1.3"
    assert bot.rules.published is None
    assert bot.rules.deadline.deadline_ms == 75
    assert bot.rules.rounds.closing_walls
    assert len(bot.seen) == 4
    assert _sent_actions(ws) == ["choose_body", "bid", "fit", "move_in"]


@pytest.mark.asyncio
async def test_a_known_rule_set_with_other_numbers_warns_and_plays_the_servers(caplog):
    table = _published_table("bigbadbots-1.2")
    table["gearup"]["budget"] = 1500
    bot = RulesReader()
    with caplog.at_level(logging.WARNING, logger="chipzen"):
        ws, _ = await _play(bot, _session(_config("bigbadbots-1.2", table)))

    (warning,) = _rule_set_warnings(caplog)
    assert "rule_set.gearup.budget is 1500 (published: 1000)" in warning
    assert bot.rules.gearup.budget == 1500
    assert len(_sent_actions(ws)) == 4


@pytest.mark.asyncio
async def test_an_unreadable_section_leaves_rules_none_and_the_bot_plays_on(caplog):
    class RawReader(BigBadBotsBot):
        def act(self, state: FightState) -> Action:
            assert state.rules is None
            return Action.act("idle")

    table = _published_table("bigbadbots-1.2")
    del table["forfeit"]
    bot = RawReader()
    with caplog.at_level(logging.WARNING, logger="chipzen"):
        ws, _ = await _play(bot, _session(_config("bigbadbots-1.2", table)))

    assert any("could not be read into state.rules" in m for m in _rule_set_warnings(caplog))
    assert bot.rules is None
    assert bot.game_config is not None and "forfeit" not in bot.game_config.rule_set
    assert _sent_actions(ws)[-1] == "idle"


@pytest.mark.asyncio
async def test_reconnect_relearns_the_rule_set():
    reconnected = {
        "type": "reconnected",
        "match_id": MATCH_ID,
        "round_number": 1,
        "seats": _match_start()["seats"],
        "game_config": _config("bigbadbots-1.1", _published_table("bigbadbots-1.1")),
        "pending_request": _request("act"),
    }
    bot = RulesReader()
    ws, _ = await _play(bot, [_server_hello(_descriptor()), reconnected, _match_end()])
    assert bot.seen == [("act", FightState, "bigbadbots-1.1")]
    assert _sent_actions(ws) == ["move_in"]


def test_decide_without_match_start_leaves_rules_none():
    bot = RulesReader()
    state = parse_bigbadbots_turn_request(_request("choose_body"))
    bot.decide(state)
    assert bot.seen == [("choose_body", BodyShopState, None)]


def test_rules_are_not_part_of_the_wire_state_or_equality():
    bot = RulesReader()
    bot.on_match_start({"game_config": _game_config()})
    request = _request("act")
    plain = parse_bigbadbots_turn_request(copy.deepcopy(request))
    bot.decide(plain)
    assert plain.rules is None  # the parsed object is not changed in place
    with_rules = dataclasses.replace(plain, rules=bot.rules)
    assert with_rules == plain
    assert with_rules.to_state() == request["state"]
    assert "rules" not in repr(with_rules)
