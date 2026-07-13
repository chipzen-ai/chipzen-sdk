"""Tests for the house-bot challenge endpoint client (mocked transport).

The endpoint contract (chipzen-ai/Chipzen#3750) is speculative until the
server PR lands; these tests pin THIS client's half of it -- URL, auth
header shape, body, and the honest mapping of every failure class -- so
aligning with the final server contract is a constants-level change with a
loud test diff.
"""

import urllib.error

from chipzen_mcp import __version__
from chipzen_mcp.config import McpConfig
from chipzen_mcp.housebot import (
    CHALLENGE_HOUSE_BOT_PATH,
    HttpResult,
    api_origin,
    request_house_bot_challenge,
)

CONFIG = McpConfig(token="cz_extbot_secret", bot_id="b-1", env="staging")


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
                status=201,
                body={
                    "match_id": "m-42",
                    "opponent": "boss-1",
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
        assert post.body == {"bot_id": "b-1", "opponent": "boss-1"}

        # Response half.
        assert result["status"] == "challenge_created"
        assert result["match_id"] == "m-42"
        assert result["opponent"] == "boss-1"
        assert result["rated"] is False
        assert result["decision_timeout_ms"] == 30000
        assert "wait_for_turn" in result["note"]

    def test_default_opponent_omitted_from_body(self) -> None:
        post = _RecordingPost(HttpResult(status=200, body={"match_id": "m-1"}))
        result = request_house_bot_challenge(CONFIG, post=post)
        assert post.body == {"bot_id": "b-1"}
        assert result["status"] == "challenge_created"
        assert result["rated"] is False  # unrated is the endpoint's contract


class TestChallengeErrors:
    def _result(self, status: int, body: dict | None = None) -> dict:
        post = _RecordingPost(HttpResult(status=status, body=body or {}))
        return request_house_bot_challenge(CONFIG, "boss-1", post=post)

    def test_401_bad_token(self) -> None:
        result = self._result(401)
        assert result["status"] == "error" and result["error"] == "unauthorized"
        assert "CHIPZEN_EXTBOT_TOKEN" in result["note"]

    def test_403_feature_off(self) -> None:
        result = self._result(403, {"detail": "house-bot challenges disabled"})
        assert result["error"] == "forbidden"
        assert result["detail"] == "house-bot challenges disabled"

    def test_404_server_not_deployed_yet(self) -> None:
        result = self._result(404)
        assert result["error"] == "endpoint_not_available"
        # Must spell out the interim dashboard fallback, not just fail.
        assert "dashboard" in result["note"]
        assert "3750" in result["note"]

    def test_409_and_429_cap_or_conflict(self) -> None:
        for status in (409, 429):
            result = self._result(status, {"detail": "concurrent cap reached"})
            assert result["error"] == "cap_or_conflict", status
            assert result["detail"] == "concurrent cap reached"
            assert "get_status" in result["note"]

    def test_unexpected_status(self) -> None:
        result = self._result(503, {"detail": "maintenance"})
        assert result["error"] == "http_503"
        assert result["detail"] == "maintenance"

    def test_network_failure_is_reported_not_raised(self) -> None:
        def broken(url: str, headers: dict, body: dict) -> HttpResult:
            raise urllib.error.URLError("connection refused")

        result = request_house_bot_challenge(CONFIG, post=broken)
        assert result["status"] == "error" and result["error"] == "network"
        assert "connection refused" in result["detail"]
