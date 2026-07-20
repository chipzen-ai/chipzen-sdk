"""Tests for the house-bot challenge endpoint client (mocked transport).

The contract is FINAL, mirrored from the server implementation
(chipzen-ai/Chipzen#3825): these tests pin the client's half exactly --
URL, auth header shape, body, the platform error envelope
(``{error_code, message, request_id}``), and the honest mapping of every
failure class the endpoint emits.
"""

import importlib.metadata
import urllib.error

from chipzen_mcp import __version__
from chipzen_mcp.config import McpConfig
from chipzen_mcp.housebot import (
    CHALLENGE_HOUSE_BOT_PATH,
    CODE_BOT_OFFLINE,
    CODE_CONCURRENT_CAP,
    CODE_DISPATCH_FAILED,
    CODE_FREE_TIER,
    CODE_HOUSE_BOT_NOT_FOUND,
    CODE_INVALID_TOKEN,
    HttpResult,
    api_origin,
    lower_headers,
    request_house_bot_challenge,
    response_request_id,
)

CONFIG = McpConfig(token="cz_extbot_secret", bot_id="b-1", env="staging")


def _envelope(code: str, message: str) -> dict:
    """The platform error envelope (``app_error_handler`` shape)."""
    return {"error_code": code, "message": message, "request_id": "req-1"}


class _RecordingPost:
    """Transport double: records the request, returns a canned result."""

    def __init__(self, result: HttpResult) -> None:
        self.result = result
        self.url: str | None = None
        self.headers: dict[str, str] | None = None
        self.body: dict[str, object] | None = None

    def __call__(self, url: str, headers: dict, body: dict) -> HttpResult:
        self.url, self.headers, self.body = url, headers, body
        return self.result


class TestVersion:
    """chipzen-ai/Chipzen#3885: the runtime ``__version__`` must equal the
    installed distribution version (it is derived from package metadata), so
    it can never drift from the published wheel -- and must never carry a
    ``dev`` placeholder, which would misattribute the outbound User-Agent."""

    def test_matches_installed_distribution_metadata(self) -> None:
        assert __version__ == importlib.metadata.version("chipzen-mcp")

    def test_has_no_dev_placeholder(self) -> None:
        assert "dev" not in __version__

    def test_user_agent_carries_the_metadata_version(self) -> None:
        # The header must reflect the metadata version independently -- not a
        # separately-maintained string that could drift back to a dev suffix.
        post = _RecordingPost(HttpResult(status=200, body={"match_id": "m-1"}))
        request_house_bot_challenge(CONFIG, post=post)
        assert post.headers is not None
        expected = importlib.metadata.version("chipzen-mcp")
        assert post.headers["User-Agent"] == f"chipzen-mcp/{expected}"
        assert "dev" not in post.headers["User-Agent"]


class TestApiOrigin:
    def test_env_mapping(self) -> None:
        assert api_origin(CONFIG) == "https://staging.chipzen.ai"
        assert api_origin(McpConfig(token="t", bot_id="b")) == "https://chipzen.ai"
        assert api_origin(McpConfig(token="t", bot_id="b", env="local")) == (
            "http://localhost:8001"
        )

    def test_lobby_url_override_wins(self) -> None:
        config = McpConfig(
            token="t", bot_id="b", env="prod", lobby_url="wss://dev.example:9000/ws/external/bot/b"
        )
        assert api_origin(config) == "https://dev.example:9000"

    def test_lobby_url_ws_maps_to_http(self) -> None:
        config = McpConfig(token="t", bot_id="b", lobby_url="ws://localhost:8001/ws/x")
        assert api_origin(config) == "http://localhost:8001"


