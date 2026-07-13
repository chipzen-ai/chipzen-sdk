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
    SDK_LOGGER_NAME,
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
