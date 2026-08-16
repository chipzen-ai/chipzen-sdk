"""Tests for the push->pull bridge (TurnRegistry, BridgeBot, session lifecycle)."""

import asyncio
import inspect
import logging
import threading
import time
from pathlib import Path

import pytest
from chipzen import Action, GameState

from chipzen_mcp.bridge import (
    LOBBY_CONNECTED,
    LOBBY_ENDED,
    LOBBY_EVICTED,
    LOBBY_RECONNECTING,
    LOBBY_STARTING,
    MATCH_CONN_ABANDONED,
    MATCH_CONN_CONNECTED,
    MATCH_CONN_PENDING,
    MATCH_CONN_RECONNECTING,
    MAX_FINISHED_MATCHES,
    SDK_LOGGER_NAME,
    SUBMIT_ACCEPTED,
    SUBMIT_NO_PENDING_TURN,
    SUBMIT_STALE_TURN,
    BridgeBot,
    ExternalSession,
    SdkLogTap,
    SessionPresence,
    TurnRegistry,
    TurnSnapshot,
    state_payload,
)
from chipzen_mcp.config import McpConfig

MATCH = "m-1"

CONFIG = McpConfig(token="cz_extbot_x", bot_id="b-1", env="staging")


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    """Poll ``predicate`` until true or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _snapshot(
    match_id: str = MATCH, deadline_offset: float = 30.0, request_id: str = "req-1"
) -> TurnSnapshot:
    now = time.time()
    return TurnSnapshot(
        match_id=match_id,
        request_id=request_id,
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
        assert registry.submit_action(MATCH, Action.check()) == SUBMIT_ACCEPTED
        assert pending.event.is_set()
        assert pending.action is not None and pending.action.action == "check"
        # Consumed: nothing pending any more.
        assert registry.wait_for_any_turn(0.0) is None
        assert registry.submit_action(MATCH, Action.check()) == SUBMIT_NO_PENDING_TURN

    def test_submit_action_unknown_match(self) -> None:
        assert TurnRegistry().submit_action("nope", Action.fold()) == SUBMIT_NO_PENDING_TURN

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
        assert registry.status()["finished_matches"] == 1

        last = registry.last_result(MATCH)
        assert last is not None and last["match_end"] == {"reason": "completed"}
        assert registry.get_match("unknown") is None
        # #3939: an explicit read of a FINISHED match consumes it -- the record
        # is evicted right after the outcome is returned.
        assert registry.get_match(MATCH) is None
        assert registry.status()["finished_matches"] == 0

    def test_last_result_no_arg_returns_most_recent_across_matches(self) -> None:
        # chipzen-ai/Chipzen#3884: with no match_id, the result that landed
        # most recently wins -- NOT the first match in insertion order. Give
        # the OLDER-created match (A) the newest result and it must surface.
        registry = TurnRegistry()
        registry.match_started("A")
        registry.match_started("B")
        registry.record_round_result("B", {"hand_number": 5, "which": "B-older"})
        registry.record_round_result("A", {"hand_number": 9, "which": "A-newest"})
        last = registry.last_result()
        assert last is not None
        assert last["match_id"] == "A"
        assert last["last_round_result"]["which"] == "A-newest"

    def test_last_result_recency_counts_match_end_too(self) -> None:
        # Recency spans BOTH result kinds: a later round result on one match
        # must beat an earlier match_end on another, and vice versa.
        registry = TurnRegistry()
        registry.match_started("X")
        registry.match_started("Y")
        registry.record_round_result("X", {"which": "X-round"})
        registry.record_match_end("Y", {"reason": "Y-ended"})
        # X then produces the most-recent result of the three events.
        registry.record_round_result("X", {"which": "X-latest"})
        assert registry.last_result()["match_id"] == "X"
        # A newer match_end on Y now overtakes X.
        registry.record_match_end("Y", {"reason": "Y-ended-later"})
        latest = registry.last_result()
        assert latest["match_id"] == "Y"
        assert latest["match_end"] == {"reason": "Y-ended-later"}

    def test_last_result_unknown_match_id_is_no_result_not_fallback(self) -> None:
        # chipzen-ai/Chipzen#3884 (repro B): a match_id the registry has never
        # seen must return None (surfaced as no_results_yet), never another
        # match's data. A typo'd id previously fell through to an all-matches
        # scan and reported an unrelated match.
        registry = TurnRegistry()
        registry.match_started("real-match")
        registry.record_round_result("real-match", {"hand_number": 7, "which": "real"})
        assert registry.last_result("TYPO-does-not-exist") is None

    def test_last_result_known_match_without_result_is_none(self) -> None:
        # A match that exists but has produced no result yet returns None
        # (not some other match's result).
        registry = TurnRegistry()
        registry.match_started("A")
        registry.record_round_result("A", {"which": "A"})
        registry.match_started("B")  # exists, no result yet
        assert registry.last_result("B") is None

    def test_match_connection_state_tracking(self) -> None:
        registry = TurnRegistry()
        registry.match_started(MATCH)
        assert registry.list_matches()[0]["connection"] == MATCH_CONN_PENDING

        registry.set_match_connection(MATCH, MATCH_CONN_CONNECTED)
        assert registry.list_matches()[0]["connection"] == MATCH_CONN_CONNECTED

        registry.set_match_connection(MATCH, MATCH_CONN_RECONNECTING, attempt=2)
        summary = registry.list_matches()[0]
        assert summary["connection"] == MATCH_CONN_RECONNECTING
        assert summary["reconnect_attempt"] == 2

        registry.set_match_connection(MATCH, MATCH_CONN_ABANDONED)
        summary = registry.list_matches()[0]
        assert summary["connection"] == MATCH_CONN_ABANDONED
        assert summary["reconnect_attempt"] is None


class TestStaleTurnGuard:
    """chipzen-ai/Chipzen#3906: an act must not land on a turn it never saw.

    ``submit_action`` used to be keyed on ``match_id`` alone, so an action that
    arrived after the bridge's local fallback cleared turn A was applied to the
    NEXT turn B of the same match -- and reported success. Quoting the turn's
    ``request_id`` makes that a refusal instead.
    """

    def test_late_act_for_cleared_turn_cannot_land_on_the_next_turn(self) -> None:
        # The issue's deterministic 4-step repro.
        registry = TurnRegistry()
        # 1. publish turn A; the agent observes request_id "A"
        pending_a = registry.publish_turn(_snapshot(request_id="A"))
        observed = registry.wait_for_any_turn(0.0)
        assert observed is not None and observed.request_id == "A"
        # 2. the bridge's decision budget elapses -> local fallback
        registry.clear_pending(MATCH)
        # 3. the hand advances -> turn B is published on the same match
        pending_b = registry.publish_turn(_snapshot(request_id="B"))
        # 4. the agent's late act for A must be REFUSED...
        assert (
            registry.submit_action(MATCH, Action.raise_to(60), request_id="A") == SUBMIT_STALE_TURN
        )
        # ...and turn B must not have received it (this is the actual defect).
        assert pending_b.event.is_set() is False
        assert pending_b.action is None
        assert pending_a.event.is_set() is False
        # B is still pending and still answerable by quoting ITS id.
        assert registry.submit_action(MATCH, Action.check(), request_id="B") == SUBMIT_ACCEPTED
        assert pending_b.action is not None and pending_b.action.action == "check"

    def test_matching_request_id_is_accepted(self) -> None:
        registry = TurnRegistry()
        pending = registry.publish_turn(_snapshot(request_id="r-7"))
        assert registry.submit_action(MATCH, Action.call(), request_id="r-7") == SUBMIT_ACCEPTED
        assert pending.action is not None and pending.action.action == "call"

    def test_stale_id_on_a_match_with_nothing_pending_is_no_pending_turn(self) -> None:
        # No turn to be stale against -- the existing no_pending_turn verdict
        # already says "nothing is awaiting you".
        registry = TurnRegistry()
        registry.match_started(MATCH)
        assert (
            registry.submit_action(MATCH, Action.fold(), request_id="A") == SUBMIT_NO_PENDING_TURN
        )

    def test_omitting_request_id_keeps_legacy_behaviour(self) -> None:
        # Backward compatibility contract: agents that never pass request_id
        # still answer whatever is pending (the pre-#3906, unsafe path).
        registry = TurnRegistry()
        registry.publish_turn(_snapshot(request_id="A"))
        registry.clear_pending(MATCH)
        pending_b = registry.publish_turn(_snapshot(request_id="B"))
        assert registry.submit_action(MATCH, Action.check()) == SUBMIT_ACCEPTED
        assert pending_b.action is not None

    def test_pending_request_id_reports_the_live_turn(self) -> None:
        registry = TurnRegistry()
        assert registry.pending_request_id(MATCH) is None  # unknown match
        registry.publish_turn(_snapshot(request_id="live-1"))
        assert registry.pending_request_id(MATCH) == "live-1"
        registry.clear_pending(MATCH)
        assert registry.pending_request_id(MATCH) is None


class TestRegistryClose:
    """chipzen-ai/Chipzen#3900: shutdown must resolve pending turns."""

    def test_close_cancels_pending_and_unblocks_decide(self) -> None:
        registry = TurnRegistry()
        pending = registry.publish_turn(_snapshot())
        assert registry.close() == 1
        assert pending.cancelled is True and pending.event.is_set()
        # A cancelled turn is NOT answerable and carries no action -- nothing
        # that could be mistaken for a real decision reaches the SDK.
        assert pending.action is None
        assert registry.submit_action(MATCH, Action.check()) == SUBMIT_NO_PENDING_TURN
        assert registry.status()["pending_turns"] == 0

    def test_close_is_idempotent(self) -> None:
        registry = TurnRegistry()
        registry.publish_turn(_snapshot())
        assert registry.close() == 1
        assert registry.close() == 0

    def test_turn_published_after_close_is_born_cancelled(self) -> None:
        # A turn_request landing mid-teardown must not park the session thread
        # on an event nobody will set.
        registry = TurnRegistry()
        registry.close()
        pending = registry.publish_turn(_snapshot())
        assert pending.cancelled is True and pending.event.is_set()
        assert registry.status()["pending_turns"] == 0

    def test_waiters_return_immediately_when_closed(self) -> None:
        registry = TurnRegistry()
        registry.close()
        started = time.monotonic()
        assert registry.wait_for_any_turn(5.0) is None
        assert time.monotonic() - started < 1.0

    def test_reopen_puts_the_registry_back_in_service(self) -> None:
        registry = TurnRegistry()
        registry.close()
        registry.reopen()
        pending = registry.publish_turn(_snapshot())
        assert pending.cancelled is False
        assert registry.wait_for_any_turn(0.0) is not None


