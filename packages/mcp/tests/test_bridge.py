"""Tests for the push->pull bridge (TurnRegistry + BridgeBot)."""

import threading
import time

from chipzen import Action, GameState

from chipzen_mcp.bridge import BridgeBot, TurnRegistry, TurnSnapshot, state_payload

MATCH = "m-1"


def _snapshot(match_id: str = MATCH, deadline_offset: float = 30.0) -> TurnSnapshot:
    now = time.time()
    return TurnSnapshot(
        match_id=match_id,
        request_id="req-1",
        published_at=now,
        deadline_at=now + deadline_offset,
        state={"hand_number": 1, "valid_actions": ["check", "raise"]},
    )


def _turn_state(**overrides: object) -> GameState:
    defaults: dict = {
        "hand_number": 3,
        "phase": "flop",
        "pot": 60,
        "your_stack": 970,
        "opponent_stacks": [970],
        "to_call": 0,
        "min_raise": 10,
        "max_raise": 970,
        "valid_actions": ["check", "raise", "all_in"],
        "request_id": "req-42",
    }
    defaults.update(overrides)
    return GameState(**defaults)


class TestTurnRegistry:
    def test_wait_times_out_when_idle(self) -> None:
        registry = TurnRegistry()
        assert registry.wait_for_any_turn(0.05) is None

    def test_publish_then_wait_returns_snapshot(self) -> None:
        registry = TurnRegistry()
        registry.publish_turn(_snapshot())
        got = registry.wait_for_any_turn(0.0)
        assert got is not None and got.match_id == MATCH

    def test_wait_unblocks_on_cross_thread_publish(self) -> None:
        registry = TurnRegistry()
        threading.Timer(0.05, registry.publish_turn, args=(_snapshot(),)).start()
        got = registry.wait_for_any_turn(2.0)
        assert got is not None and got.match_id == MATCH

    def test_earliest_deadline_wins_across_matches(self) -> None:
        registry = TurnRegistry()
        registry.publish_turn(_snapshot("m-slow", deadline_offset=50.0))
        registry.publish_turn(_snapshot("m-urgent", deadline_offset=5.0))
        got = registry.wait_for_any_turn(0.0)
        assert got is not None and got.match_id == "m-urgent"

    def test_submit_action_resolves_pending(self) -> None:
        registry = TurnRegistry()
        pending = registry.publish_turn(_snapshot())
        assert registry.submit_action(MATCH, Action.check()) is True
        assert pending.event.is_set()
        assert pending.action is not None and pending.action.action == "check"
        # Consumed: nothing pending any more.
        assert registry.wait_for_any_turn(0.0) is None
        assert registry.submit_action(MATCH, Action.check()) is False

    def test_submit_action_unknown_match(self) -> None:
        assert TurnRegistry().submit_action("nope", Action.fold()) is False

    def test_match_views_and_results(self) -> None:
        registry = TurnRegistry()
        registry.match_started(MATCH, rated=False)
        registry.publish_turn(_snapshot())
        view = registry.get_match(MATCH)
        assert view is not None and view["my_turn"] is True and view["rated"] is False
        assert view["turn"] is not None and view["turn"]["request_id"] == "req-1"

        registry.record_round_result(MATCH, {"hand_number": 1, "winner_seats": [0]})
        registry.record_match_end(MATCH, {"reason": "completed"})
        view = registry.get_match(MATCH)
        assert view is not None and view["finished"] is True and view["my_turn"] is False

        last = registry.last_result(MATCH)
        assert last is not None and last["match_end"] == {"reason": "completed"}
        assert registry.get_match("unknown") is None
        assert registry.status()["finished_matches"] == 1


class TestBridgeBot:
    def _started_bot(self, registry: TurnRegistry, timeout_ms: int, margin_ms: int) -> BridgeBot:
        bot = BridgeBot(registry, safety_margin_ms=margin_ms)
        bot.on_match_start({"match_id": MATCH, "turn_timeout_ms": timeout_ms})
        return bot

    def test_decide_returns_submitted_action(self) -> None:
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=5000, margin_ms=0)

        def answer() -> None:
            got = registry.wait_for_any_turn(2.0)
            assert got is not None
            registry.submit_action(got.match_id, Action.raise_to(60))

        answerer = threading.Thread(target=answer)
        answerer.start()
        action = bot.decide(_turn_state())
        answerer.join()
        assert action.action == "raise" and action.amount == 60

    def test_decide_times_out_to_check_when_legal(self) -> None:
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=50, margin_ms=0)
        action = bot.decide(_turn_state(valid_actions=["check", "raise"]))
        assert action.action == "check"
        # The stale pending turn must be cleared so the agent can't answer it.
        assert registry.wait_for_any_turn(0.0) is None

    def test_decide_times_out_to_fold_otherwise(self) -> None:
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=50, margin_ms=0)
        action = bot.decide(_turn_state(valid_actions=["fold", "call"], to_call=40))
        assert action.action == "fold"

    def test_lifecycle_hooks_record_results(self) -> None:
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=5000, margin_ms=0)
        bot.on_round_result({"result": {"hand_number": 2, "winner_seats": [1]}})
        bot.on_match_end({"reason": "completed", "results": []})
        last = registry.last_result(MATCH)
        assert last is not None and last["match_end"] is not None


def test_state_payload_mirrors_wire_schema() -> None:
    payload = state_payload(_turn_state())
    assert payload["phase"] == "flop"
    assert payload["valid_actions"] == ["check", "raise", "all_in"]
    assert payload["to_call"] == 0
    # Wire-schema field names, not SDK-internal ones.
    assert "your_hole_cards" in payload and "board" in payload
