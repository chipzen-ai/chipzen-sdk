"""Tests for the rated-matchmaking queue client (mocked transport).

The contract is FINAL, mirrored from the server implementation
(chipzen-ai/Chipzen#3910): these tests pin the client's half exactly --
URL + HTTP method, auth header shape, body, the platform error envelope
(``{error_code, message, request_id}``), and the honest mapping of every
queue status + failure class the three endpoints emit.
"""

import urllib.error

from chipzen_mcp import __version__
from chipzen_mcp.config import McpConfig
from chipzen_mcp.housebot import CODE_BOT_OFFLINE, CODE_INVALID_TOKEN, HttpResult, api_origin
from chipzen_mcp.matchmaking import (
    CODE_MATCHMAKING_INELIGIBLE,
    MATCHMAKING_JOIN_PATH,
    MATCHMAKING_LEAVE_PATH,
    MATCHMAKING_STATUS_PATH,
    request_join_rated_queue,
    request_leave_rated_queue,
    request_rated_queue_status,
)

CONFIG = McpConfig(token="cz_extbot_secret", bot_id="b-1", env="staging")


def _envelope(code: str, message: str) -> dict:
    """The platform error envelope (``app_error_handler`` shape)."""
    return {"error_code": code, "message": message, "request_id": "req-1"}


