"""HTTP client for the rated matchmaking queue (agent-vs-agent).

Backs the ``join_rated_queue`` / ``rated_queue_status`` / ``leave_rated_queue``
MCP tools: the extbot-token surface that opts a ``cz_extbot_`` bot into a
**rated** heads-up match against ANOTHER remote agent (chipzen-ai/Chipzen#3907).
Unlike ``challenge_house_bot`` (unrated, always pairs synchronously against a
house bot), pairing here is peer-to-peer and may not be immediate: a solo
waiter sits in a FIFO queue until an eligible partner joins or the queue TTL
elapses.

Contract status: FINAL -- aligned to the server implementation
(chipzen-ai/Chipzen#3910, ``api/routes/extapi_matchmaking.py``, mounted under
``extapi_challenges_router`` so the deployed paths carry the ``/api/external-api``
prefix). The whole contract stays isolated in this module:

* ``POST /api/external-api/matchmaking/join`` with
  ``Authorization: Bearer cz_extbot_<token>``. The token IS the bot identity;
  the body's optional ``bot_id`` is a pure cross-check (sent only when
  configured; a mismatch comes back as the opaque 401). The request model
  forbids extra fields.

  ``200`` returns ``{status, position, queue_ttl_seconds, rated}``:

  - ``status == "queued"``: waiting; ``position`` is the 1-based FIFO rank.
    The seating signal arrives later on the lobby ``matched`` push -- the
    agent waits via ``wait_for_turn`` (or polls ``status`` for ``timed_out``).
  - ``status == "matched"``: an eligible partner was found and a RATED match
    is dispatching. Like ``challenge_house_bot``, this response carries NO
    match id by design: the match reaches this session through the normal
    lobby dispatch, and the agent never dials sockets. ``rated`` is always
    ``true`` for this queue.

* ``POST /api/external-api/matchmaking/leave`` -> ``{status}`` where status is
  ``"left"`` (removed) or ``"not_queued"`` (you weren't in it -- idempotent).

* ``GET /api/external-api/matchmaking/status`` -> ``{status, position,
  waiting_seconds, queue_ttl_seconds}`` where status is ``"queued"`` (still
  waiting), ``"idle"`` (not in the queue), or ``"timed_out"`` (your wait
  exceeded the TTL and the entry has now been dropped -- re-join to retry).
  ``timed_out`` is one-shot: the first read after the TTL returns it and
  evicts the entry, then ``idle`` until you re-join.

* Errors arrive in the platform envelope ``{error_code, message, request_id}``
  and map 1:1 onto tool ``error`` values (the tool re-surfaces ``request_id``
  from the envelope body, falling back to ``X-Request-ID``, on both error and
  success payloads -- the same correlator ``challenge_house_bot`` exposes, kept
  consistent across the whole tool surface, chipzen-ai/Chipzen#3901). ``join``
  can emit:
  ``401 EXTAPI_INVALID_TOKEN`` (opaque -- missing / malformed / unknown /
  revoked token, retired bot, non-``external_api`` bot, or ``bot_id``
  mismatch), ``403 EXTAPI_MATCHMAKING_INELIGIBLE`` (a system/house-owned bot
  may not enter the rated ladder), ``409 EXT_BOT_OFFLINE`` (no live lobby
  presence). The per-token concurrent-cap / free-tier gates fire inside the
  dispatch when a pair actually forms (a solo waiter consumes no slot), so
  ``join`` itself does not surface them. ``leave`` / ``status`` share the 401.
* A plain ``404`` on any of these paths means "endpoint not deployed on this
  environment" (older server) -> the not-available message.

The seating path is entirely REUSED: once a rated match is dispatched, the SDK
session sees the lobby ``matched`` push, and the existing bridge (SdkLogTap ->
TurnRegistry -> BridgeBot.decide -> wait_for_turn / act) plays it exactly as it
plays a house-bot match. This module only owns the queue-initiation HTTP.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
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

logger = logging.getLogger("chipzen_mcp.matchmaking")

#: Paths of the rated-matchmaking endpoints. FINAL, confirmed against the
#: server implementation (chipzen-ai/Chipzen#3910). The matchmaking router is
#: mounted under ``extapi_challenges_router`` (prefix ``/api/external-api``),
#: so the deployed paths mirror the house-bot endpoint's prefix.
MATCHMAKING_JOIN_PATH = "/api/external-api/matchmaking/join"
MATCHMAKING_LEAVE_PATH = "/api/external-api/matchmaking/leave"
MATCHMAKING_STATUS_PATH = "/api/external-api/matchmaking/status"

#: Server ``error_code`` unique to the rated queue (``chipzen/errors.py``,
#: #3907). The other codes this surface can emit (invalid-token, bot-offline)
#: are shared with the house-bot path and imported from :mod:`chipzen_mcp.housebot`.
CODE_MATCHMAKING_INELIGIBLE = "EXTAPI_MATCHMAKING_INELIGIBLE"

_REQUEST_TIMEOUT_S = 15.0

#: Transport signature: ``(method, url, headers, json_body | None) -> HttpResult``.
#: Method-aware (the queue mixes POST join/leave with a GET status), and
#: injectable so tests exercise the full mapping without a socket.
HttpRequest = Callable[[str, str, dict[str, str], dict[str, Any] | None], HttpResult]


def _urllib_request(
    method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
) -> HttpResult:
    """Default transport: stdlib request (no new runtime dependency).

    An explicit User-Agent is required -- the platform edge drops default
    library UAs (see chipzen-ai/chipzen-sdk#46 for the WS equivalent). ``body``
    is ``None`` for GET (``status``) and a dict for POST (``join`` / ``leave``).
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
    """Auth + UA headers shared by all three matchmaking calls."""
    from chipzen_mcp import __version__

    return {
        "Authorization": f"Bearer {config.token}",
        "Content-Type": "application/json",
        "User-Agent": f"chipzen-mcp/{__version__}",
    }


def _join_body(config: McpConfig) -> dict[str, Any]:
    """The optional ``bot_id`` cross-check -- sent only when configured.

    The request model is ``extra="forbid"`` server-side, so nothing else may
    ever be sent; a lobby-URL-only setup (no ``bot_id``) sends an empty body.
    """
    return {"bot_id": config.bot_id} if config.bot_id else {}


def _server_message(body: dict[str, Any]) -> str:
    """The envelope's user-safe ``message`` (best-effort on odd bodies)."""
    message = body.get("message") or body.get("detail") or ""
    return message if isinstance(message, str) else json.dumps(message)


def _error(error: str, result: HttpResult, note: str) -> dict[str, Any]:
    """Uniform tool-error payload: our stable ``error`` + the server's words.

    ``request_id`` is surfaced from the error envelope (or the ``X-Request-ID``
    header) so a failing call is traceable in the platform logs without
    guessing, and the ``note`` tells the developer to quote it when reporting
    the issue -- the same treatment ``challenge_house_bot`` gives, kept
    consistent across the whole tool surface (chipzen-ai/Chipzen#3901).
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


def _unauthorized(result: HttpResult) -> dict[str, Any]:
    """The opaque-401 mapping shared by join / leave / status."""
    return _error(
        "unauthorized",
        result,
        "The server rejected the request (opaque 401: invalid, malformed or "
        "revoked CHIPZEN_EXTBOT_TOKEN, a retired or non-external_api bot, or a "
        "CHIPZEN_BOT_ID that doesn't match the token's bot). Verify both env "
        "vars, or rotate the token from the bot's dashboard page.",
    )


def _endpoint_not_available(result: HttpResult) -> dict[str, Any]:
    """The 404 mapping shared by join / leave / status."""
    return _error(
        "endpoint_not_available",
        result,
        "This Chipzen environment does not expose rated matchmaking yet "
        "(server side of chipzen-ai/Chipzen#3907; staging/dev gets it first). "
        "There is no self-serve fallback for rated agent-vs-agent play on this "
        "environment -- keep this session connected and either challenge a "
        "house bot with challenge_house_bot (unrated practice) or start a rated "
        "match from the dashboard.",
    )


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


def _map_join_response(result: HttpResult) -> dict[str, Any]:
    """Map ``POST /matchmaking/join`` onto the MCP tool payload."""
    body = result.body
    code = body.get("error_code")
    if result.status in (200, 201):
        queue_status = body.get("status")
        rated = body.get("rated", True)
        queue_ttl = body.get("queue_ttl_seconds")
        # The success body carries no request_id (it's set on the route's
        # response_model, not the envelope); the id lives only in the
        # X-Request-ID response header -- surface it so the call, and for a
        # "matched" pairing the dispatched match, are traceable in the platform
        # logs (chipzen-ai/Chipzen#3901).
        request_id = response_request_id(result)
        if queue_status == "matched":
            note = (
                "An eligible opponent was found and a RATED heads-up match "
                "is being dispatched to this session. Go straight into the "
                "wait_for_turn loop -- the match seats itself via the lobby "
                "and your first decision arrives there. Like "
                "challenge_house_bot, no match id is returned here."
            )
            if request_id:
                note = (
                    f"{note} This match's request_id={request_id} threads the "
                    "whole dispatch in the platform logs -- quote it if you "
                    "need support to trace this match."
                )
            return {
                "status": "matched",
                "rated": rated,
                "queue_ttl_seconds": queue_ttl,
                "request_id": request_id,
                "note": note,
            }
        # Anything that isn't an explicit "matched" is a still-waiting queue
        # entry; echo the server's status verbatim so an unexpected value is
        # never silently relabelled.
        return {
            "status": queue_status or "queued",
            "rated": rated,
            "position": body.get("position"),
            "queue_ttl_seconds": queue_ttl,
            "request_id": request_id,
            "note": (
                "You are waiting in the rated queue. When an eligible partner "
                "joins, a RATED match is dispatched to this session "
                "automatically -- call wait_for_turn and it returns once you "
                "are seated. If it stays idle, poll rated_queue_status: "
                "'timed_out' means no partner arrived within queue_ttl_seconds "
                "(just call join_rated_queue again). Call leave_rated_queue to "
                "cancel."
            ),
        }
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 403 and code == CODE_MATCHMAKING_INELIGIBLE:
        return _error(
            "ineligible",
            result,
            "This bot may not enter the rated matchmaking queue: the queue is "
            "for real developer-owned external_api bots, and a Chipzen "
            "house/system bot is rejected. Use a bot you own.",
        )
    if result.status == 409 and code == CODE_BOT_OFFLINE:
        return _error(
            "bot_offline",
            result,
            "Your bot has no live lobby presence, so it cannot be queued (the "
            "seating push is delivered over the lobby WS). Check get_status: "
            "lobby_connected must be true (the background session connects it "
            "automatically -- give it a moment or look at session_error), then "
            "retry.",
        )
    if result.status == 404:
        return _endpoint_not_available(result)
    return _error(
        f"http_{result.status}",
        result,
        "Unexpected response from the matchmaking join endpoint.",
    )


def request_join_rated_queue(
    config: McpConfig,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Opt the configured bot into the rated matchmaking queue.

    Args:
        config: Resolved server configuration (token, bot id, env).
        request: Transport override for tests; ``None`` uses stdlib urllib.

    Returns:
        The MCP tool payload -- ``status: "matched"`` (partner found, rated
        match dispatching), ``status: "queued"`` (waiting, with ``position``),
        or ``status: "error"`` with a distinct ``error`` code and an actionable
        ``note`` (never an exception for HTTP-level failures). Every outcome
        carries ``request_id`` (``None`` only when the platform returned neither
        an envelope id nor an ``X-Request-ID`` header) so the call is traceable
        in the platform logs (chipzen-ai/Chipzen#3901).
    """
    transport = request if request is not None else _urllib_request
    url = f"{api_origin(config)}{MATCHMAKING_JOIN_PATH}"
    try:
        result = transport("POST", url, _headers(config), _join_body(config))
    except (OSError, urllib.error.URLError) as exc:
        return _network_error(url, exc)
    return _map_join_response(result)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _map_status_response(result: HttpResult) -> dict[str, Any]:
    """Map ``GET /matchmaking/status`` onto the MCP tool payload."""
    body = result.body
    if result.status == 200:
        queue_status = body.get("status")
        common = {
            "position": body.get("position"),
            "waiting_seconds": body.get("waiting_seconds"),
            "queue_ttl_seconds": body.get("queue_ttl_seconds"),
            "request_id": response_request_id(result),
        }
        if queue_status == "queued":
            note = (
                "Still waiting in the rated queue. The match is dispatched to "
                "this session when a partner joins -- call wait_for_turn. Call "
                "leave_rated_queue to cancel."
            )
        elif queue_status == "timed_out":
            note = (
                "No partner was found within queue_ttl_seconds and your queue "
                "entry has now been dropped (this reading is one-shot). Call "
                "join_rated_queue again to re-enter."
            )
        elif queue_status == "idle":
            note = (
                "You are not in the rated queue (never joined, already matched, "
                "or already left). Call join_rated_queue to enter."
            )
        else:
            note = "Unexpected status from the matchmaking status endpoint."
        return {"status": queue_status or "idle", **common, "note": note}
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 404:
        return _endpoint_not_available(result)
    return _error(
        f"http_{result.status}",
        result,
        "Unexpected response from the matchmaking status endpoint.",
    )


def request_rated_queue_status(
    config: McpConfig,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Poll the configured bot's rated-queue state.

    Returns the MCP tool payload -- ``status`` is ``"queued"`` / ``"idle"`` /
    ``"timed_out"`` (see module docstring), or ``status: "error"`` on failure.
    Every outcome carries ``request_id`` (the platform correlator, ``None`` when
    absent) for traceability. ``timed_out`` is one-shot server-side: reading it
    evicts the queue entry.
    """
    transport = request if request is not None else _urllib_request
    url = f"{api_origin(config)}{MATCHMAKING_STATUS_PATH}"
    try:
        result = transport("GET", url, _headers(config), None)
    except (OSError, urllib.error.URLError) as exc:
        return _network_error(url, exc)
    return _map_status_response(result)


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------


def _map_leave_response(result: HttpResult) -> dict[str, Any]:
    """Map ``POST /matchmaking/leave`` onto the MCP tool payload."""
    body = result.body
    if result.status in (200, 201):
        queue_status = body.get("status")
        if queue_status == "not_queued":
            note = "You were not in the rated queue; nothing to cancel (idempotent)."
        else:
            note = "Removed from the rated queue."
        return {
            "status": queue_status or "left",
            "request_id": response_request_id(result),
            "note": note,
        }
    if result.status == 401:
        return _unauthorized(result)
    if result.status == 404:
        return _endpoint_not_available(result)
    return _error(
        f"http_{result.status}",
        result,
        "Unexpected response from the matchmaking leave endpoint.",
    )


def request_leave_rated_queue(
    config: McpConfig,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Cancel: remove the configured bot from the rated queue (idempotent).

    Returns the MCP tool payload -- ``status: "left"`` (removed) /
    ``status: "not_queued"`` (was not waiting), or ``status: "error"``. Every
    outcome carries ``request_id`` (the platform correlator, ``None`` when
    absent) for traceability (chipzen-ai/Chipzen#3901).
    """
    transport = request if request is not None else _urllib_request
    url = f"{api_origin(config)}{MATCHMAKING_LEAVE_PATH}"
    try:
        result = transport("POST", url, _headers(config), _join_body(config))
    except (OSError, urllib.error.URLError) as exc:
        return _network_error(url, exc)
    return _map_leave_response(result)


def _network_error(url: str, exc: BaseException) -> dict[str, Any]:
    """Uniform transport-failure payload (never raised for HTTP-level issues)."""
    reason = str(getattr(exc, "reason", None) or exc) or exc.__class__.__name__
    return {
        "status": "error",
        "error": "network",
        "detail": reason,
        "note": f"Could not reach {url}: {reason}",
    }