class TestChallengeSuccess:
    def test_request_shape_and_success_mapping(self) -> None:
        post = _RecordingPost(
            HttpResult(
                status=200,
                body={
                    "match_id": "m-42",
                    "participant_id": "p-1",
                    "gateway_ws_url": "/ws/external/match/m-42/p-1",
                    "opponent": "boss-1",
                    "opponent_bot_id": "hb-9",
                    "rated": False,
                    "decision_timeout_ms": 30000,
                },
            )
        )
        result = request_house_bot_challenge(CONFIG, "boss-1", post=post)

        # Request half of the contract.
        assert post.url == f"https://staging.chipzen.ai{CHALLENGE_HOUSE_BOT_PATH}"
        assert post.headers is not None
        assert post.headers["Authorization"] == "Bearer cz_extbot_secret"
        assert post.headers["User-Agent"] == f"chipzen-mcp/{__version__}"
        # bot_id is the optional cross-check; opponent the selector. The
        # request model is extra="forbid" server-side, so nothing else may
        # ever be sent.
        assert post.body == {"bot_id": "b-1", "opponent": "boss-1"}

        # Response half: surface the agent-relevant fields, tolerate the rest
        # (participant_id / gateway_ws_url are session plumbing, not agent
        # decisions -- the match arrives via the normal lobby dispatch).
        assert result["status"] == "challenge_created"
        assert result["match_id"] == "m-42"
        assert result["opponent"] == "boss-1"
        assert result["opponent_bot_id"] == "hb-9"
        assert result["rated"] is False
        assert result["decision_timeout_ms"] == 30000
        assert "wait_for_turn" in result["note"]
        assert "gateway_ws_url" not in result

    def test_default_opponent_omitted_from_body(self) -> None:
        post = _RecordingPost(HttpResult(status=200, body={"match_id": "m-1"}))
        result = request_house_bot_challenge(CONFIG, post=post)
        assert post.body == {"bot_id": "b-1"}
        assert result["status"] == "challenge_created"
        assert result["rated"] is False  # unrated is the endpoint's contract

    def test_bot_id_omitted_when_not_configured(self) -> None:
        # Lobby-URL-only setups have no bot_id; the token is the identity,
        # so the cross-check field is simply left out.
        config = McpConfig(
            token="cz_extbot_secret", bot_id="", lobby_url="ws://localhost:8001/ws/x"
        )
        post = _RecordingPost(HttpResult(status=200, body={"match_id": "m-1"}))
        request_house_bot_challenge(config, "boss-1", post=post)
        assert post.body == {"opponent": "boss-1"}

    def test_unknown_response_fields_are_tolerated(self) -> None:
        post = _RecordingPost(
            HttpResult(status=200, body={"match_id": "m-1", "brand_new_field": {"x": 1}})
        )
        result = request_house_bot_challenge(CONFIG, post=post)
        assert result["status"] == "challenge_created"

    def test_request_id_surfaced_from_header_on_success(self) -> None:
        # A 2xx success body carries NO request_id (it's set on the route's
        # response_model, not the envelope); the id lives only in the
        # X-Request-ID response header -- surface it so the accepted challenge
        # is traceable in the platform logs (chipzen-ai/Chipzen#3901).
        post = _RecordingPost(
            HttpResult(
                status=200,
                body={"match_id": "m-1", "rated": False},
                headers={"x-request-id": "18fe80c7-abc"},
            )
        )
        result = request_house_bot_challenge(CONFIG, post=post)
        assert result["status"] == "challenge_created"
        assert result["request_id"] == "18fe80c7-abc"
        # The note tells the developer to quote it.
        assert "18fe80c7-abc" in result["note"]
        assert "request_id" in result["note"]

    def test_request_id_is_none_when_no_header_present(self) -> None:
        # No header, no envelope id -> request_id is None (never fabricated),
        # and the note stays free of a bogus correlator.
        post = _RecordingPost(HttpResult(status=200, body={"match_id": "m-1"}))
        result = request_house_bot_challenge(CONFIG, post=post)
        assert result["request_id"] is None
        assert "request_id" not in result["note"]


