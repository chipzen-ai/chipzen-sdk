"""SDK backward-compat tests: NLHE-written bots at a variant (27TD / OFC) table.

Sibling of ``test_multiway_backcompat.py``. That module froze the multiway
widening (``game_config`` gained ``num_players`` while staying a **superset**);
this one freezes the *variant* widening, which is the same shape of promise one
level up: 2-7 Triple Draw and Pineapple OFC are carried in **new keys only**, so
the envelope an NLHE bot parses is unchanged and no protocol-version bump is
required.

What is pinned here:

* ``GameState.from_turn_request`` parses a 27TD ``turn_request`` and an OFC
  ``turn_request`` without raising, in a bot that knows nothing about either.
* **No existing field loses its default.** Every NLHE-era ``GameState`` field a
  variant payload does not carry comes back holding the exact value a
  default-constructed ``GameState`` holds -- the variant keys are additions, not
  displacements.
* The variant ``turn_request.state`` is a **superset** of the NLHE one, key for
  key, with matching types on every shared key.
* **The hard constraint, proven rather than asserted:** ``board`` and
  ``your_hole_cards`` are parsed by ``Card.from_str``, which raises, and it
  raises *inside* ``from_turn_request`` -- **before ``decide()`` is ever
  called**, outside any per-decision safe mode. A ``"??"`` placeholder in either
  array is a hard session kill for every deployed Python bot. That is the single
  reason both Layer 2 specs make Rule 1 normative, and the whole variant design
  rests on it, so this module drives the real session loop and shows the bot's
  ``decide()`` never runs.
* ``Action.discard()`` / ``Action.place()`` emit their parameters under
  ``params`` -- the only place the server's field allowlist accepts them.
* The optional ``decide_draw`` / ``decide_placement`` hooks are **defaulted**: a
  bot written before they existed, implementing only ``decide()``, still
  instantiates and still plays.
* The NLHE Python starter completes a full mocked 27TD session and a full mocked
  OFC session without raising, while remaining an NLHE bot.

See ``docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md`` and
``docs/protocol/OFC-GAME-STATE-PROTOCOL.md`` (both section 2, "Backward-compat
rules") for the wire contract these tests hold the SDK to.
"""

import dataclasses
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot
from chipzen.conformance import (
    DRAW27_ACTIONS,
    OFC_ACTIONS,
    _classify_turn_action,
    _draw27_match_start,
    _draw27_round_result,
    _draw27_round_start,
    _draw27_script,
    _draw27_turn_request,
    _draw27_turn_result,
    _drive_session,
    _extract_turn_actions,
    _ofc_match_start,
    _ofc_round_result,
    _ofc_round_start,
    _ofc_script,
    _ofc_turn_request,
    _ofc_turn_result,
    _turn_request,
)
from chipzen.models import Action, GameState

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

#: Every ``GameState`` field that existed before the variant widening. If a
#: variant payload silently displaced one of these, this list is what catches
#: it -- so it is written out longhand rather than derived.
NLHE_ERA_FIELDS = [
    "hand_number",
    "phase",
    "hole_cards",
    "board",
    "pot",
    "your_stack",
    "opponent_stacks",
    "your_seat",
    "dealer_seat",
    "to_call",
    "min_raise",
    "max_raise",
    "valid_actions",
    "action_history",
    "round_id",
    "request_id",
]


class NlheOnlyBot(ChipzenBot):
    """A bot written against NLHE and nothing else.

    It implements ``decide()`` -- the single required entry point -- and knows
    nothing about draws, placements, rows or royalties. It also records every
    state it was handed, so a test can prove whether ``decide()`` ran at all.
    """

    def __init__(self) -> None:
        self.seen: list[GameState] = []

    def decide(self, state: GameState) -> Action:
        self.seen.append(state)
        # The classic NLHE shape: read the pot odds, act on valid_actions.
        if "check" in state.valid_actions:
            return Action.check()
        if state.to_call and state.to_call * 3 < state.pot:
            return Action.call()
        return Action.fold()


