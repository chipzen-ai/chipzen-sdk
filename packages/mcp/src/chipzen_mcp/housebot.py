"""HTTP client for agent-initiated house-bot challenges.

Backs the ``challenge_house_bot`` MCP tool: a single, narrow call to the
Chipzen endpoint that lets a ``cz_extbot_`` token start an UNRATED,
relaxed-clock (~30s) practice match against a house bot -- the server-side
feature tracked in chipzen-ai/Chipzen#3750.

Contract status: FINAL -- aligned to the server implementation
(chipzen-ai/Chipzen#3825, ``api/routes/external_api_challenges.py``). The
whole contract stays isolated in this module:

* ``POST /api/external-api/challenges/house-bot`` with
  ``Authorization: Bearer cz_extbot_<token>``. The token IS the bot
  identity; the body's optional ``bot_id`` is a pure cross-check (sent only
  when configured; a mismatch comes back as the opaque 401) and optional
  ``opponent`` names a house bot by UUID or exact name (omitted -> the
  server's default first opponent). The request model forbids extra fields.
* ``200``: ``{match_id, participant_id, gateway_ws_url, opponent,
  opponent_bot_id, rated: false, decision_timeout_ms: 30000}``. The tool
  surfaces ``match_id`` / ``opponent`` / ``opponent_bot_id`` / ``rated`` /
  ``decision_timeout_ms`` and tolerates unknown extras. ``gateway_ws_url``
  is deliberately NOT surfaced: the match reaches this session through the
  normal lobby dispatch; the agent never dials sockets. The accepted
  challenge's ``request_id`` -- read from the ``X-Request-ID`` response
  header (the success body carries no ``request_id``) -- IS surfaced: it
  threads the whole dispatch fan-out in the platform logs, so it is the
  correlator a developer quotes when reporting a problem with the match
  (chipzen-ai/Chipzen#3901).
* Errors arrive in the platform envelope ``{error_code, message,
  request_id}`` and map 1:1 onto tool ``error`` values (the tool re-surfaces
  ``request_id`` from the envelope body, falling back to ``X-Request-ID``):
  ``401 EXTAPI_INVALID_TOKEN`` (opaque -- missing / malformed / unknown /
  revoked token, retired bot, or ``bot_id`` mismatch),
  ``400 EXTAPI_HOUSE_BOT_NOT_FOUND`` (opponent selector miss; the server
  deliberately does NOT 404 for this), ``409 EXT_BOT_OFFLINE``,
  ``429 TOKEN_AT_CONCURRENT_MATCH_CAP`` / ``429 FREE_TIER_LIMIT_EXCEEDED``
  (distinguished by ``error_code``), ``502 EXTAPI_DISPATCH_FAILED``.
* A plain ``404`` on this path still means "endpoint not deployed on this
  environment" (older server) -> the dashboard-fallback message.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from chipzen_mcp.config import McpConfig

logger = logging.getLogger("chipzen_mcp.housebot")

#: Path of the scoped house-bot challenge endpoint. FINAL, confirmed against
#: the server implementation (chipzen-ai/Chipzen#3825).
CHALLENGE_HOUSE_BOT_PATH = "/api/external-api/challenges/house-bot"

#: Server ``error_code`` values from the platform error envelope
#: (``chipzen/errors.py``, #3825). Matched exactly.
CODE_INVALID_TOKEN = "EXTAPI_INVALID_TOKEN"
CODE_HOUSE_BOT_NOT_FOUND = "EXTAPI_HOUSE_BOT_NOT_FOUND"
CODE_BOT_OFFLINE = "EXT_BOT_OFFLINE"
CODE_CONCURRENT_CAP = "TOKEN_AT_CONCURRENT_MATCH_CAP"
CODE_FREE_TIER = "FREE_TIER_LIMIT_EXCEEDED"
CODE_DISPATCH_FAILED = "EXTAPI_DISPATCH_FAILED"

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
    """Transport-agnostic HTTP outcome: status + parsed-JSON body + headers.

    ``headers`` keys are lower-cased so the ``X-Request-ID`` correlator can be
    read case-insensitively (chipzen-ai/Chipzen#3901). The platform returns
    ``request_id`` in the error-envelope body, but on a 2xx success it lives
    ONLY in the ``X-Request-ID`` response header, so surfacing it on the
    success payload needs the header. Defaults to empty for test doubles /
    transports that don't capture headers.
    """

    status: int
    body: dict[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)


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
            return HttpResult(
                status=response.status,
                body=_parse_json(response.read()),
                headers=lower_headers(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            status=exc.code,
            body=_parse_json(exc.read()),
            headers=lower_headers(exc.headers),
        )


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def lower_headers(raw: Any) -> dict[str, str]:
    """Response headers as a lower-cased ``dict`` (empty when absent).

    ``urllib`` hands back an ``http.client.HTTPMessage`` (case-insensitive on
    its own, but stored here as a plain dict so :class:`HttpResult` stays a
    trivial value). Lower-casing the keys lets callers read ``x-request-id``
    without worrying about the wire casing.
    """
    if raw is None:
        return {}
    items = getattr(raw, "items", None)
    if items is None:
        return {}
    return {str(k).lower(): str(v) for k, v in items()}


def response_request_id(result: HttpResult) -> str | None:
    """The platform request correlator for this HTTP outcome, or ``None``.

    Prefers the error-envelope body's ``request_id`` (present on every
    ``{error_code, message, request_id}`` error), falling back to the
    ``X-Request-ID`` response header -- the only place it appears on a 2xx
    success. Never fabricated: ``None`` means the response carried neither
    (chipzen-ai/Chipzen#3901).
    """
    body_id = result.body.get("request_id")
    if isinstance(body_id, str) and body_id:
        return body_id
    header_id = result.headers.get("x-request-id")
    return header_id or None


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


def _server_message(body: dict[str, Any]) -> str:
    """The envelope's user-safe ``message`` (best-effort on odd bodies)."""
    message = body.get("message") or body.get("detail") or ""
    return message if isinstance(message, str) else json.dumps(message)


def _error(error: str, result: HttpResult, note: str) -> dict[str, Any]:
    """Uniform tool-error payload: our stable ``error`` + the server's words.

    ``request_id`` is surfaced from the error envelope (or the ``X-Request-ID``
    header) so a failing call is traceable in the platform logs without
    guessing, and the ``note`` tells the developer to quote it when reporting
    the issue -- for a pre-match failure it is the ONLY correlator that exists
    server-side (chipzen-ai/Chipzen#3901).
    """
    request_id = response_request_id(result)
    return {
        "status": "error",
        "error": error,
        "server_error_code": result.body.get("error_code"),
        "request_id": request_id,
        "detail": _server_message(result.body),
        "note": _with_request_id_hint(note, request_id),
    }


def _with_request_id_hint(note: str, request_id: str | None) -> str:
    """Append the "quote request_id" support hint when we have a correlator."""
    if not request_id:
        return note
    return f"{note} When reporting this to support, quote request_id={request_id}."


def _map_response(result: HttpResult, bot_name: str | None) -> dict[str, Any]:
    """Map the endpoint's HTTP outcome onto the MCP tool payload."""
    body = result.body
    code = body.get("error_code")
    if result.status in (200, 201):
        request_id = response_request_id(result)
        note = (
            "Unrated house-bot challenge accepted. The match is dispatched "
            "to this session's lobby connection -- call wait_for_turn now; "
            "your first decision arrives there."
        )
        if request_id:
            note = (
                f"{note} This challenge's request_id={request_id} threads the "
                "whole dispatch in the platform logs -- quote it if you need "
                "support to trace this match."
            )
        return {
            "status": "challenge_created",
            "match_id": body.get("match_id"),
            "opponent": body.get("opponent", bot_name),
            "opponent_bot_id": body.get("opponent_bot_id"),
            "rated": body.get("rated", False),
            "decision_timeout_ms": body.get("decision_timeout_ms"),
            "request_id": request_id,
            "note": note,
        }
    if result.status == 401:
        return _error(
            "unauthorized",
            result,
            "The server rejected the request (opaque 401: invalid, malformed "
            "or revoked CHIPZEN_EXTBOT_TOKEN, a retired bot, or a "
            "CHIPZEN_BOT_ID that doesn't match the token's bot). Verify both "
            "env vars, or rotate the token from the bot's dashboard page.",
        )
    if result.status == 400 and code == CODE_HOUSE_BOT_NOT_FOUND:
        return _error(
            "house_bot_not_found",
            result,
            f"No house bot matches the requested opponent {bot_name!r}. Pass "
            "a house bot's exact name or UUID, or omit bot_name to challenge "
            "the default house bot.",
        )
    if result.status == 404:
        return _error(
            "endpoint_not_available",
            result,
            "This Chipzen environment does not expose agent-initiated "
            "house-bot challenges yet (server side of "
            "chipzen-ai/Chipzen#3750; staging gets it first). Fallback: "
            "keep this session connected and start an UNRANKED challenge "
            "against a house bot from the dashboard (/challenges) -- the "
            "match will be dispatched here automatically.",
        )
    if result.status == 409 and code == CODE_BOT_OFFLINE:
        return _error(
            "bot_offline",
            result,
            "Your bot has no live lobby presence, so a match could not be "
            "dispatched to it. Check get_status: lobby_connected must be "
            "true (the background session connects it automatically -- give "
            "it a moment or look at session_error), then retry.",
        )
    if result.status == 429 and code == CODE_FREE_TIER:
        return _error(
            "free_tier_limit",
            result,
            "A free-tier usage limit was exceeded for this account. The "
            "detail field says which limit and when it resets.",
        )
    if result.status == 429 and code == CODE_CONCURRENT_CAP:
        return _error(
            "concurrent_cap",
            result,
            "This token is at its concurrent-match cap (5). Finish or wait "
            "out an active match (see list_matches), then retry.",
        )
    if result.status == 502:
        return _error(
            "dispatch_failed",
            result,
            "The platform accepted the challenge but failed to launch the "
            "match (transient executor/allocation error). Retry shortly.",
        )
    return _error(
        f"http_{result.status}",
        result,
        "Unexpected response from the challenge endpoint.",
    )


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
        actionable ``note`` (never an exception for HTTP-level failures). Both
        outcomes carry ``request_id`` (``None`` only when the platform returned
        neither an envelope id nor an ``X-Request-ID`` header, e.g. a transport
        failure with no response) so the call is traceable in the platform logs
        (chipzen-ai/Chipzen#3901).
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
