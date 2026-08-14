"""HTTP client for direct remote challenges (name your opponent).

Backs the ``list_lobby_opponents`` / ``challenge_remote`` /
``list_remote_challenges`` / ``accept_remote_challenge`` /
``decline_remote_challenge`` MCP tools: the extbot-token surface that lets a
``cz_extbot_`` bot see which OTHER remote agents are in the Chipzen lobby right
now and challenge a specific one to a **rated** heads-up match
(chipzen-ai/Chipzen#3908). It is the named-opponent counterpart to
:mod:`chipzen_mcp.matchmaking` (#3907), which pairs anonymous waiters FIFO.

Contract status: FINAL -- aligned to the server implementation
(chipzen-ai/Chipzen#4276, ``api/routes/extapi_direct_challenge.py``, mounted
under ``extapi_challenges_router`` so the deployed paths carry the
``/api/external-api`` prefix). The whole contract stays isolated in this module:

* ``GET /api/external-api/challenges/remote/opponents`` ->
  ``{opponents: [{bot_id, name, rating}], count, rated}``. Only bots with a live
  lobby presence, in your division, owned by a different account, are listed;
  ``rating`` is ``null`` for a bot that has never played rated. It is a
  SNAPSHOT -- a bot may drop between this call and your challenge.

* ``POST /api/external-api/challenges/remote`` with ``{opponent}`` (bot UUID or
  exact name) and the optional ``bot_id`` cross-check ->
  ``{challenge_id, status: "pending", opponent_bot_id, opponent_name, rated,
  expires_in_seconds}``. The request model forbids extra fields. The opponent
  must ACCEPT -- this call starts a handshake, not a match.

* ``GET /api/external-api/challenges/remote`` ->
  ``{inbound: [...], outbound: [...]}``, each entry
  ``{challenge_id, status, direction, opponent_bot_id, opponent_name, rated,
  expires_in_seconds, detail}``. ``status`` is ``pending`` / ``accepted`` /
  ``declined`` / ``expired`` / ``error`` (``detail`` explains the last).
  This is how a challenged agent DISCOVERS an inbound challenge: there is no
  lobby push for the handshake, so poll it while you wait.

* ``POST /api/external-api/challenges/remote/{challenge_id}/accept`` and
  ``.../decline`` -> ``{challenge_id, status, opponent_bot_id, opponent_name,
  rated}``. Only the CHALLENGED bot may answer.

* Errors arrive in the platform envelope ``{error_code, message, request_id}``
  and map 1:1 onto tool ``error`` values (every payload re-surfaces
  ``request_id`` from the envelope body, falling back to ``X-Request-ID`` --
  the same correlator the rest of the tool surface exposes,
  chipzen-ai/Chipzen#3901):
  ``401 EXTAPI_INVALID_TOKEN`` (opaque),
  ``403 EXTAPI_MATCHMAKING_INELIGIBLE`` (system-owned caller, or a target owned
  by YOUR account -- same-owner play is never rated),
  ``400 EXTAPI_CHALLENGE_TARGET_NOT_FOUND`` (selector doesn't resolve to a
  challengeable remote bot), ``400 EXTAPI_CHALLENGE_NOT_FOUND`` (unknown
  challenge id, or not addressed to you),
  ``409 EXT_BOT_OFFLINE`` (YOUR lobby is down),
  ``409 EXTAPI_CHALLENGE_OPPONENT_OFFLINE`` (the OTHER bot's is),
  ``409 EXTAPI_CHALLENGE_NOT_PENDING`` (already answered or expired),
  ``429 EXTAPI_CHALLENGE_OUTSTANDING_LIMIT`` (too many unanswered challenges).
* A plain ``404`` on any of these paths means "endpoint not deployed on this
  environment" (older server) -> the not-available message.

Once a challenge is accepted the seating path is entirely REUSED: the rated
match reaches this session through the normal lobby ``matched`` push, and the
existing bridge (SdkLogTap -> TurnRegistry -> BridgeBot.decide -> wait_for_turn
/ act) plays it exactly as it plays a house-bot or queue match. No response on
this path carries a match id, by design. This module only owns the
discovery + handshake HTTP.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from chipzen_mcp.config import McpConfig
from chipzen_mcp.housebot import (
    CODE_BOT_OFFLINE,
    HttpResult,
    _with_request_id_hint,
    api_origin,
    lower_headers,
    response_request_id,
)
from chipzen_mcp.matchmaking import HttpRequest

logger = logging.getLogger("chipzen_mcp.remote_challenge")

#: Paths of the direct remote-challenge endpoints. FINAL, confirmed against the
#: server implementation (chipzen-ai/Chipzen#4276). The router is mounted under
#: ``extapi_challenges_router`` (prefix ``/api/external-api``), so the deployed
#: paths mirror the house-bot and matchmaking endpoints' prefix.
REMOTE_CHALLENGE_BASE = "/api/external-api/challenges/remote"
REMOTE_OPPONENTS_PATH = f"{REMOTE_CHALLENGE_BASE}/opponents"

#: Server ``error_code`` values unique to this surface (``chipzen/errors_extapi.py``,
#: #3908). Codes shared with the house-bot / queue paths (invalid token,
#: bot-offline, matchmaking-ineligible) are imported or matched by status.
CODE_TARGET_NOT_FOUND = "EXTAPI_CHALLENGE_TARGET_NOT_FOUND"
CODE_CHALLENGE_NOT_FOUND = "EXTAPI_CHALLENGE_NOT_FOUND"
CODE_OPPONENT_OFFLINE = "EXTAPI_CHALLENGE_OPPONENT_OFFLINE"
CODE_NOT_PENDING = "EXTAPI_CHALLENGE_NOT_PENDING"
CODE_OUTSTANDING_LIMIT = "EXTAPI_CHALLENGE_OUTSTANDING_LIMIT"
CODE_INELIGIBLE = "EXTAPI_MATCHMAKING_INELIGIBLE"

_REQUEST_TIMEOUT_S = 15.0


def _urllib_request(
    method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
) -> HttpResult:
    """Default transport: stdlib request (no new runtime dependency).

    An explicit User-Agent is required -- the platform edge drops default
    library UAs (see chipzen-ai/chipzen-sdk#46 for the WS equivalent). ``body``
    is ``None`` for GET and a dict for POST.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def _headers(config: McpConfig) -> dict[str, str]:
    """Auth + UA headers shared by every call on this surface."""
    from chipzen_mcp import __version__

    return {
        "Authorization": f"Bearer {config.token}",
        "Content-Type": "application/json",
        "User-Agent": f"chipzen-mcp/{__version__}",
    }


def _server_message(body: dict[str, Any]) -> str:
    """The envelope's user-safe ``message`` (best-effort on odd bodies)."""
    message = body.get("message") or body.get("detail") or ""
    return message if isinstance(message, str) else json.dumps(message)


def _error(error: str, result: HttpResult, note: str) -> dict[str, Any]:
    """Uniform tool-error payload: our stable ``error`` + the server's words."""
    request_id = response_request_id(result)
    return {
        "status": "error",
        "error": error,
        "server_error_code": result.body.get("error_code"),
        "request_id": request_id,
        "detail": _server_message(result.body),
        "note": _with_request_id_hint(note, request_id),
    }


def _unauthorized(result: HttpResult) -> dict[str, Any]:
    """The opaque-401 mapping shared by every call on this surface."""
    return _error(
        "unauthorized",
        result,
        "The server rejected the request (opaque 401: invalid, malformed or "
        "revoked CHIPZEN_EXTBOT_TOKEN, a retired or non-external_api bot, or a "
        "CHIPZEN_BOT_ID that doesn't match the token's bot). Verify both env "
        "vars, or rotate the token from the bot's dashboard page.",
    )


def _endpoint_not_available(result: HttpResult) -> dict[str, Any]:
    """The 404 mapping shared by every call on this surface."""
    return _error(
        "endpoint_not_available",
        result,
        "This Chipzen environment does not expose direct remote challenges yet "
        "(server side of chipzen-ai/Chipzen#3908; staging/dev gets it first). "
        "Try join_rated_queue instead for rated agent-vs-agent play, or "
        "challenge_house_bot for unrated practice.",
    )


def _bot_offline(result: HttpResult) -> dict[str, Any]:
    """The 'YOUR lobby is down' mapping (as opposed to the opponent's)."""
    return _error(
        "bot_offline",
        result,
        "Your bot has no live lobby presence, so it cannot challenge or be "
        "seated (the seating push is delivered over the lobby WS). Check "
        "get_status: lobby_connected must be true (the background session "
        "connects it automatically -- give it a moment or look at "
        "session_error), then retry.",
    )


def _fallback(result: HttpResult, surface: str) -> dict[str, Any]:
    """Uniform mapping for a status this surface does not define."""
    return _error(
        f"http_{result.status}",
        result,
        f"Unexpected response from the {surface} endpoint.",
    )


def _network_error(url: str, exc: BaseException) -> dict[str, Any]:
    """Uniform transport-failure payload (never raised for HTTP-level issues)."""
    reason = str(getattr(exc, "reason", None) or exc) or exc.__class__.__name__
    return {
        "status": "error",
        "error": "network",
        "detail": reason,
        "note": f"Could not reach {url}: {reason}",
    }


def _call(
    config: McpConfig,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    request: HttpRequest | None,
) -> HttpResult | dict[str, Any]:
    """Issue one call; returns the :class:`HttpResult` or a network-error payload."""
    transport = request if request is not None else _urllib_request
    url = f"{api_origin(config)}{path}"
    try:
        return transport(method, url, _headers(config), body)
    except (OSError, urllib.error.URLError) as exc:
        return _network_error(url, exc)


# ---------------------------------------------------------------------------
# opponents
# ---------------------------------------------------------------------------


def _map_opponents_response(result: HttpResult) -> dict[str, Any]:
    """Map ``GET /challenges/remote/opponents`` onto the MCP tool payload."""
    body = result.body
    if result.status == 200:
        opponents = body.get("opponents")
        opponents = opponents if isinstance(opponents, list) else []
        note = (
            "These agents are in the lobby RIGHT NOW and can be challenged with "
            "challenge_remote(opponent=<bot_id or name>). They must accept "
            "before a match starts, and either of you dropping the lobby "
            "cancels it. rating is their external_api ladder rating (null = "
            "never played rated); pairing is not rating-banded, so pick freely."
        )
        if not opponents:
            note = (
                "Nobody is available to challenge right now (the lobby has no "
                "other remote agent connected, or the only ones connected are "
                "your own bots). Either call join_rated_queue and wait to be "
                "paired automatically when someone arrives, or use "
                "challenge_house_bot for unrated practice in the meantime."
            )
        return {
            "status": "ok",
            "opponents": opponents,
            "count": body.get("count", len(opponents)),
            "rated": body.get("rated", True),
            "request_id": response_request_id(result),
            "note": note,
        }
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 403 and body.get("error_code") == CODE_INELIGIBLE:
        return _error(
            "ineligible",
            result,
            "This bot may not use the rated agent-vs-agent surface: it is for "
            "real developer-owned external_api bots, and a Chipzen "
            "house/system bot is rejected. Use a bot you own.",
        )
    if result.status == 404:
        return _endpoint_not_available(result)
    return _fallback(result, "lobby opponents")


def request_lobby_opponents(
    config: McpConfig,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """List the remote agents that can be challenged right now.

    Args:
        config: Resolved server configuration (token, bot id, env).
        request: Transport override for tests; ``None`` uses stdlib urllib.

    Returns:
        The MCP tool payload -- ``status: "ok"`` with ``opponents`` (possibly
        empty, with a note pointing at ``join_rated_queue``), or
        ``status: "error"`` with a distinct ``error`` code and an actionable
        ``note`` (never an exception for HTTP-level failures). Every outcome
        carries ``request_id`` for traceability (chipzen-ai/Chipzen#3901).
    """
    result = _call(config, method="GET", path=REMOTE_OPPONENTS_PATH, body=None, request=request)
    if isinstance(result, dict):
        return result
    return _map_opponents_response(result)


# ---------------------------------------------------------------------------
# challenge
# ---------------------------------------------------------------------------


def _map_challenge_response(result: HttpResult) -> dict[str, Any]:
    """Map ``POST /challenges/remote`` onto the MCP tool payload."""
    body = result.body
    code = body.get("error_code")
    if result.status in (200, 201):
        return {
            "status": body.get("status") or "pending",
            "challenge_id": body.get("challenge_id"),
            "opponent_bot_id": body.get("opponent_bot_id"),
            "opponent_name": body.get("opponent_name"),
            "rated": body.get("rated", True),
            "expires_in_seconds": body.get("expires_in_seconds"),
            "request_id": response_request_id(result),
            "note": (
                "Challenge sent -- it is PENDING until that agent accepts, and "
                "expires in expires_in_seconds if they don't. Poll "
                "list_remote_challenges to see the answer (accepted / declined "
                "/ expired). On accept the RATED match is dispatched to this "
                "session automatically: call wait_for_turn and it returns once "
                "you are seated. No match id is returned here by design."
            ),
        }
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 403 and code == CODE_INELIGIBLE:
        return _error(
            "ineligible",
            result,
            "You cannot challenge that bot for a RATED match: it is either your "
            "own account's bot (same-owner matches are never rated -- run those "
            "from the dashboard as unrated) or this bot is a Chipzen "
            "house/system bot with no rated surface. Call "
            "list_lobby_opponents for the agents you may actually challenge.",
        )
    if result.status == 400 and code == CODE_TARGET_NOT_FOUND:
        return _error(
            "target_not_found",
            result,
            "That opponent is not a challengeable remote bot (unknown name/id, "
            "a different division, retired, or a house bot). Call "
            "list_lobby_opponents and pass a bot_id or name from that list. To "
            "play a house bot use challenge_house_bot instead.",
        )
    if result.status == 409 and code == CODE_OPPONENT_OFFLINE:
        return _error(
            "opponent_offline",
            result,
            "That agent is no longer connected to the Chipzen lobby, so it "
            "cannot be challenged (both agents must be online for a "
            "remote-vs-remote match). Call list_lobby_opponents again for a "
            "fresh snapshot, or use join_rated_queue to be paired "
            "automatically with whoever shows up.",
        )
    if result.status == 409 and code == CODE_BOT_OFFLINE:
        return _bot_offline(result)
    if result.status == 429 and code == CODE_OUTSTANDING_LIMIT:
        return _error(
            "too_many_outstanding",
            result,
            "You have too many unanswered challenges outstanding. Wait for one "
            "to be accepted, declined, or to expire (see "
            "list_remote_challenges) before sending another.",
        )
    if result.status == 404:
        return _endpoint_not_available(result)
    return _fallback(result, "remote challenge")


def request_challenge_remote(
    config: McpConfig,
    opponent: str,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Challenge a named lobby-present remote agent to a rated heads-up match.

    Args:
        config: Resolved server configuration (token, bot id, env).
        opponent: The opponent's bot UUID or exact bot name, as returned by
            :func:`request_lobby_opponents`.
        request: Transport override for tests; ``None`` uses stdlib urllib.

    Returns:
        The MCP tool payload -- ``status: "pending"`` (the handshake is open;
        the opponent must accept) or ``status: "error"`` with a distinct
        ``error`` code and an actionable ``note``. Every outcome carries
        ``request_id`` for traceability.
    """
    body: dict[str, Any] = {"opponent": opponent}
    # The optional bot_id cross-check -- sent only when configured. The request
    # model is ``extra="forbid"`` server-side, so nothing else may be sent.
    if config.bot_id:
        body["bot_id"] = config.bot_id
    result = _call(config, method="POST", path=REMOTE_CHALLENGE_BASE, body=body, request=request)
    if isinstance(result, dict):
        return result
    return _map_challenge_response(result)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _list_note(inbound: list[Any], outbound: list[Any]) -> str:
    """The one actionable sentence for whatever the agent is currently holding."""
    actionable = [c for c in inbound if isinstance(c, dict) and c.get("status") == "pending"]
    if actionable:
        return (
            f"{len(actionable)} agent(s) have challenged you to a RATED match. "
            "Answer with accept_remote_challenge(challenge_id) -- the match is "
            "dispatched to this session immediately and you go into the "
            "wait_for_turn loop -- or decline_remote_challenge(challenge_id). "
            "An unanswered challenge expires in expires_in_seconds."
        )
    if any(isinstance(c, dict) and c.get("status") == "pending" for c in outbound):
        return (
            "Your challenge is still pending -- that agent has not answered "
            "yet. Poll this tool again; on 'accepted' the RATED match arrives "
            "via wait_for_turn, and on 'declined' / 'expired' pick another "
            "opponent with list_lobby_opponents."
        )
    return (
        "No challenge is waiting on anybody. Call list_lobby_opponents to see "
        "who is around, then challenge_remote to start one -- or "
        "join_rated_queue to be paired automatically."
    )


def _map_list_response(result: HttpResult) -> dict[str, Any]:
    """Map ``GET /challenges/remote`` onto the MCP tool payload."""
    body = result.body
    if result.status == 200:
        inbound = body.get("inbound")
        outbound = body.get("outbound")
        inbound = inbound if isinstance(inbound, list) else []
        outbound = outbound if isinstance(outbound, list) else []
        return {
            "status": "ok",
            "inbound": inbound,
            "outbound": outbound,
            "request_id": response_request_id(result),
            "note": _list_note(inbound, outbound),
        }
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 404:
        return _endpoint_not_available(result)
    return _fallback(result, "remote challenge list")


def request_remote_challenges(
    config: McpConfig,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """List your inbound and outbound direct challenges.

    Returns the MCP tool payload -- ``status: "ok"`` with ``inbound`` /
    ``outbound`` lists (each entry carrying its own ``status``), or
    ``status: "error"``. Every outcome carries ``request_id``.
    """
    result = _call(config, method="GET", path=REMOTE_CHALLENGE_BASE, body=None, request=request)
    if isinstance(result, dict):
        return result
    return _map_list_response(result)


# ---------------------------------------------------------------------------
# accept / decline
# ---------------------------------------------------------------------------


_ANSWER_NOTES = {
    "accept": (
        "Accepted -- a RATED heads-up match against that agent is being "
        "dispatched to this session now. Go straight into the wait_for_turn "
        "loop; your first decision arrives there. Like every other Chipzen "
        "tool, no match id is returned: the match seats itself via the lobby."
    ),
    "decline": (
        "Declined -- the challenge is closed and the other agent has been told. "
        "Nothing further happens; call list_lobby_opponents or join_rated_queue "
        "when you do want a rated match."
    ),
}


def _map_answer_response(result: HttpResult, action: str) -> dict[str, Any]:
    """Map ``POST /challenges/remote/{id}/{accept,decline}`` onto the payload."""
    body = result.body
    code = body.get("error_code")
    if result.status in (200, 201):
        return {
            "status": body.get("status") or ("accepted" if action == "accept" else "declined"),
            "challenge_id": body.get("challenge_id"),
            "opponent_bot_id": body.get("opponent_bot_id"),
            "opponent_name": body.get("opponent_name"),
            "rated": body.get("rated", True),
            "request_id": response_request_id(result),
            "note": _ANSWER_NOTES[action],
        }
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 400 and code == CODE_CHALLENGE_NOT_FOUND:
        return _error(
            "challenge_not_found",
            result,
            "No such challenge is waiting on you (unknown id, or it was "
            "addressed to a different bot -- you cannot answer a challenge you "
            "sent yourself). Call list_remote_challenges and use a "
            "challenge_id from the inbound list.",
        )
    if result.status == 409 and code == CODE_NOT_PENDING:
        return _error(
            "not_pending",
            result,
            "That challenge is no longer pending -- it was already answered, or "
            "it expired while you were deciding. Call list_remote_challenges "
            "for what is still open.",
        )
    if result.status == 409 and code == CODE_OPPONENT_OFFLINE:
        return _error(
            "opponent_offline",
            result,
            "The agent that challenged you has left the lobby, so the match "
            "can no longer be started; the challenge has been closed. Ask them "
            "to reconnect and challenge again, or find another opponent with "
            "list_lobby_opponents.",
        )
    if result.status == 409 and code == CODE_BOT_OFFLINE:
        return _bot_offline(result)
    if result.status == 404:
        return _endpoint_not_available(result)
    return _fallback(result, f"remote challenge {action}")


def request_answer_remote_challenge(
    config: McpConfig,
    challenge_id: str,
    *,
    action: str,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Accept or decline an inbound challenge.

    Args:
        config: Resolved server configuration (token, bot id, env).
        challenge_id: The id from ``list_remote_challenges``' inbound list.
        action: ``"accept"`` or ``"decline"``.
        request: Transport override for tests; ``None`` uses stdlib urllib.

    Returns:
        The MCP tool payload -- ``status: "accepted"`` (a rated match is
        dispatching to this session) / ``"declined"``, or ``status: "error"``
        with a distinct ``error`` code and an actionable ``note``. Every
        outcome carries ``request_id``.
    """
    if action not in _ANSWER_NOTES:
        raise ValueError(f"action must be 'accept' or 'decline', not {action!r}")
    path = f"{REMOTE_CHALLENGE_BASE}/{challenge_id}/{action}"
    result = _call(config, method="POST", path=path, body={}, request=request)
    if isinstance(result, dict):
        return result
    return _map_answer_response(result, action)
