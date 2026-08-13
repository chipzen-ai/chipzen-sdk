"""Tests for the MCP tool surface."""

import asyncio
import time

import pytest

from chipzen_mcp.bridge import ExternalSession, TurnRegistry, TurnSnapshot
from chipzen_mcp.config import McpConfig
from chipzen_mcp.housebot import HttpResult
from chipzen_mcp.server import (
    act_impl,
    answer_remote_challenge_impl,
    build_server,
    challenge_house_bot_impl,
    challenge_remote_impl,
    get_last_result_impl,
    get_match_state_impl,
    get_status_impl,
    join_rated_queue_impl,
    leave_rated_queue_impl,
    list_lobby_opponents_impl,
    list_matches_impl,
    list_remote_challenges_impl,
    rated_queue_status_impl,
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
    "join_rated_queue",
    "rated_queue_status",
    "leave_rated_queue",
    "list_lobby_opponents",
    "challenge_remote",
    "list_remote_challenges",
    "accept_remote_challenge",
    "decline_remote_challenge",
}


def _publish(registry: TurnRegistry, match_id: str = MATCH, request_id: str = "req-1") -> None:
    now = time.time()
    registry.publish_turn(
        TurnSnapshot(
            match_id=match_id,
            request_id=request_id,
            published_at=now,
            deadline_at=now + 30.0,
            state={"hand_number": 1, "valid_actions": ["check", "raise"], "min_raise": 10},
        )
    )


async def test_build_server_registers_every_tool() -> None:
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
    assert status["lobby_connected"] is False  # no session -> not connected


def test_get_status_surfaces_lobby_presence() -> None:
    registry = TurnRegistry()
    config = McpConfig(token="cz_extbot_x", bot_id="b-1", env="staging")
    session = ExternalSession(config, registry)
    status = get_status_impl(registry, session, config)
    assert status["lobby_connected"] is False
    assert status["lobby_state"] == "starting"
    assert "lobby_detail" in status


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
    # chipzen-ai/Chipzen#3906: the turn carries its id AND the nudge to quote
    # it back to act, so an LLM agent actually uses the guard.
    assert turn["request_id"] == "req-1"
    assert "request_id" in turn["note"] and "stale_turn" in turn["note"]


def test_get_match_state_turn_carries_the_request_id_hint() -> None:
    registry = TurnRegistry()
    _publish(registry)
    view = get_match_state_impl(registry, MATCH)
    assert view["turn"]["request_id"] == "req-1"
    assert "request_id" in view["turn"]["note"]


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


class TestActRequestId:
    """chipzen-ai/Chipzen#3906: the optional per-turn optimistic-concurrency token."""

    def test_matching_request_id_accepted_and_echoed(self) -> None:
        registry = TurnRegistry()
        _publish(registry, request_id="A")
        result = act_impl(registry, MATCH, "check", request_id="A")
        assert result["accepted"] is True and result["request_id"] == "A"

    def test_stale_request_id_is_refused_with_actionable_note(self) -> None:
        # The issue's repro, through the tool surface: turn A observed, bridge
        # fell back, turn B published, the agent's late act for A arrives.
        registry = TurnRegistry()
        _publish(registry, request_id="A")
        registry.clear_pending(MATCH)
        _publish(registry, request_id="B")

        result = act_impl(registry, MATCH, "raise", amount=60, request_id="A")
        assert result["accepted"] is False
        assert result["error"] == "stale_turn"
        assert result["request_id"] == "A"
        assert result["pending_request_id"] == "B"
        assert "wait_for_turn" in result["note"]
        # Turn B was NOT answered by the stale act -- it is still pending.
        assert registry.pending_request_id(MATCH) == "B"
        # ...and answering B properly still works.
        assert act_impl(registry, MATCH, "check", request_id="B")["accepted"] is True

    def test_omitted_request_id_is_backward_compatible(self) -> None:
        # Agents built against 0.1.x call act without request_id; that must keep
        # working (documented as the old, unsafe behaviour).
        registry = TurnRegistry()
        _publish(registry, request_id="A")
        assert act_impl(registry, MATCH, "check")["accepted"] is True

    def test_stale_id_with_nothing_pending_reports_no_pending_turn(self) -> None:
        registry = TurnRegistry()
        _publish(registry, request_id="A")
        registry.clear_pending(MATCH)
        result = act_impl(registry, MATCH, "check", request_id="A")
        assert result["accepted"] is False and result["error"] == "no_pending_turn"


