"""Tests for the MCP tool surface."""

import time

import pytest

from chipzen_mcp.bridge import TurnRegistry, TurnSnapshot
from chipzen_mcp.config import McpConfig
from chipzen_mcp.server import (
    act_impl,
    build_server,
    challenge_house_bot_impl,
    get_last_result_impl,
    get_match_state_impl,
    get_status_impl,
    list_matches_impl,
    wait_for_turn_impl,
)

MATCH = "m-1"

EXPECTED_TOOLS = {
    "get_status",
    "wait_for_turn",
    "get_match_state",
    "act",
    "list_matches",
    "get_last_result",
    "challenge_house_bot",
}


def _publish(registry: TurnRegistry, match_id: str = MATCH) -> None:
    now = time.time()
    registry.publish_turn(
        TurnSnapshot(
            match_id=match_id,
            request_id="req-1",
            published_at=now,
            deadline_at=now + 30.0,
            state={"hand_number": 1, "valid_actions": ["check", "raise"], "min_raise": 10},
        )
    )


async def test_build_server_registers_the_seven_tools() -> None:
    server = build_server(TurnRegistry())
    tools = {tool.name for tool in await server.list_tools()}
    assert tools == EXPECTED_TOOLS


def test_get_status_reports_config_and_counts() -> None:
    registry = TurnRegistry()
    _publish(registry)
    config = McpConfig(token="cz_extbot_x", bot_id="b-1", env="staging")
    status = get_status_impl(registry, None, config)
    assert status["bot_id"] == "b-1"
    assert status["env"] == "staging"
    assert status["session_running"] is False
    assert status["active_matches"] == 1
    assert status["pending_turns"] == 1
    assert status["concurrent_match_cap"] == 5


def test_wait_for_turn_idle_and_your_turn() -> None:
    registry = TurnRegistry()
    idle = wait_for_turn_impl(registry, timeout_ms=10)
    assert idle["status"] == "idle"

    _publish(registry)
    turn = wait_for_turn_impl(registry, timeout_ms=0)
    assert turn["status"] == "your_turn"
    assert turn["match_id"] == MATCH
    assert turn["remaining_ms"] > 0
    assert turn["state"]["valid_actions"] == ["check", "raise"]


def test_get_match_state_unknown_match() -> None:
    assert get_match_state_impl(TurnRegistry(), "nope")["error"] == "unknown_match"


class TestAct:
    def test_check_roundtrip(self) -> None:
        registry = TurnRegistry()
        _publish(registry)
        result = act_impl(registry, MATCH, "check")
        assert result["accepted"] is True

    def test_raise_requires_amount(self) -> None:
        registry = TurnRegistry()
        _publish(registry)
        result = act_impl(registry, MATCH, "raise")
        assert result["accepted"] is False and result["error"] == "amount_required"

    def test_raise_with_amount(self) -> None:
        registry = TurnRegistry()
        _publish(registry)
        result = act_impl(registry, MATCH, "raise", amount=60)
        assert result["accepted"] is True and result["amount"] == 60

    def test_invalid_action_string(self) -> None:
        result = act_impl(TurnRegistry(), MATCH, "bluff")
        assert result["accepted"] is False and result["error"] == "invalid_action"

    def test_no_pending_turn(self) -> None:
        result = act_impl(TurnRegistry(), MATCH, "fold")
        assert result["accepted"] is False and result["error"] == "no_pending_turn"

    @pytest.mark.parametrize("action", ["fold", "check", "call", "all_in"])
    def test_all_parameterless_actions_construct(self, action: str) -> None:
        registry = TurnRegistry()
        _publish(registry)
        assert act_impl(registry, MATCH, action)["accepted"] is True


def test_list_matches_and_last_result() -> None:
    registry = TurnRegistry()
    assert list_matches_impl(registry) == []
    assert get_last_result_impl(registry)["status"] == "no_results_yet"

    _publish(registry)
    registry.record_match_end(MATCH, {"reason": "completed"})
    matches = list_matches_impl(registry)
    assert len(matches) == 1 and matches[0]["finished"] is True
    assert get_last_result_impl(registry, MATCH)["match_end"] == {"reason": "completed"}


def test_challenge_house_bot_is_an_honest_stub() -> None:
    result = challenge_house_bot_impl("nexus")
    assert result["status"] == "not_implemented"
    assert "3750" in result["note"]
    assert result["requested_bot"] == "nexus"