class TestTurnRegistryEviction:
    """chipzen-ai/Chipzen#3939: TurnRegistry._matches must be bounded.

    Two complementary mechanisms keep a long-lived multi-tabling session from
    accumulating finished-match records forever: consumption eviction (an
    explicit get_last_result read drops the finished record) and a cap sweep
    (never-consumed terminals beyond MAX_FINISHED_MATCHES are evicted, oldest
    result first). Active matches are never touched by either.
    """

    def test_explicit_read_of_finished_match_evicts_it(self) -> None:
        registry = TurnRegistry()
        registry.match_started("m")
        registry.record_match_end("m", {"reason": "done"})
        # First explicit read collects the outcome...
        first = registry.last_result("m")
        assert first is not None and first["match_end"] == {"reason": "done"}
        # ...and consumes it: the record is gone.
        assert registry.get_match("m") is None
        assert registry.last_result("m") is None
        assert registry.status()["finished_matches"] == 0

    def test_explicit_read_of_active_match_does_not_evict(self) -> None:
        # A match with only a round result is still in flight -- reading it must
        # NOT evict (the match keeps playing and will produce more turns).
        registry = TurnRegistry()
        registry.match_started("m")
        registry.record_round_result("m", {"hand_number": 1})
        assert registry.last_result("m") is not None
        # Still present, still active.
        assert registry.get_match("m") is not None
        assert registry.status()["active_matches"] == 1

    def test_no_arg_peek_never_evicts_and_stays_stable(self) -> None:
        # The most-recent "peek" is idempotent: repeated no-arg reads keep
        # answering the latest result and never drop records (#3884 recency).
        registry = TurnRegistry()
        registry.match_started("a")
        registry.match_started("b")
        registry.record_match_end("a", {"reason": "a-end"})
        registry.record_match_end("b", {"reason": "b-end"})
        for _ in range(3):
            latest = registry.last_result()
            assert latest is not None and latest["match_id"] == "b"
        # Both finished records survive the repeated peeks.
        assert registry.status()["finished_matches"] == 2

    def test_cap_sweep_keeps_most_recent_finished_and_drops_oldest(self) -> None:
        registry = TurnRegistry()
        total = MAX_FINISHED_MATCHES + 10
        for i in range(total):
            mid = f"m-{i}"
            registry.match_started(mid)
            registry.record_match_end(mid, {"idx": i})
        # Bounded at the cap...
        assert registry.status()["finished_matches"] == MAX_FINISHED_MATCHES
        # ...retaining the newest by result recency, evicting the oldest.
        assert registry.get_match(f"m-{total - 1}") is not None  # newest kept
        assert registry.get_match("m-0") is None  # oldest evicted
        # The most-recent answer survives the sweep (recency preserved).
        latest = registry.last_result()
        assert latest is not None and latest["match_id"] == f"m-{total - 1}"

    def test_cap_sweep_never_evicts_active_matches(self) -> None:
        registry = TurnRegistry()
        # A pile of live matches that never finish...
        active_ids = [f"live-{i}" for i in range(5)]
        for mid in active_ids:
            registry.match_started(mid)
            registry.record_round_result(mid, {"hand": 1})
        # ...plus enough finished matches to trigger sweeps repeatedly.
        for i in range(MAX_FINISHED_MATCHES + 20):
            mid = f"fin-{i}"
            registry.match_started(mid)
            registry.record_match_end(mid, {"idx": i})
        # Every active match is untouched; finished are capped.
        for mid in active_ids:
            assert registry.get_match(mid) is not None
        status = registry.status()
        assert status["active_matches"] == len(active_ids)
        assert status["finished_matches"] == MAX_FINISHED_MATCHES

    def test_match_end_drops_last_turn_footprint(self) -> None:
        # A finished match sheds its last in-flight TurnSnapshot (with full
        # action_history) -- it can never serve another turn.
        registry = TurnRegistry()
        registry.match_started("m")
        registry.publish_turn(_snapshot("m"))
        registry.record_match_end("m", {"reason": "done"})
        with registry._lock:  # white-box: the retained record sheds last_turn
            assert registry._matches["m"].last_turn is None


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

    def test_decide_returns_immediately_when_the_turn_is_cancelled(self) -> None:
        """chipzen-ai/Chipzen#3900: a close() mid-decision unblocks decide() now.

        With a 30s budget the pre-fix decide() sat on its event until the budget
        expired -- blocking the SDK session thread and forcing stop()'s join to
        time out. The cancelled turn resolves it at once, with a REAL legal
        action (never a sentinel Action) so the SDK can send it safely.
        """
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=30_000, margin_ms=0)
        threading.Timer(0.05, registry.close).start()
        started = time.monotonic()
        action = bot.decide(_turn_state(valid_actions=["check", "raise"]))
        elapsed = time.monotonic() - started
        assert action.action == "check"
        assert action == Action.check()  # the SDK's own fallback, not a sentinel
        assert elapsed < 2.0, f"decide() lingered {elapsed:.2f}s after cancel"

    def test_decide_prefers_a_submitted_action_over_cancellation(self) -> None:
        # An act that already landed is honoured; cancellation only wins when
        # nothing was submitted.
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=5000, margin_ms=0)

        def answer_then_close() -> None:
            got = registry.wait_for_any_turn(2.0)
            assert got is not None
            registry.submit_action(got.match_id, Action.raise_to(60), request_id=got.request_id)
            registry.close()

        answerer = threading.Thread(target=answer_then_close)
        answerer.start()
        action = bot.decide(_turn_state())
        answerer.join()
        assert action.action == "raise" and action.amount == 60

    def test_lifecycle_hooks_record_results(self) -> None:
        registry = TurnRegistry()
        bot = self._started_bot(registry, timeout_ms=5000, margin_ms=0)
        bot.on_round_result({"result": {"hand_number": 2, "winner_seats": [1]}})
        bot.on_match_end({"reason": "completed", "results": []})
        last = registry.last_result(MATCH)
        assert last is not None and last["match_end"] is not None

    def test_announced_clock_surfaces_on_the_match_record(self) -> None:
        """The match_start clock (30s casual / 2s rated, chipzen-ai/Chipzen#3750
        advertises it end-to-end) must be visible in match summaries."""
        registry = TurnRegistry()
        self._started_bot(registry, timeout_ms=30000, margin_ms=0)
        summary = registry.list_matches()[0]
        assert summary["turn_timeout_ms"] == 30000

    def test_on_reconnected_populates_match_identity(self) -> None:
        """chipzen-ai/chipzen-sdk#119: a re-attach delivers ``reconnected``,
        never ``match_start`` -- the bot must still learn which match it is
        playing, or every turn publishes under an empty '' record."""
        registry = TurnRegistry()
        bot = BridgeBot(registry, safety_margin_ms=0)
        bot.on_reconnected({"match_id": MATCH, "round_number": 3, "match_state": "in_progress"})
        assert bot._match_id == MATCH
        assert [m["match_id"] for m in registry.list_matches()] == [MATCH]

    def test_on_reconnected_routes_results_to_the_real_match(self) -> None:
        # The exact #119 corruption: post-re-attach results/turns must land on
        # the real match record, not on a shared "" record.
        registry = TurnRegistry()
        registry.match_started(MATCH)  # the log tap saw the lobby `matched`
        bot = BridgeBot(registry, safety_margin_ms=0)
        bot.on_reconnected({"match_id": MATCH, "round_number": 3})
        bot.on_round_result({"result": {"hand_number": 61, "winner_seats": [0]}})
        last = registry.last_result(MATCH)
        assert last is not None
        assert last["last_round_result"]["hand_number"] == 61
        assert registry.get_match("") is None

    def test_on_reconnected_honours_announced_clock_when_present(self) -> None:
        registry = TurnRegistry()
        bot = BridgeBot(registry, safety_margin_ms=0)
        bot.on_reconnected({"match_id": MATCH, "turn_timeout_ms": 2000})
        assert registry.list_matches()[0]["turn_timeout_ms"] == 2000

    def test_on_reconnected_without_match_id_creates_no_empty_record(self) -> None:
        # A malformed frame must not mint the very "" record #119 is about.
        registry = TurnRegistry()
        bot = BridgeBot(registry, safety_margin_ms=0)
        bot.on_reconnected({})
        assert registry.list_matches() == []


