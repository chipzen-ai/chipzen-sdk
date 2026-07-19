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
import time
from typing import Any

from chipzen import __version__ as _VERSION  # noqa: N812
from chipzen.bot import ChipzenBot
from chipzen.models import Action, GameState
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

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


class BotDecisionError(Exception):
    """Raised when ``bot.decide()`` (or a lifecycle hook) errors and
    ``safe_mode=False``.

    Distinguished from transport/connection errors so the caller treats it as
    terminal (a deterministic bot bug, not a transient disconnect) and does NOT
    reconnect-retry it. See chipzen-ai/chipzen-sdk#52 (decide) and #80
    (lifecycle hooks).
    """


async def run_bot(
    url: str,
    bot: ChipzenBot,
    *,
    max_retries: int | None = None,
    retry_policy: RetryPolicy | None = None,
    token: str | None = None,
    ticket: str | None = None,
    match_id: str | None = None,
    client_name: str = "chipzen-sdk",
    client_version: str = _VERSION,
    safe_mode: bool = True,
    user_agent: str | None = None,
) -> dict | None:
    """Connect a bot to the Chipzen server and play until the match ends.

    Args:
        url: WebSocket URL, e.g.
             ``ws://localhost:8001/ws/match/{match_id}/{participant_id}``
             or ``.../ws/match/{match_id}/bot`` for internal bots.
        bot: Your bot instance.
        max_retries: Reconnect attempt **cap**. When given, overrides the
            attempt count from ``retry_policy`` (the policy's backoff knobs
            still apply). When ``None`` (default), the policy's
            ``max_reconnect_attempts`` is used.
        retry_policy: :class:`chipzen.retry.RetryPolicy` controlling reconnect
            attempts + exponential backoff. Defaults to
            :data:`chipzen.retry.DEFAULT_RETRY_POLICY` (5 attempts, 500ms
            initial backoff doubling to a 30s cap).
        token: Bot API token (for the ``/bot`` endpoint).
        ticket: Single-use ticket (for competitive endpoints).
        match_id: Match UUID. Extracted from the URL if not provided.
        client_name: Client software name sent in the ``hello`` handshake.
        client_version: Client software version sent in the ``hello``
            handshake. Defaults to the installed SDK version
            (chipzen-ai/chipzen-sdk#41).
        safe_mode: When ``True`` (default), an exception raised by
            ``bot.decide()`` is logged and folded. Set ``False`` for dev/eval
            so the first exception propagates (chipzen-ai/chipzen-sdk#52).
        user_agent: Override the WS ``User-Agent`` header. Defaults to
            ``chipzen-sdk-python/<version>`` (chipzen-ai/chipzen-sdk#46).

    Returns:
        The ``match_end`` payload, or ``None`` if the connection closed without
        a clean ``match_end`` after exhausting retries.
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

    ua = user_agent or f"chipzen-sdk-python/{client_version}"
    policy = retry_policy if retry_policy is not None else DEFAULT_RETRY_POLICY
    # An explicit max_retries caps the attempt count; the policy still supplies
    # the backoff progression.
    max_attempts = max_retries if max_retries is not None else policy.max_reconnect_attempts

    retries = 0
    while retries <= max_attempts:
        try:
            async with connect(url, user_agent_header=ua) as ws:
                retries = 0  # reset on successful connect
                return await _run_session(
                    ws,
                    bot,
                    match_id=match_id,
                    token=token,
                    ticket=ticket,
                    client_name=client_name,
                    client_version=client_version,
                    safe_mode=safe_mode,
                )

        except asyncio.CancelledError:
            raise
        except BotDecisionError:
            # A deterministic bot bug (safe_mode=False) — terminal, not a
            # transient disconnect. Do not reconnect-retry; propagate so the
            # process exits non-zero.
            raise
        except Exception:
            retries += 1
            if retries > max_attempts:
                logger.exception("Max reconnection attempts reached, giving up")
                raise
            wait = policy.backoff_ms(retries) / 1000.0
            logger.warning(
                "Connection lost, retrying in %.1fs (attempt %d/%d)",
                wait,
                retries,
                max_attempts,
            )
            await asyncio.sleep(wait)
    return None


async def _run_session(
    ws: Any,
    bot: ChipzenBot,
    *,
    match_id: str,
    token: str | None,
    ticket: str | None,
    client_name: str,
    client_version: str,
    safe_mode: bool = True,
) -> dict | None:
    """Execute a single connected session: handshake + message loop.

    Returns the ``match_end`` payload on a clean finish, or ``None`` if the
    handshake failed / the socket closed without a ``match_end``.
    """

    async def _decide(state: GameState) -> tuple[Action, float]:
        """Run ``bot.decide`` OFF the session loop, with timing + safe_mode.

        ``bot.decide`` is synchronous user code and may legitimately block for
        the whole decision clock -- an LLM agent thinking, or the MCP bridge
        waiting on the agent's ``act`` call (up to ~28.5s at the 30s casual
        clock). Running it inline on the session's event loop would pin that
        loop: the same loop holds the External-API lobby WS (keepalive/ping) and
        co-schedules every concurrent match, so one outstanding decision left
        past the ~20s keepalive drops the lobby server-side and cascades into
        lost sibling matches (chipzen-ai/Chipzen#3904). Dispatch it to a worker
        thread via :func:`asyncio.to_thread` so the loop keeps answering
        heartbeats and servicing other matches while a decision is outstanding,
        all the way to the real clock. Each match has its own bot instance and
        the MCP bridge's registry is already thread-safe, so concurrent
        off-loop decisions are safe.

        Returns ``(action, decision_ms)``. Under ``safe_mode`` a raised
        exception is logged and folded; otherwise it is re-raised as a
        :class:`BotDecisionError` so the caller treats it as terminal.
        """
        start = time.monotonic()
        try:
            action = await asyncio.to_thread(bot.decide, state)
        except Exception as exc:
            logger.exception("Bot.decide() raised an exception")
            if not safe_mode:
                raise BotDecisionError(str(exc)) from exc
            action = Action.fold()
        return action, (time.monotonic() - start) * 1000.0

    def _call_hook(hook: Any, *args: Any) -> None:
        """Invoke a bot lifecycle hook with the same safe-mode guard as decide().

        A user exception in a lifecycle/stats callback must never tear down
        the WS session (chipzen-ai/chipzen-sdk#80): historically the raise
        killed the connection, the reconnect loop logged only "Connection
        lost, retrying" (hiding the real traceback), and the reconnect never
        re-attached — the bot zombied into an auto-substitute forfeit. Log
        the full traceback loudly at ERROR and keep the session alive.

        Under ``safe_mode=False`` (dev/eval) the exception propagates as
        :class:`BotDecisionError` so the bug surfaces immediately —
        mirroring the ``decide()`` semantics above.
        """
        try:
            hook(*args)
        except Exception as exc:
            logger.exception(
                "Bot lifecycle hook %s() raised an exception; "
                "ignoring it and continuing the session",
                getattr(hook, "__name__", repr(hook)),
            )
            if not safe_mode:
                raise BotDecisionError(str(exc)) from exc

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
        return None

    selected_version = server_hello.get("selected_version")
    server_versions = server_hello.get("supported_versions", []) or []
    if selected_version and selected_version not in SUPPORTED_PROTOCOL_VERSIONS:
        logger.error(
            "Server selected unsupported protocol version %r (client supports %s)",
            selected_version,
            SUPPORTED_PROTOCOL_VERSIONS,
        )
        return None
    if not selected_version and server_versions:
        if not any(v in SUPPORTED_PROTOCOL_VERSIONS for v in server_versions):
            logger.error(
                "No mutually supported protocol version (server=%s, client=%s)",
                server_versions,
                SUPPORTED_PROTOCOL_VERSIONS,
            )
            return None

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
            _call_hook(bot.on_match_start, payload)

        elif msg_type == "round_start":
            state = payload.get("state", {}) or {}
            dealer_seat = int(state.get("dealer_seat", dealer_seat))
            current_round_id = str(payload.get("round_id", current_round_id))
            _call_hook(bot.on_round_start, payload)

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
            action, decision_ms = await _decide(state)
            await _send_json(
                ws,
                {
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": payload.get("request_id"),  # MUST echo
                    **action.to_wire(),
                },
            )
            _call_hook(bot.on_decision_latency, decision_ms)

        elif msg_type == "action_rejected":
            # Retry within ``remaining_ms`` using the SAME request_id.
            reason = payload.get("reason")
            message = payload.get("message", "")
            remaining = payload.get("remaining_ms", 0)
            # Chipzen v0.3.53+ includes ``valid_actions`` in the rejection
            # payload so the SDK can pick a legal retry instead of guessing.
            # Older servers omit the field; fall back to ["check","fold"]
            # — the previous behavior — which still beats no retry at all
            # but produces a second rejection when neither is legal (the
            # auto-substitute streak observed in the alpha matrix
            # 2026-05-04/05).
            valid = payload.get("valid_actions") or ["check", "fold"]
            logger.warning(
                "Action rejected (%s): %s -- %dms remaining, "
                "retrying with safe fallback (valid=%s)",
                reason,
                message,
                remaining,
                valid,
            )
            safe = _safe_fallback_action(valid)
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
            _call_hook(bot.on_turn_result, payload)

        elif msg_type == "phase_change":
            _call_hook(bot.on_phase_change, payload)

        elif msg_type == "round_result":
            _call_hook(bot.on_round_result, payload)

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
                action, decision_ms = await _decide(state)
                await _send_json(
                    ws,
                    {
                        "type": "turn_action",
                        "match_id": match_id,
                        "request_id": pending.get("request_id"),
                        **action.to_wire(),
                    },
                )
                _call_hook(bot.on_decision_latency, decision_ms)

        elif msg_type == "match_end":
            _call_hook(bot.on_match_end, payload)
            return payload

        else:
            # Forward compatibility: silently ignore unknown message types.
            logger.debug("Ignoring unknown message type %r", msg_type)

    # Socket closed without a match_end.
    return None
