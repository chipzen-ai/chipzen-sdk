"""Tests for the protocol-conformance harness used by ``validate --check-connectivity``."""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot
from chipzen.conformance import (
    SCENARIOS,
    ConformanceCheck,
    _action_rejected_script,
    _classify_turn_action,
    _full_match_script,
    _MockWebSocket,
    _multi_turn_script,
    _retry_storm_script,
    _run_with_hard_timeout,
    run_conformance_checks,
)
from chipzen.models import Action, GameState

# ---------------------------------------------------------------------------
# Bots used as test fixtures — each exhibits a specific behavior the
# conformance harness should classify correctly.
# ---------------------------------------------------------------------------


class PassingBot(ChipzenBot):
    """A well-behaved bot that returns a valid action quickly."""

    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        if "call" in state.valid_actions:
            return Action.call()
        return Action.fold()


class RaisingBot(ChipzenBot):
    """A bot whose decide() raises an exception.

    The SDK catches the exception and substitutes a safe fallback action
    (check if legal, else fold) so the protocol exchange still completes.
    The conformance check therefore reports PASS for this bot — the user
    code bug is the smoke_test's concern, not connectivity's.
    """

    def decide(self, state: GameState) -> Action:
        raise RuntimeError("intentional failure for testing")


# ---------------------------------------------------------------------------
# run_conformance_checks — end-to-end behavior
# ---------------------------------------------------------------------------


class TestRunConformanceChecks:
    def test_passing_bot_returns_pass(self):
        results = run_conformance_checks(PassingBot())
        assert len(results) >= 1
        assert all(isinstance(r, ConformanceCheck) for r in results)
        # Every scenario should pass for a well-behaved bot.
        fails = [r for r in results if r.severity == "fail"]
        assert fails == [], f"Unexpected failures: {fails}"

    def test_raising_bot_passes_via_sdk_fallback(self):
        """SDK's safe-fallback turns a raising decide() into a fold/check.

        The protocol exchange completes successfully from the wire's
        perspective even though the user's code raised. The user-code bug
        is the smoke_test check's responsibility — connectivity only cares
        whether the on-the-wire conversation finishes cleanly.
        """
        results = run_conformance_checks(RaisingBot())
        fails = [r for r in results if r.severity == "fail"]
        assert fails == [], (
            "Expected SDK fallback to keep the wire exchange green even when "
            f"decide() raises; got {fails}"
        )

    def test_full_match_scenario_is_present(self):
        """The full_match scenario should always run — it's the baseline."""
        results = run_conformance_checks(PassingBot())
        names = {r.name for r in results}
        assert "connectivity_full_match" in names


# ---------------------------------------------------------------------------
# _classify_turn_action — unit-level check on the validator helper
# ---------------------------------------------------------------------------


class TestClassifyTurnAction:
    def test_valid_call_action_passes(self):
        import json

        ok, msg = _classify_turn_action(
            json.dumps(
                {
                    "type": "turn_action",
                    "request_id": "req_1",
                    "params": {"action": "call"},
                }
            )
        )
        assert ok, msg

    def test_invalid_request_id_fails(self):
        import json

        ok, msg = _classify_turn_action(
            json.dumps(
                {
                    "type": "turn_action",
                    "request_id": "wrong_id",
                    "params": {"action": "call"},
                }
            )
        )
        assert not ok
        assert "request_id" in msg

    def test_unparseable_payload_fails(self):
        ok, msg = _classify_turn_action("not json at all")
        assert not ok
        assert "JSON" in msg

    def test_non_action_message_is_ignored(self):
        """Non-turn_action messages (e.g. authenticate, hello) shouldn't fail."""
        import json

        ok, msg = _classify_turn_action(json.dumps({"type": "authenticate", "token": "x"}))
        assert ok, msg


# ---------------------------------------------------------------------------
# Mock WebSocket — sanity check on the fixture itself
# ---------------------------------------------------------------------------


class TestMockWebSocket:
    @pytest.mark.asyncio
    async def test_messages_replay_in_order(self):
        ws = _MockWebSocket(_full_match_script())
        # The first messages should be the handshake
        first = await ws.recv()
        import json as _j

        assert _j.loads(first)["type"] == "hello"

    @pytest.mark.asyncio
    async def test_send_captures_payloads(self):
        ws = _MockWebSocket([])
        await ws.send('{"type": "turn_action", "request_id": "req_1"}')
        assert ws.sent == ['{"type": "turn_action", "request_id": "req_1"}']

    @pytest.mark.asyncio
    async def test_async_iteration_terminates(self):
        ws = _MockWebSocket([{"type": "match_end", "match_id": "m"}])
        count = 0
        async for _ in ws:
            count += 1
        assert count == 1


# ---------------------------------------------------------------------------
# Scenario coverage — every scenario should run for a passing bot
# ---------------------------------------------------------------------------


class TestScenarioCoverage:
    def test_all_four_scenarios_registered(self):
        names = [name for name, _ in SCENARIOS]
        assert names == [
            "connectivity_full_match",
            "multi_turn_request_id_echo",
            "action_rejected_recovery",
            "retry_storm_bounded",
        ]

    def test_passing_bot_passes_all_scenarios(self):
        results = run_conformance_checks(PassingBot())
        assert len(results) == 4
        names = [r.name for r in results]
        assert "connectivity_full_match" in names
        assert "multi_turn_request_id_echo" in names
        assert "action_rejected_recovery" in names
        assert "retry_storm_bounded" in names
        fails = [r for r in results if r.severity == "fail"]
        assert fails == [], f"Unexpected failures: {[(r.name, r.message) for r in fails]}"