async def test_act_tool_schema_exposes_request_id() -> None:
    """An LLM agent can only quote the token if the tool schema advertises it."""
    server = build_server(TurnRegistry())
    act_tool = next(tool for tool in await server.list_tools() if tool.name == "act")
    assert "request_id" in act_tool.inputSchema["properties"]
    # Optional: only match_id/action are required.
    assert set(act_tool.inputSchema.get("required", [])) == {"match_id", "action"}
    assert "request_id" in (act_tool.description or "")


async def test_act_tool_rejects_a_stale_turn_end_to_end() -> None:
    registry = TurnRegistry()
    server = build_server(registry)
    _publish(registry, request_id="A")
    registry.clear_pending(MATCH)
    _publish(registry, request_id="B")
    result = await server.call_tool("act", {"match_id": MATCH, "action": "fold", "request_id": "A"})
    assert "stale_turn" in str(result)
    assert registry.pending_request_id(MATCH) == "B"  # B untouched


def test_list_matches_and_last_result() -> None:
    registry = TurnRegistry()
    assert list_matches_impl(registry) == []
    assert get_last_result_impl(registry)["status"] == "no_results_yet"

    _publish(registry)
    registry.record_match_end(MATCH, {"reason": "completed"})
    matches = list_matches_impl(registry)
    assert len(matches) == 1 and matches[0]["finished"] is True
    assert get_last_result_impl(registry, MATCH)["match_end"] == {"reason": "completed"}


def test_get_last_result_no_arg_is_most_recent() -> None:
    # chipzen-ai/Chipzen#3884: the tool's "most recent across all matches"
    # path returns the newest result by recency, not by match insertion order.
    registry = TurnRegistry()
    registry.match_started("A")
    registry.match_started("B")
    registry.record_round_result("B", {"which": "B-older"})
    registry.record_round_result("A", {"which": "A-newest"})
    out = get_last_result_impl(registry, None)
    assert out["match_id"] == "A"
    assert out["last_round_result"]["which"] == "A-newest"


def test_get_last_result_unknown_match_id_is_no_results_yet() -> None:
    # chipzen-ai/Chipzen#3884 (repro B): a typo'd match_id must report
    # no_results_yet, never fall back to a different match's outcome.
    registry = TurnRegistry()
    registry.match_started("real-match")
    registry.record_round_result("real-match", {"which": "real"})
    out = get_last_result_impl(registry, "TYPO-does-not-exist")
    assert out == {"status": "no_results_yet"}


class TestChallengeHouseBotWiring:
    """The endpoint contract itself is covered in test_housebot.py; this is
    the tool-level wiring (config gate, lobby-liveness annotation)."""

    CONFIG = McpConfig(token="cz_extbot_x", bot_id="b-1", env="staging")

    def test_requires_configuration(self) -> None:
        result = challenge_house_bot_impl(None, None, "boss-1")
        assert result["status"] == "error" and result["error"] == "not_configured"

    def test_success_passes_through(self) -> None:
        def post(url: str, headers: dict, body: dict) -> HttpResult:
            return HttpResult(status=201, body={"match_id": "m-7", "decision_timeout_ms": 30000})

        result = challenge_house_bot_impl(self.CONFIG, None, "boss-1", post=post)
        assert result["status"] == "challenge_created"
        assert result["match_id"] == "m-7"
        assert "warning" not in result  # no session to second-guess

    def test_success_warns_when_lobby_looks_down(self) -> None:
        async def never_runs() -> None:  # session constructed but not started
            raise AssertionError("not reached")

        session = ExternalSession(self.CONFIG, TurnRegistry(), runner=never_runs)

        def post(url: str, headers: dict, body: dict) -> HttpResult:
            return HttpResult(status=201, body={"match_id": "m-7"})

        result = challenge_house_bot_impl(self.CONFIG, session, post=post)
        assert result["status"] == "challenge_created"
        assert "lobby" in result["warning"]

    def test_error_passes_through_unannotated(self) -> None:
        def post(url: str, headers: dict, body: dict) -> HttpResult:
            return HttpResult(status=404, body={})

        result = challenge_house_bot_impl(self.CONFIG, None, post=post)
        assert result["error"] == "endpoint_not_available"
        assert "warning" not in result