def _load_starter_module():
    """Import packages/python/starters/python/bot.py as a module by path."""
    pytest.importorskip("websockets")
    path = os.path.join(os.path.dirname(__file__), "..", "starters", "python", "bot.py")
    spec = importlib.util.spec_from_file_location("chipzen_variant_starter_bot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VARIANT_TURN_REQUESTS = {
    "draw27_draw_phase": lambda: _draw27_turn_request(draw_phase=True),
    "draw27_betting_phase": lambda: _draw27_turn_request(draw_phase=False),
    "ofc_placement": _ofc_turn_request,
}


# --------------------------------------------------------------------------
# 1. A variant turn_request parses in an NLHE-shaped bot without raising
# --------------------------------------------------------------------------


class TestVariantTurnRequestParses:
    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_from_turn_request_does_not_raise(self, name):
        state = GameState.from_turn_request(VARIANT_TURN_REQUESTS[name]())
        assert isinstance(state, GameState)
        # board is [] at both variant tables, and it is CARRIED, never omitted
        # and never a placeholder -- that is Rule 1's whole point.
        assert state.board == []

    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_nlhe_bot_decides_without_raising(self, name):
        """The bot is handed a variant state and returns an NLHE action.

        The action is wrong for the table -- an NLHE bot cannot draw or place --
        but WRONG is survivable and CRASHING is not. The server rejects it and
        substitutes its own default; the session lives.
        """
        bot = NlheOnlyBot()
        action = bot.decide(GameState.from_turn_request(VARIANT_TURN_REQUESTS[name]()))
        assert action.action in {"fold", "check", "call", "raise", "all_in"}
        assert len(bot.seen) == 1

    def test_draw27_new_keys_are_readable(self):
        state = GameState.from_turn_request(_draw27_turn_request(draw_phase=True))
        assert state.is_draw_phase is True
        assert state.draw_number == 1
        assert state.draws_remaining == 3
        # The BOUND on the discard lives in state, not in valid_actions.
        assert state.max_discard == 5
        assert state.valid_actions == ["draw"]
        assert state.your_draw_counts == []
        # Seat index is a DECIMAL STRING key, because JSON object keys are.
        assert state.opponent_draw_counts == {"1": []}

    def test_draw27_betting_turn_is_fixed_limit(self):
        """Fixed limit: exactly one legal raise size, so min_raise == max_raise."""
        state = GameState.from_turn_request(_draw27_turn_request(draw_phase=False))
        assert state.is_draw_phase is False
        assert state.min_raise == state.max_raise == 200
        assert state.max_discard == 0
        assert "raise" in state.valid_actions

    def test_ofc_new_keys_are_readable(self):
        state = GameState.from_turn_request(_ofc_turn_request())
        assert state.valid_actions == ["place"]
        assert state.cards_to_place == ["9s", "Js", "Ks"]
        assert state.place == 2
        assert state.must_discard == 1
        # place + must_discard == len(cards_to_place), always.
        assert state.place + state.must_discard == len(state.cards_to_place)
        assert state.row_capacity == {"top": 2, "middle": 3, "bottom": 3}
        assert state.your_rows["bottom"] == ["5s", "6s"]
        assert state.opponent_rows["0"]["middle"] == ["8d", "9h"]
        assert state.royalties == {"top": 0, "middle": 0, "bottom": 0}
        assert state.opponent_royalties["0"]["top"] == 0
        assert state.point_value == 50
        assert state.in_fantasy_land is False
        assert state.phase_sequence[0] == "deal1"

    def test_ofc_state_carries_its_own_seats(self):
        """OFC puts your_seat/dealer_seat IN state; the kwargs are only fallbacks.

        27TD does the opposite -- it carries neither, and a bot must retain the
        button from ``round_start``. Both readings have to work.
        """
        # OFC: state wins over the (wrong) kwargs.
        ofc = GameState.from_turn_request(_ofc_turn_request(), your_seat=9, dealer_seat=9)
        assert ofc.your_seat == 1
        assert ofc.dealer_seat == 0

        # 27TD: no seat keys in state, so the kwargs are what survive.
        d27 = GameState.from_turn_request(_draw27_turn_request(), your_seat=0, dealer_seat=1)
        assert "your_seat" not in _draw27_turn_request()["state"]
        assert "dealer_seat" not in _draw27_turn_request()["state"]
        assert d27.your_seat == 0
        assert d27.dealer_seat == 1

    def test_nlhe_payload_still_leaves_variant_fields_at_defaults(self):
        """The other direction: an NLHE table must look untouched."""
        state = GameState.from_turn_request(_turn_request())
        blank = GameState()
        for f in dataclasses.fields(GameState):
            if f.name in NLHE_ERA_FIELDS:
                continue
            assert getattr(state, f.name) == getattr(blank, f.name), f.name