# ---------------------------------------------------------------------------
# Multi-turn scenario — request_id echo across preflop/flop/turn
# ---------------------------------------------------------------------------


class TestMultiTurnScenario:
    def test_script_has_three_turn_requests(self):
        """Sanity-check the fixture itself before the SDK consumes it."""
        script = _multi_turn_script()
        turn_requests = [m for m in script if m.get("type") == "turn_request"]
        assert len(turn_requests) == 3
        assert [r["request_id"] for r in turn_requests] == ["req_1", "req_2", "req_3"]

    def test_passing_bot_echoes_all_three_request_ids(self):
        results = run_conformance_checks(PassingBot())
        multi_turn = next(r for r in results if r.name == "multi_turn_request_id_echo")
        assert multi_turn.severity == "pass", multi_turn.message


# ---------------------------------------------------------------------------
# action_rejected scenario — SDK retries with safe action
# ---------------------------------------------------------------------------


class TestActionRejectedScenario:
    def test_script_includes_action_rejected(self):
        script = _action_rejected_script()
        rejections = [m for m in script if m.get("type") == "action_rejected"]
        assert len(rejections) == 1
        assert rejections[0]["request_id"] == "req_1"

    def test_passing_bot_recovers_via_safe_fallback(self):
        results = run_conformance_checks(PassingBot())
        recovery = next(r for r in results if r.name == "action_rejected_recovery")
        assert recovery.severity == "pass", recovery.message
        # Message should explicitly mention the safe action used.
        assert "check" in recovery.message or "fold" in recovery.message


# ---------------------------------------------------------------------------
# Retry storm — three back-to-back rejections, SDK stays responsive
# ---------------------------------------------------------------------------


class TestRetryStormScenario:
    def test_script_includes_three_consecutive_rejections(self):
        script = _retry_storm_script()
        rejections = [m for m in script if m.get("type") == "action_rejected"]
        assert len(rejections) == 3

    def test_passing_bot_handles_storm(self):
        results = run_conformance_checks(PassingBot())
        storm = next(r for r in results if r.name == "retry_storm_bounded")
        assert storm.severity == "pass", storm.message
        # Pass message should confirm the 4-turn-action total (1 + 3 retries).
        assert "4" in storm.message


# ---------------------------------------------------------------------------
# Hard watchdog — protects against busy-loop bots that block the event loop
# ---------------------------------------------------------------------------


class _BusyLoopBot(ChipzenBot):
    """A bot whose ``decide()`` blocks the calling thread for longer than the timeout.

    This is the exact failure mode ``asyncio.wait_for`` cannot rescue from
    on its own — sync blocking from inside an asyncio task starves the
    event loop, so the inner timeout coroutine never fires.

    The ``time.sleep`` here is deliberate (vs. ``while True: pass`` busy
    loop) so the test runtime is bounded — we want the watchdog to
    reliably trigger, not for the test itself to pin a CPU.
    """

    def decide(self, state: GameState) -> Action:
        time.sleep(2.0)
        return Action.fold()


class TestHardWatchdog:
    def test_watchdog_terminates_blocking_scenario(self):
        """Confirm ``_run_with_hard_timeout`` returns a Fail when the inner work overruns.

        Use a trivially-blocking coroutine (``await asyncio.sleep``) that
        outlasts ``hard_timeout_s``. ``asyncio.sleep`` in a thread-pool
        ``asyncio.run`` won't be cancelled when the surrounding
        ``ThreadPoolExecutor.shutdown(cancel_futures=True)`` fires — the
        future was already running. So we depend on the watchdog's
        ``future.result(timeout=...)`` returning a TimeoutError, which
        gets translated to a Fail ConformanceCheck.
        """

        async def slow_scenario() -> ConformanceCheck:
            await asyncio.sleep(2.0)
            return ConformanceCheck("pass", "should_not_reach", "unreachable")

        result = _run_with_hard_timeout(
            "watchdog_test",
            slow_scenario,
            inner_timeout_s=0.5,
            hard_timeout_s=0.3,
        )
        assert result.severity == "fail"
        assert result.name == "watchdog_test"
        assert "watchdog" in result.message.lower()

    def test_busy_loop_bot_is_caught_by_inner_or_outer_timeout(self):
        """End-to-end: a sync-blocking bot must produce a fail, not hang the harness.

        With the bot's ``decide()`` blocking for 2.0s and a per-scenario
        timeout of 0.4s (plus +5s hard watchdog = 5.4s), the inner
        ``asyncio.wait_for`` SHOULD eventually fire after the sync block
        returns — the event loop comes back to life and processes the
        scheduled timeout callback. We assert: at least one scenario
        fails (not necessarily all four).
        """
        results = run_conformance_checks(_BusyLoopBot(), timeout_s=0.4)
        fails = [r for r in results if r.severity == "fail"]
        # Don't assert every scenario fails — the precise number depends on
        # which scenarios block on a turn_request vs. resolve via SDK
        # action_rejected substitution. The contract is "the harness
        # surfaces a failure for a too-slow bot, doesn't hang silently".
        assert len(fails) >= 1, (
            f"Expected at least one scenario to fail for a slow bot; got results: "
            f"{[(r.name, r.severity, r.message) for r in results]}"
        )
