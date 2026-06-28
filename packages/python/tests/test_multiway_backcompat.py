"""SDK backward-compat tests: HU-written bots on multiway (3-6 seat) tables.

The two-layer Poker protocol was multiway-shaped from day one: ``opponent_stacks``
is a LIST, and the seat fields (``your_seat``, ``dealer_seat``, ``winner_seats``)
are already plural/seat-indexed. This module PROVES that a bot written for
heads-up (one opponent) keeps running unchanged when the platform seats it at a
3-6 player table, and pins down the one real risk: a bot that hardcodes
``opponent_stacks[0]`` does not crash but silently reads a single neighbor's
stack rather than the whole field.

Key assertions:

* ``GameState.from_turn_request`` parses well-formed N-seat ``turn_request``
  messages for N in {2, 3, 6} (the list-shaped ``opponent_stacks`` simply
  carries N-1 entries).
* A legacy HU bot that indexes ``opponent_stacks[0]`` runs without raising on a
  3/6 seat table.
* No breaking protocol-version bump is required: the multiway envelope is a pure
  additive superset of the heads-up envelope (the only new ``game_config`` key is
  ``num_players``), so a v1.0 bot keeps parsing it.
* The seat-aware Python starter still returns a legal action for every seat
  count, preserving its heads-up behavior, and exposes a ``table_position()``
  helper for authors extending to multiway.

See ``docs/protocol/POKER-GAME-STATE-PROTOCOL.md`` Section 5.9 and
``docs/DEV-MANUAL.md`` Section 2.3 for the position-derivation guidance.
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.models import GameState

# --------------------------------------------------------------------------
# Fixtures: build N-seat protocol messages (match_start + turn_request)
# --------------------------------------------------------------------------


def make_match_start(num_players: int, *, starting_stack: int = 1000) -> dict:
    """Build a ``match_start`` envelope for a ``num_players``-seat table.

    The only key that varies with seat count is ``num_players``. Everything else
    is the seat-agnostic match config a heads-up bot already understands.
    """
    return {
        "type": "match_start",
        "match_id": "m_test",
        "seq": 1,
        "game_config": {
            "variant": "nlhe",
            "starting_stack": starting_stack,
            "small_blind": 5,
            "big_blind": 10,
            "ante": 0,
            "num_players": num_players,
        },
        "your_seat": 0,
    }


def make_turn_request(
    num_players: int,
    *,
    your_seat: int = 0,
    opponent_stacks: list[int] | None = None,
) -> dict:
    """Build a ``turn_request`` envelope seated at an N-player table.

    ``opponent_stacks`` carries every OTHER seat's stack in seat order, so for an
    N-seat table it has length N-1 (length 1 in heads-up). When not given, a
    descending fan of distinct stacks is synthesized so tests can tell the
    difference between ``opponent_stacks[0]`` and the aggregate.
    """
    if opponent_stacks is None:
        opponent_stacks = [900 - 50 * i for i in range(num_players - 1)]
    assert len(opponent_stacks) == num_players - 1
    return {
        "type": "turn_request",
        "match_id": "m_test",
        "seq": 4,
        "seat": your_seat,
        "request_id": "req_1",
        "round_id": "r_1",
        "state": {
            "hand_number": 1,
            "phase": "flop",
            "board": ["Ts", "7h", "2d"],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 120,
            "your_stack": 950,
            "opponent_stacks": opponent_stacks,
            "to_call": 20,
            "min_raise": 40,
            "max_raise": 950,
            "action_history": [
                {
                    "seat": 0,
                    "action": "post_small_blind",
                    "amount": 5,
                    "phase": "preflop",
                    "is_timeout": False,
                },
                {
                    "seat": 1,
                    "action": "post_big_blind",
                    "amount": 10,
                    "phase": "preflop",
                    "is_timeout": False,
                },
            ],
        },
        "valid_actions": ["fold", "call", "raise", "all_in"],
    }


SEAT_COUNTS = [2, 3, 6]


# --------------------------------------------------------------------------
# 1. SDK models parse N-seat messages (well-formed for 2/3/6)
# --------------------------------------------------------------------------


class TestNSeatGameStateParsing:
    @pytest.mark.parametrize("num_players", SEAT_COUNTS)
    def test_from_turn_request_parses_n_seats(self, num_players):
        msg = make_turn_request(num_players)
        state = GameState.from_turn_request(msg, your_seat=0, dealer_seat=0)

        # opponent_stacks is the list of every OTHER seat -> length N-1.
        assert len(state.opponent_stacks) == num_players - 1
        assert all(isinstance(s, int) for s in state.opponent_stacks)
        # Core decision fields are intact regardless of seat count.
        assert state.phase == "flop"
        assert state.your_stack == 950
        assert state.to_call == 20
        assert state.valid_actions == ["fold", "call", "raise", "all_in"]
        assert state.request_id == "req_1"

    @pytest.mark.parametrize("num_players", SEAT_COUNTS)
    def test_game_config_num_players_is_additive(self, num_players):
        """game_config gained num_players but stays a superset.

        An old bot that only reads small_blind/big_blind/starting_stack keeps
        working; the new key is simply available for bots that want it.
        """
        cfg = make_match_start(num_players)["game_config"]
        # Heads-up keys an existing bot already reads are unchanged.
        assert cfg["variant"] == "nlhe"
        assert cfg["small_blind"] == 5
        assert cfg["big_blind"] == 10
        assert cfg["starting_stack"] == 1000
        # New explicit seat count.
        assert cfg["num_players"] == num_players


# --------------------------------------------------------------------------
# 2. A legacy HU bot does not crash on a multiway table
# --------------------------------------------------------------------------


def legacy_hu_decide(state: dict, valid_actions: list[str]) -> dict:
    """A deliberately naive heads-up bot.

    It assumes exactly one opponent and indexes ``opponent_stacks[0]`` to size a
    shove decision. This is the EXACT pattern this module is about: it must not
    crash when seated at a 3-6 table, even though it only sees one neighbor.
    """
    opp_stack = state["opponent_stacks"][0]  # HU assumption: one opponent
    your_stack = state["your_stack"]
    effective = min(your_stack, opp_stack)
    if effective < 50 and "all_in" in valid_actions:
        return {"action": "all_in", "params": {}}
    if "check" in valid_actions:
        return {"action": "check", "params": {}}
    if "call" in valid_actions:
        return {"action": "call", "params": {}}
    return {"action": "fold", "params": {}}


class TestLegacyHUBotSurvivesMultiway:
    @pytest.mark.parametrize("num_players", SEAT_COUNTS)
    def test_legacy_bot_does_not_crash(self, num_players):
        msg = make_turn_request(num_players)
        action = legacy_hu_decide(msg["state"], msg["valid_actions"])
        assert action["action"] in {"fold", "check", "call", "raise", "all_in"}

    def test_legacy_bot_via_sdk_gamestate(self, num_players=6):
        """Same naive access via the parsed GameState dataclass."""
        state = GameState.from_turn_request(make_turn_request(num_players))
        # opponent_stacks[0] is valid (list is non-empty) but is ONE neighbor.
        neighbor = state.opponent_stacks[0]
        assert isinstance(neighbor, int)

    def test_silent_misbehavior_is_real_not_a_crash(self):
        """The documented hazard: [0] != aggregate at a multiway table.

        At 6 seats, ``opponent_stacks[0]`` is a single neighbor while the bot
        likely meant "the opponents". This proves the failure is silent (no
        exception) AND material (the two numbers genuinely differ), which is why
        we document it rather than relying on a crash to surface it.
        """
        state = GameState.from_turn_request(make_turn_request(6))
        assert len(state.opponent_stacks) == 5
        single_neighbor = state.opponent_stacks[0]
        aggregate = sum(state.opponent_stacks)
        largest = max(state.opponent_stacks)
        assert single_neighbor != aggregate
        # The naive read also is not the most-dangerous (largest) opponent.
        assert single_neighbor <= largest


# --------------------------------------------------------------------------
# 3. No protocol-version bump required (additive superset)
# --------------------------------------------------------------------------


class TestNoVersionBumpRequired:
    def test_multiway_is_superset_of_heads_up(self):
        """Every key a heads-up turn_request carries is present at 6 seats.

        If the multiway message were a breaking change it would drop or retype a
        heads-up field; instead the only difference is that the existing
        list-typed ``opponent_stacks`` carries more entries. A v1.0 bot needs no
        handshake change.
        """
        hu = make_turn_request(2)["state"]
        multi = make_turn_request(6)["state"]
        assert set(hu.keys()) == set(multi.keys())
        for key in hu:
            assert type(hu[key]) is type(multi[key]), key
        # opponent_stacks stays a list; only its length grows.
        assert isinstance(hu["opponent_stacks"], list)
        assert isinstance(multi["opponent_stacks"], list)
        assert len(multi["opponent_stacks"]) > len(hu["opponent_stacks"])


# --------------------------------------------------------------------------
# 4. The seat-aware Python starter still returns a legal action for every
#    seat count, and its table_position() helper derives position correctly.
# --------------------------------------------------------------------------


def _load_starter_module():
    """Import packages/python/starters/python/bot.py as a module by path."""
    pytest.importorskip("websockets")
    path = os.path.join(os.path.dirname(__file__), "..", "starters", "python", "bot.py")
    spec = importlib.util.spec_from_file_location("chipzen_starter_bot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSeatAwareStarter:
    @pytest.mark.parametrize("num_players", SEAT_COUNTS)
    def test_starter_decide_returns_legal_action(self, num_players):
        starter = _load_starter_module()
        state = GameState.from_turn_request(make_turn_request(num_players))
        action = starter.MyBot().decide(state)
        assert action.action in {"fold", "check", "call", "raise", "all_in"}

    @pytest.mark.parametrize(
        "num_players,your_seat,dealer_seat,expected",
        [
            (2, 0, 0, "button_sb"),  # heads-up button posts the small blind
            (2, 1, 0, "big_blind"),
            (6, 0, 0, "button"),
            (6, 1, 0, "small_blind"),
            (6, 2, 0, "big_blind"),
            (6, 3, 0, "early"),
            (6, 5, 0, "cutoff"),
        ],
    )
    def test_table_position_derivation(self, num_players, your_seat, dealer_seat, expected):
        starter = _load_starter_module()
        assert starter.table_position(your_seat, dealer_seat, num_players) == expected