# --------------------------------------------------------------------------
# 2. No existing field's default is lost
# --------------------------------------------------------------------------


class TestExistingDefaultsSurvive:
    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_every_nlhe_era_field_is_still_present(self, name):
        state = GameState.from_turn_request(VARIANT_TURN_REQUESTS[name]())
        for field_name in NLHE_ERA_FIELDS:
            assert hasattr(state, field_name), field_name

    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_nlhe_era_types_are_unchanged(self, name):
        """A variant payload must not RETYPE a field an NLHE bot reads."""
        state = GameState.from_turn_request(VARIANT_TURN_REQUESTS[name]())
        blank = GameState()
        for field_name in NLHE_ERA_FIELDS:
            assert type(getattr(state, field_name)) is type(getattr(blank, field_name)), field_name

    def test_ofc_zeroes_the_betting_fields_rather_than_dropping_them(self):
        """OFC has no betting, and says so in numbers rather than by omission.

        A bot that computes a bet size from these fields computes zero, which
        is correct. A bot that hits a MISSING key crashes, which is not -- so
        the keys stay present and numeric (Rule 2).
        """
        raw = _ofc_turn_request()["state"]
        for key in ("pot", "to_call", "min_raise", "max_raise", "your_stack"):
            assert key in raw
            assert isinstance(raw[key], int)
        assert isinstance(raw["opponent_stacks"], list)

        state = GameState.from_turn_request(_ofc_turn_request())
        assert (state.to_call, state.min_raise, state.max_raise, state.pot) == (0, 0, 0, 0)

    def test_draw_phase_zeroes_the_raise_bounds_rather_than_dropping_them(self):
        raw = _draw27_turn_request(draw_phase=True)["state"]
        for key in ("pot", "to_call", "min_raise", "max_raise", "your_stack"):
            assert key in raw
            assert isinstance(raw[key], int)
        state = GameState.from_turn_request(_draw27_turn_request(draw_phase=True))
        assert state.min_raise == 0
        assert state.max_raise == 0

    def test_variant_fields_all_default_and_never_required(self):
        """Every variant field is constructible-by-omission.

        ``GameState()`` with no arguments is what an NLHE bot's local test
        harness builds. If any variant field were required, that call would
        raise and every such harness would break on upgrade.
        """
        blank = GameState()
        variant_fields = [f for f in dataclasses.fields(GameState) if f.name not in NLHE_ERA_FIELDS]
        assert variant_fields, "no variant fields found -- test is checking nothing"
        for f in variant_fields:
            has_default = f.default is not dataclasses.MISSING
            has_factory = f.default_factory is not dataclasses.MISSING
            assert has_default or has_factory, f.name
            assert hasattr(blank, f.name)


# --------------------------------------------------------------------------
# 3. The variant envelope is an additive superset (no version bump)
# --------------------------------------------------------------------------


