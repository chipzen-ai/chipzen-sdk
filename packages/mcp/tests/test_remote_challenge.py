"""Tests for the direct remote-challenge HTTP contract (chipzen-ai/Chipzen#3908).

Exercises every mapping in :mod:`chipzen_mcp.remote_challenge` through an
injected transport -- no sockets. The point of these tests is that each server
outcome becomes a DISTINCT, actionable tool payload: an agent that gets
``opponent_offline`` must be told something different from one that gets
``bot_offline`` (whose OWN lobby is down) or ``not_pending`` (too slow).
"""

from typing import Any

import pytest

from chipzen_mcp.config import McpConfig
from chipzen_mcp.housebot import HttpResult
from chipzen_mcp.remote_challenge import (
    REMOTE_CHALLENGE_BASE,
    REMOTE_OPPONENTS_PATH,
    request_answer_remote_challenge,
    request_challenge_remote,
    request_lobby_opponents,
    request_remote_challenges,
)

CONFIG = McpConfig(token="cz_extbot_secret", bot_id="bot-1", env="staging")
CONFIG_NO_BOT_ID = McpConfig(token="cz_extbot_secret", bot_id=None, env="staging")


def _transport(result: HttpResult, seen: dict[str, Any] | None = None):
    """A transport that records the call and replays *result*."""

    def request(method: str, url: str, headers: dict[str, str], body: dict | None) -> HttpResult:
        if seen is not None:
            seen.update({"method": method, "url": url, "headers": headers, "body": body})
        return result

    return request


def _envelope(code: str, message: str = "nope") -> dict[str, Any]:
    """A platform error envelope, as the API actually returns it."""
    return {"error_code": code, "message": message, "request_id": "req-abc"}


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


class TestWireShape:
    def test_opponents_is_a_get_on_the_right_path(self) -> None:
        seen: dict[str, Any] = {}
        request_lobby_opponents(
            CONFIG, request=_transport(HttpResult(status=200, body={"opponents": []}), seen)
        )
        assert seen["method"] == "GET"
        assert seen["url"] == f"https://staging.chipzen.ai{REMOTE_OPPONENTS_PATH}"
        assert seen["body"] is None
        assert seen["headers"]["Authorization"] == "Bearer cz_extbot_secret"
        assert seen["headers"]["User-Agent"].startswith("chipzen-mcp/")

    def test_challenge_posts_opponent_and_bot_id_cross_check(self) -> None:
        seen: dict[str, Any] = {}
        request_challenge_remote(
            CONFIG, "rival", request=_transport(HttpResult(status=200, body={}), seen)
        )
        assert seen["method"] == "POST"
        assert seen["url"] == f"https://staging.chipzen.ai{REMOTE_CHALLENGE_BASE}"
        assert seen["body"] == {"opponent": "rival", "bot_id": "bot-1"}

    def test_challenge_omits_bot_id_when_unconfigured(self) -> None:
        """The server model is ``extra="forbid"`` -- never send a null bot_id."""
        seen: dict[str, Any] = {}
        request_challenge_remote(
            CONFIG_NO_BOT_ID, "rival", request=_transport(HttpResult(status=200, body={}), seen)
        )
        assert seen["body"] == {"opponent": "rival"}

    @pytest.mark.parametrize("action", ["accept", "decline"])
    def test_answer_posts_to_the_action_path(self, action: str) -> None:
        seen: dict[str, Any] = {}
        request_answer_remote_challenge(
            CONFIG,
            "c-9",
            action=action,
            request=_transport(HttpResult(status=200, body={}), seen),
        )
        assert seen["method"] == "POST"
        assert seen["url"].endswith(f"{REMOTE_CHALLENGE_BASE}/c-9/{action}")

    def test_answer_rejects_an_unknown_action(self) -> None:
        with pytest.raises(ValueError, match="accept"):
            request_answer_remote_challenge(CONFIG, "c-9", action="ignore")


# ---------------------------------------------------------------------------
# opponents
# ---------------------------------------------------------------------------


