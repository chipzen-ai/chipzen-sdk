"""WebSocket client for connecting a :class:`ChipzenBot` to a Chipzen server.

Implements the Chipzen two-layer protocol:

- Layer 1 (Transport): ``docs/protocol/TRANSPORT-PROTOCOL.md``
- Layer 2 (Poker):     ``docs/protocol/POKER-GAME-STATE-PROTOCOL.md``

Handles the connection lifecycle: ``authenticate``, ``hello`` handshake,
``match_start``, per-round ``round_start`` / ``turn_request`` /
``turn_result`` / ``phase_change`` / ``round_result`` dispatch, heartbeat
``ping``/``pong``, and safe handling of ``action_rejected`` retries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from chipzen.bot import ChipzenBot
from chipzen.models import Action, GameState

logger = logging.getLogger("chipzen")

# Protocol versions this client implements. Sent in the ``authenticate`` /
# client ``hello`` so the server can negotiate a mutually supported version.
SUPPORTED_PROTOCOL_VERSIONS = ["1.0"]


def _extract_match_id(url: str) -> str:
    """Extract a match UUID from a ``.../ws/match/{match_id}/...`` URL."""
    parts = url.rstrip("/").split("/")
    for i, part in enumerate(parts):
        if part == "match" and i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


def _safe_fallback_action(valid_actions: list[str]) -> Action:
    """Return a safe fallback for an ``action_rejected`` retry.

    Prefers ``check`` when legal, otherwise ``fold``. Mirrors the server's
    auto-action policy (see TRANSPORT-PROTOCOL section 8.12).
    """
    if "check" in valid_actions:
        return Action.check()
    if "fold" in valid_actions:
        return Action.fold()
    # Last resort: echo the first valid action the server offered.
    if valid_actions:
        return Action(action=valid_actions[0])
    return Action.fold()


async def _send_json(ws: Any, message: dict) -> None:
    await ws.send(json.dumps(message))


async def run_bot(
    url: str,
    bot: ChipzenBot,
    *,
    max_retries: int = 3,
    token: str | None = None,
    ticket: str | None = None,
    match_id: str | None = None,
    client_name: str = "chipzen-sdk",
    client_version: str = "0.2.0",
) -> None:
    """Connect a bot to the Chipzen server and play until the match ends.

    Args:
        url: WebSocket URL, e.g.
             ``ws://localhost:8001/ws/match/{match_id}/{participant_id}``
             or ``.../ws/match/{match_id}/bot`` for internal bots.
        bot: Your bot instance.
        max_retries: Number of reconnection attempts on unexpected disconnect.
        token: Bot API token (for the ``/bot`` endpoint).
        ticket: Single-use ticket (for competitive endpoints).
        match_id: Match UUID. Extracted from the URL if not provided.
        client_name: Client software name sent in the ``hello`` handshake.
        client_version: Client software version sent in the ``hello`` handshake.
    """
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        try:
            from websockets import connect  # type: ignore[assignment]
        except ImportError as exc:
            raise ImportError(
                "The 'websockets' package is required. Install it with:\n  pip install websockets"
            ) from exc

    if match_id is None:
        match_id = _extract_match_id(url)

    retries = 0
    while retries <= max_retries:
        try:
            async with connect(url) as ws:
                retries = 0  # reset on successful connect
                await _run_session(
                    ws,
                    bot,
                    match_id=match_id,
                    token=token,
                    ticket=ticket,
                    client_name=client_name,
                    client_version=client_version,
                )
                # _run_session returns cleanly on match_end.
                return

        except asyncio.CancelledError:
            raise
        except Exception:
            retries += 1
            if retries > max_retries:
                logger.exception("Max reconnection attempts reached, giving up")
                raise
            wait = min(2**retries, 8)
            logger.warning(
                "Connection lost, retrying in %ds (attempt %d/%d)",
                wait,
                retries,
                max_retries,
            )
            await asyncio.sleep(wait)


async def _run_session(
    ws: Any,
    bot: ChipzenBot,
    *,
    match_id: str,
    token: str | None,
    ticket: str | None,
    client_name: str,
    client_version: str,
) -> None:
    """Execute a single connected session: handshake + message loop."""
    # --- Layer 1 handshake --------------------------------------------
    auth_msg: dict[str, Any] = {
        "type": "authenticate",
        "match_id": match_id,
    }
    if token is not None:
        auth_msg["token"] = token
    elif ticket is not None:
        auth_msg["ticket"] = ticket
    else:
        # Sidecar / localhost dev may accept an empty token. Production
        # endpoints require one of {token, ticket}.
        auth_msg["token"] = ""
    await _send_json(ws, auth_msg)

    raw_hello = await ws.recv()
    server_hello = json.loads(raw_hello)
    if server_hello.get("type") != "hello":
        logger.error(
            "Expected 'hello' from server, got %r",
            server_hello.get("type"),
        )
        return

    selected_version = server_hello.get("selected_version")
    server_versions = server_hello.get("supported_versions", []) or []
    if selected_version and selected_version not in SUPPORTED_PROTOCOL_VERSIONS:
        logger.error(
            "Server selected unsupported protocol version %r (client supports %s)",
            selected_version,
            SUPPORTED_PROTOCOL_VERSIONS,
        )
        return
    if not selected_version and server_versions:
        if not any(v in SUPPORTED_PROTOCOL_VERSIONS for v in server_versions):
            logger.error(
                "No mutually supported protocol version (server=%s, client=%s)",
                server_versions,
                SUPPORTED_PROTOCOL_VERSIONS,
            )
            return

    await _send_json(
        ws,
        {
            "type": "hello",
            "match_id": match_id,
            "supported_versions": SUPPORTED_PROTOCOL_VERSIONS,
            "client_name": client_name,
            "client_version": client_version,
        },
    )
    logger.info(
        "Handshake complete: version=%s game_type=%s",
        selected_version or "?",
        server_hello.get("game_type", "?"),
    )

    # --- Session state tracked across messages ------------------------
    your_seat: int = 0
    dealer_seat: int = 0
    current_round_id: str = ""
    last_seq: int | None = None

    # --- Main message loop --------------------------------------------
    async for raw in ws:
        try:
            payload: dict[str, Any] = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Received non-JSON frame, ignoring")
            continue

        msg_type = payload.get("type")
        seq = payload.get("seq")
        if isinstance(seq, int):
            if last_seq is not None and seq != last_seq + 1:
                logger.warning(
                    "Sequence gap detected: expected %d, got %d",
                    last_seq + 1,
                    seq,
                )
            last_seq = seq

        if msg_type == "ping":
            # Heartbeat: server expects a ``pong`` within 5000ms.
            await _send_json(ws, {"type": "pong", "match_id": match_id})

        elif msg_type == "session_token":
            # Informational -- SDK does not currently use the session token.
            logger.debug("Received session_token")

        elif msg_type == "match_start":
            # Determine this bot's seat from the seats array.
            for seat_info in payload.get("seats", []) or []:
                if seat_info.get("is_self"):
                    your_seat = int(seat_info.get("seat", 0))
                    break
            bot.on_match_start(payload)

        elif msg_type == "round_start":
            state = payload.get("state", {}) or {}
            dealer_seat = int(state.get("dealer_seat", dealer_seat))
            current_round_id = str(payload.get("round_id", current_round_id))
            bot.on_round_start(payload)

        elif msg_type == "turn_request":
            # ``turn_request`` has no round_id of its own; inject the one we
            # learned from the most recent ``round_start`` so the bot can
            # correlate turns to rounds.
            if "round_id" not in payload and current_round_id:
                payload = {**payload, "round_id": current_round_id}
            state = GameState.from_turn_request(
                payload,
                your_seat=your_seat,
                dealer_seat=dealer_seat,
            )
            try:
                action = bot.decide(state)
            except Exception:
                logger.exception("Bot.decide() raised an exception, folding")
                action = Action.fold()

            await _send_json(
                ws,
                {
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": payload.get("request_id"),  # MUST echo
                    **action.to_wire(),
                },
            )

        elif msg_type == "action_rejected":
            # Retry within ``remaining_ms`` using the SAME request_id.
            reason = payload.get("reason")
            message = payload.get("message", "")
            remaining = payload.get("remaining_ms", 0)
            logger.warning(
                "Action rejected (%s): %s -- %dms remaining, retrying with safe fallback",
                reason,
                message,
                remaining,
            )
            # For the safe fallback we need to know what's still legal; the
            # rejection message does not include valid_actions, so we fall
            # back to check-or-fold which is always safe per the server's
            # auto-action policy.
            safe = _safe_fallback_action(["check", "fold"])
            await _send_json(
                ws,
                {
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": payload.get("request_id"),
                    **safe.to_wire(),
                },
            )

        elif msg_type == "turn_result":
            bot.on_turn_result(payload)

        elif msg_type == "phase_change":
            bot.on_phase_change(payload)

        elif msg_type == "round_result":
            bot.on_round_result(payload)

        elif msg_type == "action_timeout":
            logger.info(
                "Server auto-applied %s after timeout",
                payload.get("auto_action", "?"),
            )

        elif msg_type == "session_control":
            logger.info(
                "Session control: %s (%s)",
                payload.get("action"),
                payload.get("reason"),
            )

        elif msg_type == "error":
            logger.error(
                "Server error [%s]: %s",
                payload.get("code"),
                payload.get("message"),
            )

        elif msg_type == "reconnected":
            # We are mid-session after a reconnect; resume.
            logger.info("Reconnected at round %s", payload.get("round_number"))
            pending = payload.get("pending_request")
            if pending:
                # Treat the pending request exactly like a ``turn_request``.
                state = GameState.from_turn_request(
                    pending,
                    your_seat=your_seat,
                    dealer_seat=dealer_seat,
                )
                try:
                    action = bot.decide(state)
                except Exception:
                    logger.exception("Bot.decide() raised an exception, folding")
                    action = Action.fold()
                await _send_json(
                    ws,
                    {
                        "type": "turn_action",
                        "match_id": match_id,
                        "request_id": pending.get("request_id"),
                        **action.to_wire(),
                    },
                )

        elif msg_type == "match_end":
            bot.on_match_end(payload)
            return

        else:
            # Forward compatibility: silently ignore unknown message types.
            logger.debug("Ignoring unknown message type %r", msg_type)


def _import_bot(specifier: str) -> ChipzenBot:
    """Import a bot from a ``module:ClassName`` specifier.

    Example: ``my_bot:MyBot`` imports ``MyBot`` from ``my_bot.py``.
    """
    if ":" not in specifier:
        raise ValueError(f"Bot specifier must be 'module:ClassName', got {specifier!r}")
    module_path, class_name = specifier.rsplit(":", 1)

    import importlib

    # Add cwd to sys.path so local modules resolve
    if "" not in sys.path:
        sys.path.insert(0, "")

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    instance = cls()
    if not isinstance(instance, ChipzenBot):
        raise TypeError(
            f"{class_name} must be a subclass of ChipzenBot, got {type(instance).__name__}"
        )
    return instance


def connect_cli(args: list[str] | None = None) -> None:
    """CLI entry point: connect a bot to a server.

    Usage::

        python -m chipzen connect --url ws://... --bot my_bot:MyBot
    """
    import argparse

    parser = argparse.ArgumentParser(description="Connect a Chipzen poker bot to a server")
    parser.add_argument(
        "--url",
        required=True,
        help="WebSocket URL (ws://host/ws/match/{match_id}/{participant_id})",
    )
    parser.add_argument(
        "--bot",
        required=True,
        help="Bot specifier as module:ClassName (e.g. my_bot:MyBot)",
    )
    parser.add_argument(
        "--token",
        help="Bot API token for authentication",
    )
    parser.add_argument(
        "--ticket",
        help="Single-use ticket for authentication",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Max reconnection attempts (default: 3)",
    )

    parsed = parser.parse_args(args)
    bot = _import_bot(parsed.bot)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger.info("Connecting to %s", parsed.url)

    asyncio.run(
        run_bot(
            parsed.url,
            bot,
            max_retries=parsed.retries,
            token=parsed.token,
            ticket=parsed.ticket,
        )
    )