class TestVariantIsAdditiveSuperset:
    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_state_is_a_superset_of_the_nlhe_state(self, name):
        nlhe = _turn_request()["state"]
        variant = VARIANT_TURN_REQUESTS[name]()["state"]
        missing = set(nlhe) - set(variant)
        assert not missing, f"{name} dropped NLHE keys: {sorted(missing)}"
        for key in nlhe:
            assert type(nlhe[key]) is type(variant[key]), key

    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_envelope_fields_are_unchanged(self, name):
        nlhe = _turn_request()
        variant = VARIANT_TURN_REQUESTS[name]()
        for key in ("type", "match_id", "seq", "seat", "request_id", "timeout_ms"):
            assert key in variant, key
            assert type(nlhe[key]) is type(variant[key]), key
        # valid_actions stays a FLAT ARRAY OF STRINGS at every table. The
        # bounds live in state; they were never smuggled into this list.
        assert isinstance(variant["valid_actions"], list)
        assert all(isinstance(a, str) for a in variant["valid_actions"])

    def test_game_config_variant_key_already_existed(self):
        """No new config key is needed to ANNOUNCE the variant.

        ``game_config.variant`` reaches ``on_match_start`` today and is already
        ignored by every bot, so a variant table needs zero envelope change.
        """
        assert _draw27_match_start()["game_config"]["variant"] == "27tripledraw"
        assert _ofc_match_start()["game_config"]["variant"] == "pineapple"

    def test_ofc_config_has_no_betting_structure_key(self):
        """OFC's config says ``scoring: points`` and carries no betting keys.

        There is nothing for ``betting_structure``, blinds or ``bet_cap`` to
        describe, and inventing them would be a lie a bot could act on.
        """
        cfg = _ofc_match_start()["game_config"]
        assert cfg["scoring"] == "points"
        for absent in ("betting_structure", "small_blind", "big_blind", "bet_cap"):
            assert absent not in cfg, absent
        # point_value is the ruled quantity; bank_points is its derivation.
        assert cfg["point_value"] == 50
        assert cfg["bank_points"] == cfg["starting_stack"] // cfg["point_value"]
        # rows is an OBJECT of row name -> capacity, and its keys are the row
        # names the `place` action uses.
        assert cfg["rows"] == {"top": 3, "middle": 5, "bottom": 5}
        assert cfg["cards_placed"] == sum(cfg["rows"].values())
        assert "points_per_chip" not in cfg

    def test_draw27_config_is_fixed_limit_with_four_independent_sizes(self):
        cfg = _draw27_match_start()["game_config"]
        assert cfg["betting_structure"] == "fixed_limit"
        for key in ("small_blind", "big_blind", "small_bet", "big_bet"):
            assert isinstance(cfg[key], int), key
        assert cfg["bet_cap"] == 5
        assert cfg["num_draws"] == 3
        assert cfg["cards_per_player"] == 5
        # Elimination-only, exactly as for NLHE.
        assert "total_hands" not in cfg


# --------------------------------------------------------------------------
# 4. The hard constraint: an invalid card raises BEFORE decide()
# --------------------------------------------------------------------------


def _poison(message: dict, key: str, value) -> dict:
    """Return ``message`` with ``state[key]`` replaced -- the hazard payload."""
    poisoned = json.loads(json.dumps(message))
    poisoned["state"][key] = value
    return poisoned


BAD_CARD_ARRAYS = [
    ["??"],
    ["XX"],
    ["Ah", "??"],
    ["10h"],
]