class TestChallengeErrors:
    def _result(self, status: int, body: dict | None = None) -> dict:
        post = _RecordingPost(HttpResult(status=status, body=body or {}))
        return request_house_bot_challenge(CONFIG, "boss-1", post=post)

    def test_401_invalid_token_is_opaque(self) -> None:
        result = self._result(
            401, _envelope(CODE_INVALID_TOKEN, "Invalid or revoked external-API token.")
        )
        assert result["status"] == "error" and result["error"] == "unauthorized"
        assert result["server_error_code"] == CODE_INVALID_TOKEN
        # The 401 is opaque server-side; the note must name BOTH env vars a
        # user can actually fix (token AND the bot_id cross-check).
        assert "CHIPZEN_EXTBOT_TOKEN" in result["note"]
        assert "CHIPZEN_BOT_ID" in result["note"]
        # #3901: for a pre-match failure request_id is the ONLY correlator that
        # exists server-side -- surface it and tell the dev to quote it.
        assert result["request_id"] == "req-1"
        assert "req-1" in result["note"]

    def test_error_request_id_surfaced_on_every_envelope(self) -> None:
        # request_id rides the platform error envelope on every failure class,
        # so it must be surfaced on each one (not just the 401 above).
        for status, code in (
            (400, CODE_HOUSE_BOT_NOT_FOUND),
            (409, CODE_BOT_OFFLINE),
            (429, CODE_FREE_TIER),
            (429, CODE_CONCURRENT_CAP),
            (502, CODE_DISPATCH_FAILED),
        ):
            result = self._result(status, _envelope(code, "msg"))
            assert result["request_id"] == "req-1", (status, code)

    def test_error_request_id_falls_back_to_header(self) -> None:
        # An edge/proxy failure whose body lost the envelope id still has the
        # X-Request-ID header -- fall back to it rather than dropping the id.
        post = _RecordingPost(
            HttpResult(status=503, body={"message": "maintenance"}, headers={"x-request-id": "hx"})
        )
        result = request_house_bot_challenge(CONFIG, "boss-1", post=post)
        assert result["error"] == "http_503"
        assert result["request_id"] == "hx"

    def test_error_request_id_is_none_without_any_id(self) -> None:
        result = self._result(429, {})  # no envelope, no header
        assert result["error"] == "http_429"
        assert result["request_id"] is None
        assert "request_id" not in result["note"]

    def test_400_house_bot_not_found(self) -> None:
        result = self._result(400, _envelope(CODE_HOUSE_BOT_NOT_FOUND, "House bot not found."))
        assert result["error"] == "house_bot_not_found"
        assert "boss-1" in result["note"]  # names the selector that missed
        assert "omit" in result["note"]  # points at the default-opponent path

    def test_400_without_the_code_is_not_misclassified(self) -> None:
        result = self._result(400, _envelope("BAD_REQUEST_001", "Bad request"))
        assert result["error"] == "http_400"

    def test_404_server_not_deployed_yet(self) -> None:
        # Route-absent on an older deployment -> generic envelope, no
        # endpoint error_code. Selector misses are 400 by server contract,
        # so 404 unambiguously means "endpoint not deployed here".
        result = self._result(404, {"error_code": "NOT_FOUND_001", "message": "Not Found"})
        assert result["error"] == "endpoint_not_available"
        assert "dashboard" in result["note"]
        assert "3750" in result["note"]

    def test_409_bot_offline(self) -> None:
        result = self._result(
            409, _envelope(CODE_BOT_OFFLINE, "Bot is not connected to its lobby WebSocket.")
        )
        assert result["error"] == "bot_offline"
        assert "get_status" in result["note"]

    def test_429_concurrent_cap(self) -> None:
        result = self._result(
            429, _envelope(CODE_CONCURRENT_CAP, "This token is at its concurrent-match cap.")
        )
        assert result["error"] == "concurrent_cap"
        assert "list_matches" in result["note"]

    def test_429_free_tier_is_distinguished(self) -> None:
        message = "Free-tier limit exceeded: daily_matches (10/10); resets at 00:00Z."
        result = self._result(429, _envelope(CODE_FREE_TIER, message))
        assert result["error"] == "free_tier_limit"
        assert result["detail"] == message  # the server names limit + reset

    def test_429_without_a_known_code_stays_generic(self) -> None:
        # An edge/proxy 429 (no platform envelope) must not masquerade as
        # the concurrent-match cap.
        result = self._result(429, {})
        assert result["error"] == "http_429"

    def test_502_dispatch_failed(self) -> None:
        result = self._result(
            502, _envelope(CODE_DISPATCH_FAILED, "Match dispatch failed. Try again shortly.")
        )
        assert result["error"] == "dispatch_failed"
        assert "Retry" in result["note"]

    def test_unexpected_status(self) -> None:
        result = self._result(503, {"message": "maintenance"})
        assert result["error"] == "http_503"
        assert result["detail"] == "maintenance"

    def test_network_failure_is_reported_not_raised(self) -> None:
        def broken(url: str, headers: dict, body: dict) -> HttpResult:
            raise urllib.error.URLError("connection refused")

        result = request_house_bot_challenge(CONFIG, post=broken)
        assert result["status"] == "error" and result["error"] == "network"
        assert "connection refused" in result["detail"]


class TestResponseRequestId:
    """Unit tests for the request_id extractor: body wins, header is the
    fallback (chipzen-ai/Chipzen#3901)."""

    def test_body_request_id_wins(self) -> None:
        result = HttpResult(
            status=401,
            body={"request_id": "from-body"},
            headers={"x-request-id": "from-header"},
        )
        assert response_request_id(result) == "from-body"

    def test_header_is_used_when_body_has_none(self) -> None:
        result = HttpResult(status=200, body={"match_id": "m"}, headers={"x-request-id": "hx"})
        assert response_request_id(result) == "hx"

    def test_none_when_neither_present(self) -> None:
        assert response_request_id(HttpResult(status=200, body={})) is None

    def test_empty_body_id_falls_through_to_header(self) -> None:
        result = HttpResult(status=200, body={"request_id": ""}, headers={"x-request-id": "hx"})
        assert response_request_id(result) == "hx"


class TestLowerHeaders:
    def test_lower_cases_keys_for_case_insensitive_lookup(self) -> None:
        # urllib hands back mixed-case header names; we normalise so callers
        # read `x-request-id` regardless of the wire casing.
        headers = lower_headers({"X-Request-ID": "abc", "Content-Type": "application/json"})
        assert headers["x-request-id"] == "abc"
        assert headers["content-type"] == "application/json"

    def test_none_yields_empty(self) -> None:
        assert lower_headers(None) == {}