def test_state_payload_mirrors_wire_schema() -> None:
    payload = state_payload(_turn_state())
    assert payload["phase"] == "flop"
    assert payload["valid_actions"] == ["check", "raise", "all_in"]
    assert payload["to_call"] == 0
    # Wire-schema field names, not SDK-internal ones.
    assert "your_hole_cards" in payload and "board" in payload


class TestSessionPresence:
    def test_starts_starting_and_transitions(self) -> None:
        presence = SessionPresence()
        assert presence.snapshot()["lobby_state"] == LOBBY_STARTING
        assert presence.connected is False

        presence.transition(LOBBY_CONNECTED)
        assert presence.connected is True

        presence.transition(LOBBY_RECONNECTING, "retrying")
        snap = presence.snapshot()
        assert snap["lobby_state"] == LOBBY_RECONNECTING and snap["lobby_detail"] == "retrying"

    def test_session_over_first_verdict_wins(self) -> None:
        presence = SessionPresence()
        presence.session_over("session ended")
        presence.session_over("session thread exited")
        assert presence.snapshot()["lobby_detail"] == "session ended"

    def test_session_over_preserves_evicted(self) -> None:
        presence = SessionPresence()
        presence.transition(LOBBY_EVICTED, "replaced")
        presence.session_over("session thread exited")
        assert presence.snapshot()["lobby_state"] == LOBBY_EVICTED