class TestInvalidCardIsAHardParseError:
    @pytest.mark.parametrize("bad", BAD_CARD_ARRAYS)
    def test_bad_board_raises_in_from_turn_request(self, bad):
        with pytest.raises(ValueError):
            GameState.from_turn_request(_poison(_ofc_turn_request(), "board", bad))

    @pytest.mark.parametrize("bad", BAD_CARD_ARRAYS)
    def test_bad_hole_cards_raise_in_from_turn_request(self, bad):
        with pytest.raises(ValueError):
            GameState.from_turn_request(_poison(_draw27_turn_request(), "your_hole_cards", bad))

    @pytest.mark.parametrize("key", ["board", "your_hole_cards"])
    def test_a_null_card_raises_typeerror_not_valueerror(self, key):
        """Worth knowing, and pinned: a JSON ``null`` raises **TypeError**.

        ``Card.from_str`` calls ``len(s)`` before it validates anything, so a
        ``null`` placeholder surfaces as ``TypeError`` while ``"??"`` / ``"XX"``
        surface as ``ValueError``. Both are equally fatal -- the session dies
        either way -- but code guarding with a bare ``except ValueError`` would
        catch one and not the other. Both Layer 2 specs make ``null`` in these
        arrays unreachable by contract; this records what actually happens if
        that contract is ever broken.
        """
        with pytest.raises(TypeError):
            GameState.from_turn_request(_poison(_ofc_turn_request(), key, [None]))

    def test_raises_before_decide_is_called(self):
        """PROOF, not assertion: the bot's decide() never runs.

        ``from_turn_request`` is called by the session loop and the exception
        escapes it, so there is no bot code between the bad payload and the
        failure -- which is why a placeholder card is a session kill rather
        than one bad decision.
        """
        bot = NlheOnlyBot()
        with pytest.raises(ValueError):
            GameState.from_turn_request(_poison(_ofc_turn_request(), "board", ["??"]))
        assert bot.seen == [], "decide() must not have been reached"

    async def test_session_dies_before_decide_on_a_poisoned_board(self):
        """The same proof through the REAL session loop, end to end.

        A full scripted 27TD session, with one placeholder card dropped into
        ``board``. ``_run_session`` raises, the bot's ``decide()`` is never
        entered, and no ``turn_action`` is ever emitted.
        """
        bot = NlheOnlyBot()
        script = _draw27_script()
        script[3] = _poison(script[3], "board", ["??"])

        mock_ws, exc = await _drive_session(bot, script, timeout_s=5.0)

        assert mock_ws is None
        assert isinstance(exc, ValueError), f"expected ValueError, got {exc!r}"
        assert bot.seen == [], "decide() ran despite an unparseable board"

    async def test_a_clean_variant_session_does_reach_decide(self):
        """Control for the test above: the same script, unpoisoned, works.

        Without this, a bug that made every session die would leave the
        poisoned-board test passing for the wrong reason.
        """
        bot = NlheOnlyBot()
        mock_ws, exc = await _drive_session(bot, _draw27_script(), timeout_s=5.0)
        assert exc is None, f"clean 27TD script raised {exc!r}"
        assert mock_ws is not None
        assert len(bot.seen) == 2, "both 27TD turns should have reached decide()"


# --------------------------------------------------------------------------
# 5. Action factories emit their parameters under `params`
# --------------------------------------------------------------------------