class TestOpponents:
    def test_success_surfaces_the_roster(self) -> None:
        result = request_lobby_opponents(
            CONFIG,
            request=_transport(
                HttpResult(
                    status=200,
                    body={
                        "opponents": [{"bot_id": "b-2", "name": "Rival", "rating": 1533.2}],
                        "count": 1,
                        "rated": True,
                    },
                    headers={"x-request-id": "req-1"},
                )
            ),
        )
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["opponents"][0]["name"] == "Rival"
        assert result["rated"] is True
        assert result["request_id"] == "req-1"
        assert "challenge_remote" in result["note"]

    def test_empty_roster_points_at_the_queue(self) -> None:
        """Nobody around is not an error -- but the agent must be told what to do."""
        result = request_lobby_opponents(
            CONFIG,
            request=_transport(
                HttpResult(status=200, body={"opponents": [], "count": 0, "rated": True})
            ),
        )
        assert result["status"] == "ok"
        assert result["opponents"] == []
        assert "join_rated_queue" in result["note"]

    def test_unauthorized(self) -> None:
        result = request_lobby_opponents(
            CONFIG,
            request=_transport(HttpResult(status=401, body=_envelope("EXTAPI_INVALID_TOKEN"))),
        )
        assert result["error"] == "unauthorized"
        assert result["server_error_code"] == "EXTAPI_INVALID_TOKEN"
        assert result["request_id"] == "req-abc"
        assert "req-abc" in result["note"]

    def test_system_bot_is_ineligible(self) -> None:
        result = request_lobby_opponents(
            CONFIG,
            request=_transport(
                HttpResult(status=403, body=_envelope("EXTAPI_MATCHMAKING_INELIGIBLE"))
            ),
        )
        assert result["error"] == "ineligible"

    def test_404_means_not_deployed(self) -> None:
        result = request_lobby_opponents(
            CONFIG, request=_transport(HttpResult(status=404, body={}))
        )
        assert result["error"] == "endpoint_not_available"
        assert "join_rated_queue" in result["note"]

    def test_unexpected_status_is_surfaced_not_swallowed(self) -> None:
        result = request_lobby_opponents(
            CONFIG, request=_transport(HttpResult(status=503, body={}))
        )
        assert result["error"] == "http_503"

    def test_network_failure_is_a_payload_not_an_exception(self) -> None:
        def boom(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            raise OSError("connection refused")

        result = request_lobby_opponents(CONFIG, request=boom)
        assert result["error"] == "network"
        assert "connection refused" in result["note"]


# ---------------------------------------------------------------------------
# challenge
# ---------------------------------------------------------------------------


class TestChallenge:
    def test_pending_success(self) -> None:
        result = request_challenge_remote(
            CONFIG,
            "b-2",
            request=_transport(
                HttpResult(
                    status=200,
                    body={
                        "challenge_id": "c-1",
                        "status": "pending",
                        "opponent_bot_id": "b-2",
                        "opponent_name": "Rival",
                        "rated": True,
                        "expires_in_seconds": 58,
                    },
                    headers={"x-request-id": "req-2"},
                )
            ),
        )
        assert result["status"] == "pending"
        assert result["challenge_id"] == "c-1"
        assert result["opponent_name"] == "Rival"
        assert result["expires_in_seconds"] == 58
        assert result["request_id"] == "req-2"
        # The note must set the expectation that this is a handshake, not a match.
        assert "list_remote_challenges" in result["note"]

    def test_target_not_found(self) -> None:
        result = request_challenge_remote(
            CONFIG,
            "ghost",
            request=_transport(
                HttpResult(status=400, body=_envelope("EXTAPI_CHALLENGE_TARGET_NOT_FOUND"))
            ),
        )
        assert result["error"] == "target_not_found"
        assert "list_lobby_opponents" in result["note"]

    def test_same_owner_target_is_ineligible(self) -> None:
        result = request_challenge_remote(
            CONFIG,
            "my-other-bot",
            request=_transport(
                HttpResult(status=403, body=_envelope("EXTAPI_MATCHMAKING_INELIGIBLE"))
            ),
        )
        assert result["error"] == "ineligible"
        assert "same-owner" in result["note"]

    def test_opponent_offline_is_distinct_from_our_own_offline(self) -> None:
        """The two 409s must never collapse: one says 'reconnect yourself'."""
        theirs = request_challenge_remote(
            CONFIG,
            "b-2",
            request=_transport(
                HttpResult(status=409, body=_envelope("EXTAPI_CHALLENGE_OPPONENT_OFFLINE"))
            ),
        )
        mine = request_challenge_remote(
            CONFIG,
            "b-2",
            request=_transport(HttpResult(status=409, body=_envelope("EXT_BOT_OFFLINE"))),
        )
        assert theirs["error"] == "opponent_offline"
        assert mine["error"] == "bot_offline"
        assert "get_status" in mine["note"]
        assert "get_status" not in theirs["note"]

    def test_outstanding_limit(self) -> None:
        result = request_challenge_remote(
            CONFIG,
            "b-2",
            request=_transport(
                HttpResult(status=429, body=_envelope("EXTAPI_CHALLENGE_OUTSTANDING_LIMIT"))
            ),
        )
        assert result["error"] == "too_many_outstanding"

    def test_404_means_not_deployed(self) -> None:
        result = request_challenge_remote(
            CONFIG, "b-2", request=_transport(HttpResult(status=404, body={}))
        )
        assert result["error"] == "endpoint_not_available"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_inbound_pending_drives_the_note(self) -> None:
        result = request_remote_challenges(
            CONFIG,
            request=_transport(
                HttpResult(
                    status=200,
                    body={
                        "inbound": [{"challenge_id": "c-1", "status": "pending"}],
                        "outbound": [],
                    },
                )
            ),
        )
        assert result["status"] == "ok"
        assert "accept_remote_challenge" in result["note"]

    def test_outbound_pending_drives_the_note(self) -> None:
        result = request_remote_challenges(
            CONFIG,
            request=_transport(
                HttpResult(
                    status=200,
                    body={
                        "inbound": [{"challenge_id": "c-0", "status": "declined"}],
                        "outbound": [{"challenge_id": "c-1", "status": "pending"}],
                    },
                )
            ),
        )
        assert "still pending" in result["note"]

    def test_nothing_open_points_at_discovery(self) -> None:
        result = request_remote_challenges(
            CONFIG,
            request=_transport(HttpResult(status=200, body={"inbound": [], "outbound": []})),
        )
        assert "list_lobby_opponents" in result["note"]

    def test_malformed_lists_do_not_crash(self) -> None:
        """A server that answers oddly must degrade, never raise."""
        result = request_remote_challenges(
            CONFIG,
            request=_transport(HttpResult(status=200, body={"inbound": None, "outbound": "?"})),
        )
        assert result["inbound"] == [] and result["outbound"] == []

    def test_unauthorized(self) -> None:
        result = request_remote_challenges(
            CONFIG,
            request=_transport(HttpResult(status=401, body=_envelope("EXTAPI_INVALID_TOKEN"))),
        )
        assert result["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# accept / decline
# ---------------------------------------------------------------------------


class TestAnswer:
    def test_accept_success_sends_the_agent_to_wait_for_turn(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG,
            "c-1",
            action="accept",
            request=_transport(
                HttpResult(
                    status=200,
                    body={
                        "challenge_id": "c-1",
                        "status": "accepted",
                        "opponent_bot_id": "b-2",
                        "opponent_name": "Rival",
                        "rated": True,
                    },
                    headers={"x-request-id": "req-3"},
                )
            ),
        )
        assert result["status"] == "accepted"
        assert result["rated"] is True
        assert result["request_id"] == "req-3"
        assert "wait_for_turn" in result["note"]
        # No match id on this path, by design.
        assert "match_id" not in result

    def test_decline_success(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG,
            "c-1",
            action="decline",
            request=_transport(
                HttpResult(status=200, body={"challenge_id": "c-1", "status": "declined"})
            ),
        )
        assert result["status"] == "declined"

    def test_challenge_not_found(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG,
            "c-nope",
            action="accept",
            request=_transport(
                HttpResult(status=400, body=_envelope("EXTAPI_CHALLENGE_NOT_FOUND"))
            ),
        )
        assert result["error"] == "challenge_not_found"

    def test_not_pending_tells_the_agent_it_was_too_slow(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG,
            "c-1",
            action="accept",
            request=_transport(
                HttpResult(status=409, body=_envelope("EXTAPI_CHALLENGE_NOT_PENDING"))
            ),
        )
        assert result["error"] == "not_pending"
        assert "expired" in result["note"]

    def test_challenger_left_the_lobby(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG,
            "c-1",
            action="accept",
            request=_transport(
                HttpResult(status=409, body=_envelope("EXTAPI_CHALLENGE_OPPONENT_OFFLINE"))
            ),
        )
        assert result["error"] == "opponent_offline"

    def test_our_own_lobby_is_down(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG,
            "c-1",
            action="accept",
            request=_transport(HttpResult(status=409, body=_envelope("EXT_BOT_OFFLINE"))),
        )
        assert result["error"] == "bot_offline"

    def test_404_means_not_deployed(self) -> None:
        result = request_answer_remote_challenge(
            CONFIG, "c-1", action="accept", request=_transport(HttpResult(status=404, body={}))
        )
        assert result["error"] == "endpoint_not_available"