def _emit(tap: SdkLogTap, template: str, *args: object) -> None:
    """Feed the tap a record exactly as the SDK logger would build it."""
    tap.emit(logging.LogRecord(SDK_LOGGER_NAME, logging.INFO, __file__, 0, template, args, None))


class TestSdkLogTap:
    def _tap(self) -> tuple[SdkLogTap, TurnRegistry, SessionPresence]:
        registry = TurnRegistry()
        presence = SessionPresence()
        return SdkLogTap(registry, presence), registry, presence

    def test_lobby_connected_and_closed(self) -> None:
        tap, _, presence = self._tap()
        _emit(tap, SdkLogTap.T_LOBBY_CONNECTED, "lobby")
        assert presence.connected is True
        _emit(tap, SdkLogTap.T_LOBBY_CLOSED)
        assert presence.snapshot()["lobby_state"] == LOBBY_RECONNECTING

    def test_lobby_connect_failed_reports_attempt(self) -> None:
        tap, _, presence = self._tap()
        _emit(tap, SdkLogTap.T_LOBBY_CONNECT_FAILED, "boom", 1.5, 2, 5)
        snap = presence.snapshot()
        assert snap["lobby_state"] == LOBBY_RECONNECTING
        assert snap["lobby_detail"] == "attempt 2/5"

    def test_lobby_evicted_is_terminal(self) -> None:
        tap, _, presence = self._tap()
        _emit(tap, SdkLogTap.T_LOBBY_EVICTED)
        assert presence.snapshot()["lobby_state"] == LOBBY_EVICTED
        _emit(tap, SdkLogTap.T_SESSION_ENDED, 3)
        assert presence.snapshot()["lobby_state"] == LOBBY_EVICTED

    def test_session_ended(self) -> None:
        tap, _, presence = self._tap()
        _emit(tap, SdkLogTap.T_SESSION_ENDED, 1)
        assert presence.snapshot()["lobby_state"] == LOBBY_ENDED

    def test_matched_records_match_and_rated(self) -> None:
        tap, registry, _ = self._tap()
        _emit(tap, SdkLogTap.T_MATCHED, "m-9", False)
        matches = registry.list_matches()
        assert matches[0]["match_id"] == "m-9" and matches[0]["rated"] is False

    def test_matched_tolerates_non_bool_rated(self) -> None:
        tap, registry, _ = self._tap()
        _emit(tap, SdkLogTap.T_MATCHED, "m-9", None)
        assert registry.list_matches()[0]["rated"] is None

    def test_gateway_and_match_reconnect_states(self) -> None:
        tap, registry, _ = self._tap()
        _emit(tap, SdkLogTap.T_GATEWAY_CONNECTING, "m-9")
        assert registry.list_matches()[0]["connection"] == MATCH_CONN_CONNECTED
        _emit(tap, SdkLogTap.T_MATCH_RECONNECTING, "m-9", 1.5, 3, 5, "drop")
        summary = registry.list_matches()[0]
        assert summary["connection"] == MATCH_CONN_RECONNECTING
        assert summary["reconnect_attempt"] == 3
        _emit(tap, SdkLogTap.T_MATCH_ABANDONED, "m-9", "drop")
        assert registry.list_matches()[0]["connection"] == MATCH_CONN_ABANDONED

    def test_unknown_template_is_ignored(self) -> None:
        tap, registry, presence = self._tap()
        _emit(tap, "some brand-new SDK log line %s", "x")
        assert presence.snapshot()["lobby_state"] == LOBBY_STARTING
        assert registry.list_matches() == []

    def test_required_templates_pinned_to_installed_sdk(self) -> None:
        """Every REQUIRED template must exist verbatim in the installed SDK.

        This is the drift guard: an SDK release that rewords a lobby log
        line would silently blind the presence tap; this test makes it a
        loud CI failure instead.
        """
        import chipzen.external

        source = inspect.getsource(chipzen.external)
        for template in SdkLogTap.REQUIRED_TEMPLATES:
            assert template in source, f"SDK no longer logs template: {template!r}"

    def test_optional_templates_pinned_to_repo_sdk(self) -> None:
        """OPTIONAL templates (post-0.3.0 events) tracked against the repo SDK.

        The published 0.3.0 wheel predates mid-match gateway reconnect, so
        these are checked against the in-repo SDK source when available
        (always true in this repo's CI checkout).
        """
        repo_sdk = Path(__file__).resolve().parents[2] / "python" / "src" / "chipzen"
        source_file = repo_sdk / "external.py"
        if not source_file.exists():
            pytest.skip("in-repo SDK source not available")
        source = source_file.read_text(encoding="utf-8")
        for template in SdkLogTap.OPTIONAL_TEMPLATES:
            assert template in source, f"repo SDK no longer logs template: {template!r}"