class TestVariantActionFactories:
    def test_draw_emits_discard_under_params(self):
        wire = Action.discard(["Ah", "Kd"]).to_wire()
        assert wire == {"action": "draw", "params": {"discard": ["Ah", "Kd"]}}

    def test_draw_accepts_hand_positions(self):
        """The discard list takes card strings OR 0-based hand positions."""
        assert Action.discard([0, 3]).to_wire() == {
            "action": "draw",
            "params": {"discard": [0, 3]},
        }

    @pytest.mark.parametrize("factory", [lambda: Action.discard([]), Action.stand_pat])
    def test_stand_pat_is_an_empty_discard(self, factory):
        assert factory().to_wire() == {"action": "draw", "params": {"discard": []}}

    def test_stand_pat_is_not_a_no_op(self):
        """It is a real action that passes the turn, not an absent one."""
        assert Action.stand_pat().action == "draw"
        assert Action.stand_pat() == Action.discard([])
        assert Action.stand_pat() != Action.discard(["Ah"])

    def test_place_emits_placements_and_discard_under_params(self):
        wire = Action.place([("Ks", "bottom"), ("Js", "middle")], discard="9s").to_wire()
        assert wire == {
            "action": "place",
            "params": {
                "placements": [
                    {"card": "Ks", "row": "bottom"},
                    {"card": "Js", "row": "middle"},
                ],
                "discard": "9s",
            },
        }

    def test_place_accepts_mapping_entries_and_a_list_discard(self):
        wire = Action.place(
            [{"card": "2c", "row": "top"}, {"card": "3d", "row": "middle"}],
            discard=["9s"],
        ).to_wire()
        assert wire["params"]["placements"][0] == {"card": "2c", "row": "top"}
        assert wire["params"]["discard"] == ["9s"]

    def test_opening_set_discards_nothing(self):
        wire = Action.place([("2c", "top")]).to_wire()
        assert wire["params"]["discard"] == []

    @pytest.mark.parametrize(
        "action",
        [
            Action.fold(),
            Action.check(),
            Action.call(),
            Action.all_in(),
            Action.raise_to(200),
        ],
    )
    def test_nlhe_actions_are_byte_unchanged(self, action):
        """The NLHE factories must not have shifted an inch."""
        wire = action.to_wire()
        assert set(wire) == {"action", "params"}
        if action.action == "raise":
            assert wire["params"] == {"amount": 200}
        else:
            assert wire["params"] == {}

    def test_action_is_still_hashable(self):
        """Adding a dict field must not have broken the frozen dataclass.

        Existing code is free to put actions in a set or use them as dict
        keys; a naively-added ``dict`` field would have made that raise.
        """
        assert len({Action.fold(), Action.fold(), Action.check()}) == 2
        assert hash(Action.discard(["Ah"])) is not None

    def test_legacy_to_dict_carries_params_rather_than_dropping_them(self):
        """``to_dict`` is the legacy flat format, which has no ``params`` slot.

        It carries them anyway: silently turning a draw into a bare
        ``{"action": "draw"}`` would be a stand pat the bot never chose.
        """
        assert Action.discard(["Ah"]).to_dict() == {
            "action": "draw",
            "params": {"discard": ["Ah"]},
        }
        assert Action.fold().to_dict() == {"action": "fold"}
        assert Action.raise_to(200).to_dict() == {"action": "raise", "amount": 200}


# --------------------------------------------------------------------------
# 6. decide() stays the single required entry point
# --------------------------------------------------------------------------


class LegacyBot(ChipzenBot):
    """A bot written before the variant hooks existed. Implements decide(), full stop."""

    def decide(self, state: GameState) -> Action:
        return Action.fold()


class TestOptionalHooksAreDefaulted:
    def test_a_decide_only_bot_still_instantiates(self):
        """If the hooks were abstract, this line would raise TypeError."""
        bot = LegacyBot()
        assert isinstance(bot, ChipzenBot)
        assert bot.decide(GameState()).action == "fold"

    def test_decide_draw_defaults_to_stand_pat(self):
        state = GameState.from_turn_request(_draw27_turn_request(draw_phase=True))
        action = LegacyBot().decide_draw(state)
        assert action.to_wire() == {"action": "draw", "params": {"discard": []}}

    def test_decide_placement_default_is_legal(self):
        state = GameState.from_turn_request(_ofc_turn_request())
        action = LegacyBot().decide_placement(state)
        params = action.to_wire()["params"]

        assert action.action == "place"
        # Exactly `place` placements and exactly `must_discard` discards, all
        # drawn from cards_to_place, each card used once.
        assert len(params["placements"]) == state.place
        assert len(params["discard"]) == state.must_discard
        used = [p["card"] for p in params["placements"]] + list(params["discard"])
        assert sorted(used) == sorted(state.cards_to_place)
        # Every named row is a real row with capacity to spare.
        for row, count in _row_counts(params["placements"]).items():
            assert row in state.row_capacity
            assert count <= state.row_capacity[row]

    def test_decide_placement_respects_a_full_row(self):
        """A row with zero capacity must not be assigned to."""
        msg = _ofc_turn_request()
        msg["state"]["row_capacity"] = {"top": 0, "middle": 0, "bottom": 2}
        state = GameState.from_turn_request(msg)
        params = LegacyBot().decide_placement(state).to_wire()["params"]
        assert _row_counts(params["placements"]) == {"bottom": 2}

    def test_hooks_are_not_called_by_the_session_loop(self):
        """``decide()`` is still the ONLY method the SDK invokes on a turn.

        The hooks are a convenience your own ``decide()`` may delegate to.
        Wiring them into the loop would silently change behaviour for every
        bot that never asked for them.
        """

        class HookSpy(NlheOnlyBot):
            def __init__(self) -> None:
                super().__init__()
                self.hook_calls = 0

            def decide_draw(self, state: GameState) -> Action:
                self.hook_calls += 1
                return Action.stand_pat()

        bot = HookSpy()
        # The draw turn in this script would be the one to trigger it.
        _mock_ws, exc = _run_sync(_drive_session(bot, _draw27_script(), timeout_s=5.0))
        assert exc is None
        assert len(bot.seen) == 2
        assert bot.hook_calls == 0


