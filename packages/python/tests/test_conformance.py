"""Tests for the protocol-conformance harness used by ``validate --check-connectivity``."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.bot import ChipzenBot
from chipzen.conformance import (
    ConformanceCheck,
    _classify_turn_action,
    _full_match_script,
    _MockWebSocket,
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
