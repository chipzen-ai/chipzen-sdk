"""External-API bot reference client.

A minimal, runnable demonstration of the full External-API path:

    get a token (out-of-band)  ->  lobby WS  ->  "matched" notify
                                ->  per-match gateway WS  ->  play to match_end

This is a *reference*, not a published SDK. It speaks raw JSON over WebSockets
(via the ``websockets`` library, its only dependency) so the wire protocol is
fully visible. The protocol it implements is documented in
``docs/EXTERNAL-API-BOT-PROTOCOL.md`` (+ the two-layer specs it links).

The game decision is delegated to :func:`strategy.decide` (a pure function) so
the protocol plumbing here stays free of strategy logic.

Run it with ``run.py`` (see that module / the example README for usage). The
``play_match`` loop is importable on its own for an embedded driver.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit, urlunsplit

import websockets

from strategy import decide

logger = logging.getLogger("extapi_reference_bot")

# Protocol version this client speaks (Layer-1 negotiation). The server selects
# the highest mutually-supported version; advertising 1.0 matches the platform's
# current SUPPORTED_VERSIONS.
PROTOCOL_VERSIONS = ["1.0"]
CLIENT_NAME = "chipzen-extapi-reference"
CLIENT_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def lobby_ws_url(base_url: str, bot_id: str) -> str:
    """Build the lobby WS URL from a base origin + bot id.

    ``base_url`` is the platform origin, e.g. ``wss://staging.chipzen.ai`` or
    ``ws://localhost:8001``. Returns ``<base>/ws/external/bot/{bot_id}``.
    """
    return f"{_normalise_base(base_url)}/ws/external/bot/{bot_id}"


#: Sentinel subprotocol that marks the ``cz_extbot_`` token in the
#: ``Sec-WebSocket-Protocol`` header (CZ issue 2932). Must match the sentinel
#: value the platform's api gateway expects.
BOT_TOKEN_SUBPROTOCOL = "chipzen-bot-token"


def resolve_gateway_url(base_url: str, gateway_ws_path: str) -> str:
    """Resolve the ``matched.gateway_ws_url`` path against the base origin.

    The ``matched`` notification carries ``gateway_ws_url`` as a *path*
    (``/ws/external/match/{mid}/{pid}``). The client resolves it against the
    same origin the lobby used and returns the bare URL — the ``cz_extbot_``
    token is NO LONGER on the query string (CZ issue 2932: it leaked into
    proxy access logs). It now travels in the ``Sec-WebSocket-Protocol`` header (see
    :func:`bot_token_subprotocols`), per ``docs/EXTERNAL-API-BOT-PROTOCOL.md``
    §5 — the token authenticates the bot to the api gateway; it never reaches
    the executor.
    """
    base = _normalise_base(base_url)
    # gateway_ws_path is server-provided and already absolute-path-shaped; join
    # defensively in case a future server returns a full URL.
    if gateway_ws_path.startswith(("ws://", "wss://")):
        return gateway_ws_path
    return f"{base}{gateway_ws_path}"


def bot_token_subprotocols(token: str) -> list[str]:
    """Build the ``Sec-WebSocket-Protocol`` offer carrying the bot token.

    Returns ``[sentinel, token]`` — the sentinel marks "the next value is my
    bot token". The api gateway extracts the token from this header (so it
    never appears in any access log / URL) and echoes the sentinel back on
    accept. Pass to ``websockets.connect(..., subprotocols=...)``.
    """
    return [BOT_TOKEN_SUBPROTOCOL, token]


def _normalise_base(base_url: str) -> str:
    """Strip any path/query from a base origin, keeping scheme + netloc.

    Tolerates callers passing a trailing slash or an accidental path. Defaults
    a missing scheme to ``wss``.
    """
    parts = urlsplit(base_url)
    scheme = parts.scheme or "wss"
    netloc = parts.netloc or parts.path  # tolerate "host:port" without scheme
    return urlunsplit((scheme, netloc, "", "", "")).rstrip("/")


# ---------------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------------


async def wait_for_matched(base_url: str, bot_id: str, token: str) -> dict:
    """Connect the lobby, authenticate, and return the first ``matched`` notify.

    Implements the lobby half of the path (docs §3–§4):

    1. Open ``/ws/external/bot/{bot_id}``.
    2. Send ``{"type": "authenticate", "token": ...}`` as the first frame.
    3. Receive the server ``hello`` (lobby ``endpoint: "lobby"``); we do NOT
       send a client hello back on the lobby socket.
    4. Answer ``ping`` with ``pong`` (15s heartbeat) while waiting.
    5. Return the first ``matched`` payload received.

    Returns the ``matched`` dict (``match_id`` / ``participant_id`` /
    ``gateway_ws_url`` / ``rated``). Raises :class:`ConnectionError` if the
    lobby closes before a ``matched`` arrives.
    """
    url = lobby_ws_url(base_url, bot_id)
    logger.info("lobby: connecting to %s", url)
    async with websockets.connect(url, max_size=2**24) as ws:
        await ws.send(json.dumps({"type": "authenticate", "token": token}))

        async for raw in ws:
            msg = _loads(raw)
            mtype = msg.get("type")
            if mtype == "hello":
                logger.info("lobby: connected (endpoint=%s)", msg.get("endpoint"))
            elif mtype == "ping":
                # Heartbeat — must pong within 5s or the lobby closes us (4000).
                await ws.send(json.dumps({"type": "pong"}))
            elif mtype == "matched":
                logger.info(
                    "lobby: matched match=%s participant=%s rated=%s",
                    msg.get("match_id"),
                    msg.get("participant_id"),
                    msg.get("rated"),
                )
                return msg
            elif mtype == "evict":
                raise ConnectionError("lobby: evicted (replaced by a newer connection)")
            else:
                # Forward-compat: ignore unknown lobby frames.
                logger.debug("lobby: ignoring frame type=%s", mtype)

    raise ConnectionError(
        "lobby: connection closed before a 'matched' notification arrived"
    )


# ---------------------------------------------------------------------------
# Match data plane
# ---------------------------------------------------------------------------


async def play_match(gateway_url: str, match_id: str, token: str) -> dict | None:
    """Run one match end-to-end over the per-match gateway WS (docs §5–§6).

    Opens the gateway WS — the ``cz_extbot_`` token authenticates via the
    ``Sec-WebSocket-Protocol`` header, NOT the query string — runs the
    two-layer bot handshake, then drives the game loop until ``match_end``. The
    game decision is delegated to :func:`strategy.decide`.

    Returns the ``match_end`` payload (with ``reason`` + ``results``), or
    ``None`` if the connection closed without a clean ``match_end``.
    """
    logger.info("match: connecting (match=%s)", match_id)
    async with websockets.connect(
        gateway_url,
        max_size=2**24,
        subprotocols=bot_token_subprotocols(token),  # type: ignore[arg-type]
    ) as ws:
        # ---- Layer-1 handshake (docs §6.1) ----
        # The executor ignores this token (the gateway's internal JWT is
        # authoritative), but the authenticate frame MUST be sent first or the
        # handshake stalls. We omit a real token here for clarity; an empty
        # token is accepted on this leg.
        await ws.send(
            json.dumps({"type": "authenticate", "match_id": match_id, "token": ""})
        )

        server_hello = _loads(await ws.recv())
        if server_hello.get("type") != "hello":
            logger.error(
                "match: expected server hello, got %r", server_hello.get("type")
            )
            return None
        logger.info(
            "match: server hello version=%s game_type=%s",
            server_hello.get("selected_version"),
            server_hello.get("game_type"),
        )
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "match_id": match_id,
                    "supported_versions": PROTOCOL_VERSIONS,
                    "client_name": CLIENT_NAME,
                    "client_version": CLIENT_VERSION,
                }
            )
        )

        # ---- Game loop (docs §6.2) ----
        async for raw in ws:
            msg = _loads(raw)
            result = await handle_match_message(ws, match_id, msg)
            if result is not None:
                return result

    logger.info("match: connection closed without a match_end")
    return None


async def handle_match_message(ws, match_id: str, msg: dict) -> dict | None:
    """Handle one server→bot match frame.

    Returns the ``match_end`` payload when the match ends (signalling the
    caller's loop to stop), otherwise ``None``. ``ws`` is anything with an
    awaitable ``send`` taking a string (a ``websockets`` connection in
    production; a fake in tests).
    """
    mtype = msg.get("type")

    if mtype == "ping":
        await ws.send(json.dumps({"type": "pong", "match_id": match_id}))
        return None

    if mtype == "turn_request":
        action = decide(msg.get("state", {}) or {}, msg.get("valid_actions", []) or [])
        await ws.send(
            json.dumps(
                {
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": msg.get("request_id"),  # MUST echo
                    "action": action["action"],
                    "params": action.get("params", {}),
                }
            )
        )
        return None

    if mtype == "action_rejected":
        # Retry with the SAME request_id, using a guaranteed-legal fallback from
        # the rejection's valid_actions (or check/fold per the server's
        # auto-action guarantee when the field is absent).
        valid = msg.get("valid_actions") or ["check", "fold"]
        fallback = "check" if "check" in valid else "fold"
        logger.info(
            "match: action rejected (%s); retrying with %s", msg.get("reason"), fallback
        )
        await ws.send(
            json.dumps(
                {
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": msg.get("request_id"),
                    "action": fallback,
                    "params": {},
                }
            )
        )
        return None

    if mtype == "match_end":
        logger.info("match: ended reason=%s", msg.get("reason"))
        return msg

    if mtype == "error":
        logger.warning("match: error [%s] %s", msg.get("code"), msg.get("message"))
        return None

    # match_start / round_start / turn_result / phase_change / round_result /
    # action_timeout / session_control / session_token / reconnected and any
    # unknown future type: nothing to do for a trivial bot. Forward-compat
    # requires silently ignoring unknown types.
    logger.debug("match: observed frame type=%s", mtype)
    return None


# ---------------------------------------------------------------------------
# End-to-end driver
# ---------------------------------------------------------------------------


async def run_once(base_url: str, bot_id: str, token: str) -> dict | None:
    """Drive one full path: lobby -> matched -> play one match to match_end.

    Returns the ``match_end`` payload (or ``None`` if the match WS closed
    without one). Composes :func:`wait_for_matched` + :func:`play_match`.
    """
    matched = await wait_for_matched(base_url, bot_id, token)
    gateway_url = resolve_gateway_url(base_url, matched["gateway_ws_url"])
    return await play_match(gateway_url, matched["match_id"], token)


def _loads(raw: str | bytes) -> dict:
    """Parse a WS text/bytes frame into a dict (``{}`` on non-dict / bad JSON)."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return msg if isinstance(msg, dict) else {}