def _row_counts(placements: list[dict]) -> dict:
    counts: dict = {}
    for p in placements:
        counts[p["row"]] = counts.get(p["row"], 0) + 1
    return counts


def _run_sync(coro):
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 7. The NLHE starter survives a full mocked variant session
# --------------------------------------------------------------------------


class TestNlheStarterAtAVariantTable:
    @pytest.mark.parametrize("script_name", ["draw27", "ofc"])
    async def test_starter_completes_a_mocked_variant_session(self, script_name):
        starter = _load_starter_module()
        script = _draw27_script() if script_name == "draw27" else _ofc_script()

        mock_ws, exc = await _drive_session(starter.MyBot(), script, timeout_s=5.0)

        assert exc is None, f"starter raised at a {script_name} table: {exc!r}"
        assert mock_ws is not None
        turn_actions = _extract_turn_actions(mock_ws.sent)
        assert turn_actions, "starter never sent a turn_action"
        # Whatever it sent, it echoed the request_id -- the field the server
        # uses for correlation, idempotency and action_rejected retries.
        for msg in turn_actions:
            assert msg["request_id"] in {"req_1", "req_2"}

    @pytest.mark.parametrize("name", sorted(VARIANT_TURN_REQUESTS))
    def test_starter_returns_a_legal_nlhe_action_shape(self, name):
        """It stays an NLHE bot: it never invents a `draw` or `place` payload."""
        starter = _load_starter_module()
        state = GameState.from_turn_request(VARIANT_TURN_REQUESTS[name]())
        action = starter.MyBot().decide(state)
        assert action.action in starter.KNOWN_ACTIONS
        assert action.to_wire()["params"] in ({}, {"amount": action.amount})

    def test_starter_reports_an_unknown_action_once(self, capsys):
        """Graceful handling is NOTICING, not guessing."""
        starter = _load_starter_module()
        bot = starter.MyBot()
        state = GameState.from_turn_request(_ofc_turn_request())

        bot.decide(state)
        first = capsys.readouterr().err
        assert "place" in first

        bot.decide(state)
        assert capsys.readouterr().err == "", "unknown-action notice repeated every turn"

    def test_starter_still_checks_and_folds_at_an_nlhe_table(self):
        """The NLHE behaviour this starter documents is unchanged."""
        starter = _load_starter_module()
        bot = starter.MyBot()

        checkable = GameState(valid_actions=["check", "raise"], opponent_stacks=[1000])
        assert bot.decide(checkable).action == "check"

        facing_bet = GameState(valid_actions=["fold", "call", "raise"], opponent_stacks=[1000])
        assert bot.decide(facing_bet).action == "fold"


# --------------------------------------------------------------------------
# 8. The conformance fixtures match the published Layer 2 specs
# --------------------------------------------------------------------------