class _RecordingRequest:
    """Transport double: records the request, returns a canned result."""

    def __init__(self, result: HttpResult) -> None:
        self.result = result
        self.method: str | None = None
        self.url: str | None = None
        self.headers: dict[str, str] | None = None
        self.body: dict[str, object] | None = None

    def __call__(self, method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
        self.method, self.url, self.headers, self.body = method, url, headers, body
        return self.result


class TestJoinRequestShape:
    def test_url_method_auth_and_body(self) -> None:
        req = _RecordingRequest(
            HttpResult(status=200, body={"status": "queued", "position": 1, "rated": True})
        )
        request_join_rated_queue(CONFIG, request=req)
        assert req.method == "POST"
        assert req.url == f"https://staging.chipzen.ai{MATCHMAKING_JOIN_PATH}"
        assert req.headers is not None
        assert req.headers["Authorization"] == "Bearer cz_extbot_secret"
        assert req.headers["User-Agent"] == f"chipzen-mcp/{__version__}"
        # bot_id is the optional cross-check; the request model is
        # extra="forbid" server-side, so nothing else may ever be sent.
        assert req.body == {"bot_id": "b-1"}

    def test_bot_id_omitted_when_not_configured(self) -> None:
        # Lobby-URL-only setups have no bot_id; the token is the identity, so
        # the cross-check field is simply left out (empty body).
        config = McpConfig(
            token="cz_extbot_secret", bot_id="", lobby_url="ws://localhost:8001/ws/x"
        )
        req = _RecordingRequest(HttpResult(status=200, body={"status": "queued"}))
        request_join_rated_queue(config, request=req)
        assert req.body == {}
        assert api_origin(config) == "http://localhost:8001"


class TestJoinSuccess:
    def test_matched_immediate_pair(self) -> None:
        req = _RecordingRequest(
            HttpResult(
                status=200, body={"status": "matched", "queue_ttl_seconds": 60, "rated": True}
            )
        )
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["status"] == "matched"
        assert result["rated"] is True
        # No match id by design (seating comes via the lobby push, house-bot
        # parity) -- the note points the agent at the wait_for_turn loop.
        assert "match_id" not in result
        assert "wait_for_turn" in result["note"]

    def test_queued_reports_position(self) -> None:
        req = _RecordingRequest(
            HttpResult(
                status=200,
                body={"status": "queued", "position": 3, "queue_ttl_seconds": 60, "rated": True},
            )
        )
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["status"] == "queued"
        assert result["position"] == 3
        assert result["queue_ttl_seconds"] == 60
        assert result["rated"] is True
        # The queued note must teach both the timeout-recovery and cancel paths.
        assert "rated_queue_status" in result["note"]
        assert "leave_rated_queue" in result["note"]

    def test_rated_defaults_true_when_omitted(self) -> None:
        # The queue is rated by definition; a body that omits `rated` still
        # surfaces rated=True so the host can display "RATED".
        req = _RecordingRequest(HttpResult(status=200, body={"status": "queued"}))
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["rated"] is True

    def test_request_id_surfaced_from_header_on_matched(self) -> None:
        # The success body carries no request_id (it's on the route's
        # response_model, not the envelope); the id lives only in the
        # X-Request-ID header -- surface it, and tell the dev to quote it for
        # the dispatched match (chipzen-ai/Chipzen#3901, house-bot parity).
        req = _RecordingRequest(
            HttpResult(
                status=200,
                body={"status": "matched", "rated": True},
                headers={"x-request-id": "18fe80c7-abc"},
            )
        )
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["status"] == "matched"
        assert result["request_id"] == "18fe80c7-abc"
        assert "18fe80c7-abc" in result["note"]

    def test_request_id_surfaced_from_header_on_queued(self) -> None:
        req = _RecordingRequest(
            HttpResult(
                status=200,
                body={"status": "queued", "position": 1},
                headers={"x-request-id": "hx-q"},
            )
        )
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["status"] == "queued"
        assert result["request_id"] == "hx-q"

    def test_request_id_is_none_when_no_header(self) -> None:
        # No header and no envelope id on the success body -> request_id is
        # None (never fabricated).
        req = _RecordingRequest(HttpResult(status=200, body={"status": "queued"}))
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["request_id"] is None


class TestJoinErrors:
    def _result(self, status: int, body: dict | None = None) -> dict:
        req = _RecordingRequest(HttpResult(status=status, body=body or {}))
        return request_join_rated_queue(CONFIG, request=req)

    def test_401_invalid_token_is_opaque(self) -> None:
        result = self._result(
            401, _envelope(CODE_INVALID_TOKEN, "Invalid or revoked external-API token.")
        )
        assert result["status"] == "error" and result["error"] == "unauthorized"
        assert result["server_error_code"] == CODE_INVALID_TOKEN
        # #3901: the request_id is surfaced so the failure is traceable, and the
        # note tells the developer to quote it (house-bot parity).
        assert result["request_id"] == "req-1"
        assert "req-1" in result["note"]
        assert "CHIPZEN_EXTBOT_TOKEN" in result["note"]
        assert "CHIPZEN_BOT_ID" in result["note"]

    def test_error_request_id_falls_back_to_header(self) -> None:
        # An edge/proxy failure whose body lost the envelope id still has the
        # X-Request-ID header -- fall back to it rather than dropping the id.
        req = _RecordingRequest(
            HttpResult(status=503, body={"message": "maintenance"}, headers={"x-request-id": "hx"})
        )
        result = request_join_rated_queue(CONFIG, request=req)
        assert result["error"] == "http_503"
        assert result["request_id"] == "hx"

    def test_error_request_id_is_none_without_any_id(self) -> None:
        # No envelope id, no header -> request_id is None and the note stays
        # free of a bogus correlator.
        result = self._result(429, {})
        assert result["error"] == "http_429"
        assert result["request_id"] is None
        assert "request_id" not in result["note"]

    def test_403_ineligible_system_bot(self) -> None:
        result = self._result(
            403, _envelope(CODE_MATCHMAKING_INELIGIBLE, "Not eligible for rated matchmaking.")
        )
        assert result["error"] == "ineligible"
        assert result["server_error_code"] == CODE_MATCHMAKING_INELIGIBLE

    def test_403_without_the_code_is_not_misclassified(self) -> None:
        result = self._result(403, _envelope("AUTH_002", "Access revoked."))
        assert result["error"] == "http_403"

    def test_409_bot_offline(self) -> None:
        result = self._result(
            409, _envelope(CODE_BOT_OFFLINE, "Bot is not connected to its lobby WebSocket.")
        )
        assert result["error"] == "bot_offline"
        assert "get_status" in result["note"]

    def test_404_server_not_deployed_yet(self) -> None:
        result = self._result(404, {"error_code": "NOT_FOUND_001", "message": "Not Found"})
        assert result["error"] == "endpoint_not_available"
        assert "3907" in result["note"]

    def test_unexpected_status(self) -> None:
        result = self._result(503, {"message": "maintenance"})
        assert result["error"] == "http_503"
        assert result["detail"] == "maintenance"

    def test_network_failure_is_reported_not_raised(self) -> None:
        def broken(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            raise urllib.error.URLError("connection refused")

        result = request_join_rated_queue(CONFIG, request=broken)
        assert result["status"] == "error" and result["error"] == "network"
        assert "connection refused" in result["detail"]


class TestStatus:
    def test_request_shape_is_a_get_with_no_body(self) -> None:
        req = _RecordingRequest(HttpResult(status=200, body={"status": "idle"}))
        request_rated_queue_status(CONFIG, request=req)
        assert req.method == "GET"
        assert req.url == f"https://staging.chipzen.ai{MATCHMAKING_STATUS_PATH}"
        assert req.body is None  # GET carries no body
        assert req.headers is not None
        assert req.headers["Authorization"] == "Bearer cz_extbot_secret"

    def test_queued(self) -> None:
        req = _RecordingRequest(
            HttpResult(
                status=200,
                body={
                    "status": "queued",
                    "position": 1,
                    "waiting_seconds": 12,
                    "queue_ttl_seconds": 60,
                },
            )
        )
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["status"] == "queued"
        assert result["position"] == 1
        assert result["waiting_seconds"] == 12
        assert "wait_for_turn" in result["note"]

    def test_timed_out(self) -> None:
        req = _RecordingRequest(
            HttpResult(
                status=200,
                body={"status": "timed_out", "waiting_seconds": 61, "queue_ttl_seconds": 60},
            )
        )
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["status"] == "timed_out"
        # The note must point the agent at re-joining (the entry is now dropped).
        assert "join_rated_queue" in result["note"]

    def test_idle(self) -> None:
        req = _RecordingRequest(HttpResult(status=200, body={"status": "idle"}))
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["status"] == "idle"
        assert "join_rated_queue" in result["note"]

    def test_request_id_surfaced_from_header_on_success(self) -> None:
        # A status read's body carries no request_id; surface it from the
        # X-Request-ID header so the poll is traceable (chipzen-ai/Chipzen#3901).
        req = _RecordingRequest(
            HttpResult(status=200, body={"status": "queued"}, headers={"x-request-id": "hx-s"})
        )
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["status"] == "queued"
        assert result["request_id"] == "hx-s"

    def test_401_is_opaque(self) -> None:
        req = _RecordingRequest(
            HttpResult(status=401, body=_envelope(CODE_INVALID_TOKEN, "Invalid token."))
        )
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["error"] == "unauthorized"
        assert result["request_id"] == "req-1"

    def test_404_not_deployed(self) -> None:
        req = _RecordingRequest(HttpResult(status=404, body={}))
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["error"] == "endpoint_not_available"

    def test_unexpected_status_value_is_echoed(self) -> None:
        # A 200 with an unrecognised status string is surfaced verbatim (never
        # silently relabelled) with a generic note.
        req = _RecordingRequest(HttpResult(status=200, body={"status": "weird"}))
        result = request_rated_queue_status(CONFIG, request=req)
        assert result["status"] == "weird"
        assert "Unexpected" in result["note"]

    def test_network_failure_is_reported_not_raised(self) -> None:
        def broken(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            raise urllib.error.URLError("dns")

        result = request_rated_queue_status(CONFIG, request=broken)
        assert result["error"] == "network"


class TestLeave:
    def test_request_shape_is_a_post(self) -> None:
        req = _RecordingRequest(HttpResult(status=200, body={"status": "left"}))
        request_leave_rated_queue(CONFIG, request=req)
        assert req.method == "POST"
        assert req.url == f"https://staging.chipzen.ai{MATCHMAKING_LEAVE_PATH}"
        assert req.body == {"bot_id": "b-1"}

    def test_left(self) -> None:
        req = _RecordingRequest(HttpResult(status=200, body={"status": "left"}))
        result = request_leave_rated_queue(CONFIG, request=req)
        assert result["status"] == "left"
        assert "Removed" in result["note"]

    def test_not_queued_is_idempotent(self) -> None:
        req = _RecordingRequest(HttpResult(status=200, body={"status": "not_queued"}))
        result = request_leave_rated_queue(CONFIG, request=req)
        assert result["status"] == "not_queued"
        assert "idempotent" in result["note"]

    def test_request_id_surfaced_from_header_on_success(self) -> None:
        # The leave response body carries no request_id; surface it from the
        # X-Request-ID header for traceability (chipzen-ai/Chipzen#3901).
        req = _RecordingRequest(
            HttpResult(status=200, body={"status": "left"}, headers={"x-request-id": "hx-l"})
        )
        result = request_leave_rated_queue(CONFIG, request=req)
        assert result["status"] == "left"
        assert result["request_id"] == "hx-l"

    def test_401_is_opaque(self) -> None:
        req = _RecordingRequest(
            HttpResult(status=401, body=_envelope(CODE_INVALID_TOKEN, "Invalid token."))
        )
        result = request_leave_rated_queue(CONFIG, request=req)
        assert result["error"] == "unauthorized"

    def test_network_failure_is_reported_not_raised(self) -> None:
        def broken(method: str, url: str, headers: dict, body: dict | None) -> HttpResult:
            raise OSError("boom")

        result = request_leave_rated_queue(CONFIG, request=broken)
        assert result["error"] == "network"