class TestExternalSession:
    def _forever(self) -> ExternalSession:
        async def forever() -> None:
            await asyncio.Event().wait()

        return ExternalSession(CONFIG, TurnRegistry(), runner=forever)

    def test_stop_unblocks_a_running_session(self) -> None:
        session = self._forever()
        session.start()
        assert _wait_until(lambda: session.running)
        assert session.stop(timeout=5.0) is True
        assert session.running is False
        assert session.error is None
        assert session.stop_requested is True
        assert session.presence_snapshot()["lobby_state"] == LOBBY_ENDED

    def test_stop_is_idempotent_and_safe_without_start(self) -> None:
        session = self._forever()
        assert session.stop() is True  # never started
        session.start()
        assert _wait_until(lambda: session.running)
        assert session.stop(timeout=5.0) is True
        assert session.stop(timeout=1.0) is True  # second stop is a no-op

    def test_stop_gives_stragglers_a_grace_then_cancels(self) -> None:
        started = threading.Event()
        finished = threading.Event()

        async def spawns_straggler() -> None:
            async def quick_cleanup() -> None:
                # Still in flight when stop() cancels the session; if the
                # drain cancelled instead of waiting, CancelledError would
                # fire here and `finished` would never be set.
                await asyncio.sleep(0.2)
                finished.set()

            asyncio.get_running_loop().create_task(quick_cleanup())
            started.set()
            await asyncio.Event().wait()

        session = ExternalSession(
            CONFIG, TurnRegistry(), runner=spawns_straggler, drain_grace_s=2.0
        )
        session.start()
        assert started.wait(timeout=5.0)  # the session (and straggler) is live
        assert session.stop(timeout=5.0) is True
        assert finished.is_set()  # the straggler got its grace window

    def test_stop_with_a_pending_turn_joins_fast(self) -> None:
        """chipzen-ai/Chipzen#3900: host death mid-turn must not linger ~10s.

        Faithful reproduction of the mechanism: ``decide()`` runs synchronously
        ON the session thread, so while it waits for an ``act`` the session's
        event loop cannot even process ``stop()``'s cancellation. Pre-fix,
        ``stop()`` therefore burned its whole ``timeout`` (10.08s measured on
        the published wheel), returned ``False``, abandoned the daemon thread
        and logged "session still winding down at exit". With the pending turn
        cancelled on ``stop()``, ``decide()`` returns at once and the thread
        joins in well under a second.
        """
        registry = TurnRegistry()
        bot = BridgeBot(registry, safety_margin_ms=0)
        bot.on_match_start({"match_id": MATCH, "turn_timeout_ms": 30_000})
        in_decide = threading.Event()
        decided: dict[str, object] = {}

        async def blocks_in_decide() -> None:
            in_decide.set()
            # Synchronous decide() on the session thread: the loop is blocked
            # here, exactly as in the real SDK session.
            decided["action"] = bot.decide(_turn_state(valid_actions=["check", "raise"]))
            await asyncio.Event().wait()

        session = ExternalSession(CONFIG, registry, runner=blocks_in_decide)
        session.start()
        assert in_decide.wait(timeout=5.0)
        assert _wait_until(lambda: registry.status()["pending_turns"] == 1)

        started = time.monotonic()
        assert session.stop(timeout=10.0) is True  # pre-fix: False at the cap
        elapsed = time.monotonic() - started

        assert elapsed < 3.0, f"stop() lingered {elapsed:.2f}s with a turn pending"
        assert registry.status()["pending_turns"] == 0
        action = decided.get("action")
        assert action is not None and action.action == "check"  # type: ignore[attr-defined]

    def test_session_completing_normally(self) -> None:
        async def quick() -> list:
            return []

        session = ExternalSession(CONFIG, TurnRegistry(), runner=quick)
        session.start()
        assert _wait_until(lambda: not session.running)
        assert session.error is None
        assert session.presence_snapshot()["lobby_state"] == LOBBY_ENDED
        assert session.stop() is True

    def test_session_error_is_surfaced(self) -> None:
        async def boom() -> None:
            raise RuntimeError("kaput")

        session = ExternalSession(CONFIG, TurnRegistry(), runner=boom)
        session.start()
        assert _wait_until(lambda: not session.running)
        assert isinstance(session.error, RuntimeError)
        assert session.presence_snapshot()["lobby_state"] == LOBBY_ENDED
        session.stop()

    def test_lobby_presence_flows_from_sdk_logs(self) -> None:
        session = self._forever()
        session.start()
        assert _wait_until(lambda: session.running)
        try:
            sdk_logger = logging.getLogger(SDK_LOGGER_NAME)
            assert session.lobby_connected is False
            sdk_logger.info("lobby: connected (endpoint=%s)", "lobby")
            assert session.lobby_connected is True
            snap = session.presence_snapshot()
            assert snap["lobby_state"] == LOBBY_CONNECTED and snap["lobby_connected"] is True

            sdk_logger.warning("lobby: evicted (replaced by a newer connection)")
            assert session.lobby_connected is False
            assert session.presence_snapshot()["lobby_state"] == LOBBY_EVICTED
        finally:
            assert session.stop(timeout=5.0) is True
        # Eviction is a terminal verdict; teardown must not repaint it.
        assert session.presence_snapshot()["lobby_state"] == LOBBY_EVICTED

    def test_lobby_connected_requires_live_thread(self) -> None:
        async def quick() -> None:
            logging.getLogger(SDK_LOGGER_NAME).info("lobby: connected (endpoint=%s)", "lobby")

        session = ExternalSession(CONFIG, TurnRegistry(), runner=quick)
        session.start()
        assert _wait_until(lambda: not session.running)
        # Presence saw "connected", but the thread is gone -> not connected.
        assert session.lobby_connected is False
        session.stop()