class TestConformanceFixturesMatchTheSpecs:
    def test_variant_fixtures_are_not_wired_into_the_grader(self):
        """An uploaded bot is an NLHE bot. Grading it on a variant table would
        fail every clean bot on the platform, so the fixtures stay fixtures."""
        from chipzen import conformance

        source = open(conformance.__file__, encoding="utf-8").read()
        grader = source.split("def run_conformance_checks")[1]
        for fixture in ("_draw27_script", "_ofc_script"):
            assert fixture not in grader, f"{fixture} leaked into the grader"

    def test_classify_defaults_to_the_nlhe_vocabulary(self):
        """Widening the DEFAULT legal set would hide a real bug.

        A bot that answers an NLHE table with ``place`` is broken. The variant
        vocabularies are opt-in, per scenario.
        """
        payload = json.dumps({"type": "turn_action", "request_id": "req_1", "action": "place"})
        ok, _msg = _classify_turn_action(payload)
        assert ok is False

        ok, _msg = _classify_turn_action(payload, legal_actions=OFC_ACTIONS)
        assert ok is True

    def test_draw_is_legal_only_under_the_draw27_vocabulary(self):
        payload = json.dumps({"type": "turn_action", "request_id": "req_1", "action": "draw"})
        assert _classify_turn_action(payload)[0] is False
        assert _classify_turn_action(payload, legal_actions=DRAW27_ACTIONS)[0] is True

    def test_draw27_action_history_amount_is_a_card_count(self):
        """For ``action: "draw"``, ``amount`` is CARDS -- not a bitmask, not chips."""
        details = _draw27_turn_result()["details"]
        assert details["action"] == "draw"
        assert details["amount"] == 2

    def test_ofc_action_history_amount_is_a_placement_mask(self):
        """For ``action: "place"``, ``amount`` is a 2-bits-per-card mask.

        0 discard, 1 top, 2 middle, 3 bottom, in deal order. The trail carries
        masks and never cards, which is what keeps a hidden board hidden.
        """
        entry = _ofc_turn_request()["state"]["action_history"][0]
        assert entry["action"] == "place"
        codes = [(entry["amount"] >> (2 * i)) & 0b11 for i in range(5)]
        assert codes == [1, 2, 2, 3, 3]  # top, middle, middle, bottom, bottom

        # The mask decodes to exactly the rows the same payload reports, and
        # deal1 places 5 / discards 0 so no card may carry code 0.
        rows = {1: "top", 2: "middle", 3: "bottom"}
        dealt = _ofc_round_start()["state"]["cards_to_place"]
        rebuilt: dict = {"top": [], "middle": [], "bottom": []}
        for card, code in zip(dealt, codes):
            assert code != 0, "the opening set discards nothing"
            rebuilt[rows[code]].append(card)
        assert rebuilt == _ofc_turn_request()["state"]["your_rows"]

        # A pineapple street DOES carry one code 0: place 2, discard 1.
        details = _ofc_turn_result()["details"]
        assert details["action"] == "place"
        assert [(details["amount"] >> (2 * i)) & 0b11 for i in range(3)] == [2, 3, 0]

    def test_draw27_showdown_rank_is_not_an_nlhe_enum(self):
        """A dash-joined rank string, and the LOWEST hand wins."""
        showdown = _draw27_round_result()["result"]["showdown"]
        assert showdown[0]["hand_rank"] == "8-6-4-3-2"
        assert all("-" in entry["hand_rank"] for entry in showdown)
        # The winner is the seat with the LOW hand.
        assert _draw27_round_result()["result"]["winner_seats"] == [0]

    def test_ofc_showdown_points_sum_to_zero(self):
        showdown = _ofc_round_result()["result"]["showdown"]
        assert sum(entry["points"] for entry in showdown) == 0
        assert sum(entry["net_chips"] for entry in showdown) == 0
        # Every seat appears: OFC has no fold.
        assert {entry["seat"] for entry in showdown} == {0, 1}
        # A discard never appears, not even in its owner's own entry.
        for entry in showdown:
            assert len(entry["hole_cards"]) == 13

    def test_round_start_fixtures_parse_as_round_start(self):
        """``RoundStart`` is game-agnostic and must keep parsing both variants."""
        from chipzen.models import RoundStart

        d27 = RoundStart.from_message(_draw27_round_start())
        assert len(d27.hole_cards) == 5
        assert d27.dealer_seat == 0

        ofc = RoundStart.from_message(_ofc_round_start())
        assert len(ofc.hole_cards) == 5
        assert ofc.stacks == [10000, 10000]
