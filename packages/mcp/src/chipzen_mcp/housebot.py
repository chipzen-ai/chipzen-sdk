"""HTTP client for agent-initiated house-bot challenges.

Backs the ``challenge_house_bot`` MCP tool: a single, narrow call to the
Chipzen endpoint that lets a ``cz_extbot_`` token start an UNRATED,
relaxed-clock (~30s) practice match against a house bot -- the server-side
feature tracked in chipzen-ai/Chipzen#3750.

Contract status
---------------

The server side is being implemented concurrently and its HTTP contract is
not final. EVERY assumption about it is isolated in this module and pinned
to named constants:

* :data:`CHALLENGE_HOUSE_BOT_PATH` -- the endpoint path (derived from the
  established External-API route shape, ``/api/external-api/...``).
* :func:`_build_request` -- request body + auth header
  (``Authorization: Bearer cz_extbot_...``; the token is bot-scoped, but
  ``bot_id`` is sent too so the server can cross-check).
* :func:`_map_response` -- response fields consumed (``match_id``,
  ``opponent``, ``rated``, ``decision_timeout_ms``) and error mapping.

When the server PR lands, aligning this client is a constants-level edit
here plus the mocked-endpoint tests in ``tests/test_housebot.py`` -- nothing
outside this module encodes the contract.

Error surface is deliberately honest: bad token (401), house-bot challenges
disabled (403), endpoint not deployed on this environment yet (404, with the
dashboard fallback spelled out), concurrency cap / bot-offline conflicts
(409/429), and plain network failure each map to a distinct ``error`` code
the agent can act on.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from chipzen_mcp.config import McpConfig

logger = logging.getLogger("chipzen_mcp.housebot")

#: Path of the scoped house-bot challenge endpoint (chipzen-ai/Chipzen#3750).
#: SPECULATIVE until the server PR merges -- follows the External-API route
#: family (``POST /api/external-api/bots/{bot_id}/tokens`` etc.). If the
#: server lands elsewhere, this constant is the one-line fix.
CHALLENGE_HOUSE_BOT_PATH = "/api/external-api/challenges/house-bot"

#: HTTP origins per environment, mirroring the SDK's lobby-URL env mapping
#: (``chipzen.connect._ENV_URL_TEMPLATES``) with ws(s) -> http(s).
_ENV_API_ORIGINS: dict[str, str] = {
    "prod": "https://chipzen.ai",
    "staging": "https://staging.chipzen.ai",
    "local": "http://localhost:8001",
}

_REQUEST_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class HttpResult:
    """Transport-agnostic HTTP outcome: status + parsed-JSON body."""

    status: int
    body: dict[str, Any]


#: Transport signature: ``(url, headers, json_body) -> HttpResult``.
#: Injectable so tests exercise the full mapping without a socket.
HttpPost = Callable[[str, dict[str, str], dict[str, Any]], HttpResult]


def api_origin(config: McpConfig) -> str:
    """HTTP origin of the Chipzen API for this configuration.

    An explicit ``CHIPZEN_LOBBY_URL`` wins (its origin, ws->http scheme) so
    local/dev setups target the same host their WS session uses; otherwise
    the env mapping applies.
    """
    if config.lobby_url:
        parts = urlsplit(config.lobby_url)
        scheme = "https" if parts.scheme in ("wss", "https") else "http"
        if parts.netloc:
            return f"{scheme}://{parts.netloc}"
    return _ENV_API_ORIGINS[config.env or "prod"]


def _urllib_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> HttpResult:
    """Default transport: stdlib POST (no new runtime dependency).

    An explicit User-Agent is required -- the platform edge drops default
    library UAs (see chipzen-ai/chipzen-sdk#46 for the WS equivalent).
    """
    payload = json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
            return HttpResult(status=response.status, body=_parse_json(response.read()))
    except urllib.error.HTTPError as exc:
        return HttpResult(status=exc.code, body=_parse_json(exc.read()))


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_request(
    config: McpConfig, bot_name: str | None
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Assemble (url, headers, body) -- the request half of the contract."""
    from chipzen_mcp import __version__

    url = f"{api_origin(config)}{CHALLENGE_HOUSE_BOT_PATH}"
    headers = {
        "Authorization": f"Bearer {config.token}",
        "Content-Type": "application/json",
        "User-Agent": f"chipzen-mcp/{__version__}",
    }
    body: dict[str, Any] = {}
    if config.bot_id:
        body["bot_id"] = config.bot_id
    if bot_name:
        body["opponent"] = bot_name
    return url, headers, body


def _detail(body: dict[str, Any]) -> str:
    """Best-effort server-supplied error detail."""
    detail = body.get("detail") or body.get("message") or body.get("error") or ""
    return detail if isinstance(detail, str) else json.dumps(detail)


def _map_response(result: HttpResult, bot_name: str | None) -> dict[str, Any]:
    """Map the endpoint's HTTP outcome onto the MCP tool payload."""
    body = result.body
    if result.status in (200, 201):
        return {
            "status": "challenge_created",
            "match_id": body.get("match_id"),
            "opponent": body.get("opponent", bot_name),
            "rated": body.get("rated", False),
            "decision_timeout_ms": body.get("decision_timeout_ms"),
            "note": (
                "Unrated house-bot challenge accepted. The match is dispatched "
                "to this session's lobby connection -- call wait_for_turn now; "
                "your first decision arrives there."
            ),
        }
    if result.status == 401:
        return {
            "status": "error",
            "error": "unauthorized",
            "note": (
                "The server rejected the CHIPZEN_EXTBOT_TOKEN (invalid or "
                "revoked). Verify the token, or rotate it from the bot's "
                "dashboard page and update the MCP server config."
            ),
        }
    if result.status == 403:
        return {
            "status": "error",
            "error": "forbidden",
            "detail": _detail(body),
            "note": (
                "The server refused the challenge. Agent-initiated house-bot "
                "challenges may be disabled on this environment, or this "
                "token's bot is not allowed to use them."
            ),
        }
    if result.status == 404:
        return {
            "status": "error",
            "error": "endpoint_not_available",
            "note": (
                "This Chipzen environment does not expose agent-initiated "
                "house-bot challenges yet (server side of "
                "chipzen-ai/Chipzen#3750; staging gets it first). Fallback: "
                "keep this session connected and start an UNRANKED challenge "
                "against a house bot from the dashboard (/challenges) -- the "
                "match will be dispatched here automatically."
            ),
        }
    if result.status in (409, 429):
        return {
            "status": "error",
            "error": "cap_or_conflict",
            "detail": _detail(body),
            "note": (
                "The challenge was refused: either the per-token concurrent-"
                "match cap (5) is used up, or your bot is not currently "
                "connected to the lobby. Check get_status (lobby_connected "
                "must be true), finish or wait out an active match, then retry."
            ),
        }
    return {
        "status": "error",
        "error": f"http_{result.status}",
        "detail": _detail(body),
        "note": "Unexpected response from the challenge endpoint.",
    }


def request_house_bot_challenge(
    config: McpConfig,
    bot_name: str | None = None,
    *,
    post: HttpPost | None = None,
) -> dict[str, Any]:
    """Ask the platform to start an unrated house-bot challenge.

    Args:
        config: Resolved server configuration (token, bot id, env).
        bot_name: Optional named house bot to face; ``None`` lets the server
            pick its default first opponent.
        post: Transport override for tests; ``None`` uses stdlib urllib.

    Returns:
        The MCP tool payload -- ``status: "challenge_created"`` with match
        details, or ``status: "error"`` with a distinct ``error`` code and an
        actionable ``note`` (never an exception for HTTP-level failures).
    """
    transport = post if post is not None else _urllib_post
    url, headers, body = _build_request(config, bot_name)
    try:
        result = transport(url, headers, body)
    except (OSError, urllib.error.URLError) as exc:
        reason = str(getattr(exc, "reason", None) or exc) or exc.__class__.__name__
        return {
            "status": "error",
            "error": "network",
            "detail": reason,
            "note": f"Could not reach {url}: {reason}",
        }
    return _map_response(result, bot_name)