async def test_challenge_house_bot_tool_runs_off_loop() -> None:
    """The registered tool must not block the server loop (urllib is sync)."""
    server = build_server(TurnRegistry(), None, None)
    result = await asyncio.wait_for(
        server.call_tool("challenge_house_bot", {"bot_name": "boss-1"}), timeout=5.0
    )
    # No config injected -> the impl's config gate answers, proving the
    # async wrapper + to_thread path works end-to-end through FastMCP.
    assert "not_configured" in str(result)


class TestRatedQueueWiring:
    """The endpoint contract itself is covered in test_matchmaking.py; this is
    the tool-level wiring (config gate, lobby-liveness annotation)."""

    CONFIG = McpConfig(token="cz_extbot_x", bot_id="b-1", env="staging")

    def test_join_requires_configuration(self) -> None:
        result = join_rated_queue_impl(None, None)
        assert result["status"] == "error" and result["error"] == "not_configured"

    def test_status_requires_configuration(self) -> None:
        result = rated_queue_status_impl(None)
        assert result["status"] == "error" and result["error"] == "not_configured"

    def test_leave_requires_configuration(self) -> None:
        result = leave_rated_queue_impl(None)
        assert result["status"] == "error" and result["error"] == "not_configured"

    def test_join_queued_passes_through(self) -> None:
        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(
                status=200,
                body={"status": "queued", "position": 2, "queue_ttl_seconds": 60, "rated": True},
            )

        result = join_rated_queue_impl(self.CONFIG, None, request=request)
        assert result["status"] == "queued"
        assert result["position"] == 2
        assert "warning" not in result  # no session to second-guess

    def test_join_matched_warns_when_lobby_looks_down(self) -> None:
        async def never_runs() -> None:  # session constructed but not started
            raise AssertionError("not reached")

        session = ExternalSession(self.CONFIG, TurnRegistry(), runner=never_runs)

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(status=200, body={"status": "matched", "rated": True})

        result = join_rated_queue_impl(self.CONFIG, session, request=request)
        assert result["status"] == "matched"
        assert "lobby" in result["warning"]

    def test_join_error_passes_through_unannotated(self) -> None:
        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(status=404, body={})

        result = join_rated_queue_impl(self.CONFIG, None, request=request)
        assert result["error"] == "endpoint_not_available"
        assert "warning" not in result


async def test_rated_queue_tools_run_off_loop() -> None:
    """The registered rated-queue tools must not block the server loop."""
    server = build_server(TurnRegistry(), None, None)
    for tool in ("join_rated_queue", "rated_queue_status", "leave_rated_queue"):
        result = await asyncio.wait_for(server.call_tool(tool, {}), timeout=5.0)
        # No config injected -> the impl's config gate answers, proving the
        # async wrapper + to_thread path works end-to-end through FastMCP.
        assert "not_configured" in str(result)


class TestRemoteChallengeWiring:
    """The endpoint contract itself is covered in test_remote_challenge.py; this
    is the tool-level wiring (config gate, argument gate, lobby annotation)."""

    CONFIG = McpConfig(token="cz_extbot_x", bot_id="b-1", env="staging")

    def test_every_tool_requires_configuration(self) -> None:
        assert list_lobby_opponents_impl(None)["error"] == "not_configured"
        assert challenge_remote_impl(None, None, "rival")["error"] == "not_configured"
        assert list_remote_challenges_impl(None)["error"] == "not_configured"
        assert (
            answer_remote_challenge_impl(None, None, "c-1", action="accept")["error"]
            == "not_configured"
        )

    def test_challenge_requires_an_opponent(self) -> None:
        """A blank opponent is refused locally, with the discovery tool named."""

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            raise AssertionError("must not reach the network")

        result = challenge_remote_impl(self.CONFIG, None, "   ", request=request)
        assert result["error"] == "opponent_required"
        assert "list_lobby_opponents" in result["note"]

    def test_answer_requires_a_challenge_id(self) -> None:
        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            raise AssertionError("must not reach the network")

        result = answer_remote_challenge_impl(
            self.CONFIG, None, "", action="accept", request=request
        )
        assert result["error"] == "challenge_id_required"

    def test_opponent_is_trimmed_before_sending(self) -> None:
        seen: dict = {}

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            seen["body"] = body
            return HttpResult(status=200, body={"challenge_id": "c-1", "status": "pending"})

        challenge_remote_impl(self.CONFIG, None, "  Rival  ", request=request)
        assert seen["body"]["opponent"] == "Rival"

    def test_pending_challenge_warns_when_lobby_looks_down(self) -> None:
        async def never_runs() -> None:  # session constructed but not started
            raise AssertionError("not reached")

        session = ExternalSession(self.CONFIG, TurnRegistry(), runner=never_runs)

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(status=200, body={"challenge_id": "c-1", "status": "pending"})

        result = challenge_remote_impl(self.CONFIG, session, "rival", request=request)
        assert result["status"] == "pending"
        assert "lobby" in result["warning"]

    def test_error_passes_through_unannotated(self) -> None:
        async def never_runs() -> None:
            raise AssertionError("not reached")

        session = ExternalSession(self.CONFIG, TurnRegistry(), runner=never_runs)

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(status=404, body={})

        result = challenge_remote_impl(self.CONFIG, session, "rival", request=request)
        assert result["error"] == "endpoint_not_available"
        assert "warning" not in result

    def test_decline_is_never_lobby_annotated(self) -> None:
        """Declining starts no match, so our own lobby state is irrelevant."""

        async def never_runs() -> None:
            raise AssertionError("not reached")

        session = ExternalSession(self.CONFIG, TurnRegistry(), runner=never_runs)

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(status=200, body={"challenge_id": "c-1", "status": "declined"})

        result = answer_remote_challenge_impl(
            self.CONFIG, session, "c-1", action="decline", request=request
        )
        assert result["status"] == "declined"
        assert "warning" not in result

    def test_accept_is_lobby_annotated(self) -> None:
        async def never_runs() -> None:
            raise AssertionError("not reached")

        session = ExternalSession(self.CONFIG, TurnRegistry(), runner=never_runs)

        def request(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            return HttpResult(status=200, body={"challenge_id": "c-1", "status": "accepted"})

        result = answer_remote_challenge_impl(
            self.CONFIG, session, "c-1", action="accept", request=request
        )
        assert result["status"] == "accepted"
        assert "lobby" in result["warning"]


async def test_remote_challenge_tools_run_off_loop() -> None:
    """The registered direct-challenge tools must not block the server loop."""
    server = build_server(TurnRegistry(), None, None)
    calls = {
        "list_lobby_opponents": {},
        "challenge_remote": {"opponent": "rival"},
        "list_remote_challenges": {},
        "accept_remote_challenge": {"challenge_id": "c-1"},
        "decline_remote_challenge": {"challenge_id": "c-1"},
    }
    for tool, args in calls.items():
        result = await asyncio.wait_for(server.call_tool(tool, args), timeout=5.0)
        # No config injected -> the impl's config gate answers, proving the
        # async wrapper + to_thread path works end-to-end through FastMCP.
        assert "not_configured" in str(result)
