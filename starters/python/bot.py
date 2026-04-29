#!/usr/bin/env python3
"""Chipzen starter bot — tight-aggressive preflop, check-call postflop.

Implements the Chipzen two-layer protocol:
  Layer 1 (Transport): docs/protocol/TRANSPORT-PROTOCOL.md
  Layer 2 (Poker):     docs/protocol/POKER-GAME-STATE-PROTOCOL.md

Usage:  python bot.py ws://localhost:8001/ws/match/{match_id}/bot
Env:    CHIPZEN_WS_URL     — WebSocket URL (alternative to CLI arg)
        CHIPZEN_TOKEN      — Bot API token (for /bot endpoints)
        CHIPZEN_TICKET     — Single-use ticket (for competitive endpoints)
        CHIPZEN_MATCH_ID   — Match UUID (auto-extracted from URL if omitted)
"""

import asyncio
import json
import os
import sys

import websockets

# -----------------------------------------------------------------------------
# Strategy: tight-aggressive preflop, check-call postflop
# -----------------------------------------------------------------------------

# Strong preflop hands: pairs 77+, broadways, suited aces
STRONG = {
    "AA",
    "KK",
    "QQ",
    "JJ",
    "TT",
    "99",
    "88",
    "77",
    "AKs",
    "AKo",
    "AQs",
    "AQo",
    "AJs",
    "ATs",
    "KQs",
    "KQo",
    "KJs",
    "QJs",
    "JTs",
}

PROTOCOL_VERSIONS = ["1.0"]
CLIENT_NAME = "chipzen-starter-python"
CLIENT_VERSION = "0.2.0"


def log(msg: str) -> None:
    """Log to stderr so stdout stays clean for piping."""
    print(f"[bot] {msg}", file=sys.stderr)


def hand_key(cards: list[str]) -> str:
    """Convert ['Ah','Kd'] to 'AKo' shorthand (rank-ordered, 's'/'o' for suit)."""
    if not cards or len(cards) != 2:
        return ""
    order = "23456789TJQKA"
    r0, s0 = cards[0][:-1], cards[0][-1]
    r1, s1 = cards[1][:-1], cards[1][-1]
    if order.index(r0) < order.index(r1):
        r0, s0, r1, s1 = r1, s1, r0, s0
    return f"{r0}{r1}" if r0 == r1 else f"{r0}{r1}{'s' if s0 == s1 else 'o'}"


def decide(state: dict, valid_actions: list[str]) -> dict:
    """Pick an action from a Layer 2 turn_request state.

    Returns a dict with ``action`` and (optional) ``params`` keys.
    """
    phase = state.get("phase", "preflop")
    to_call = int(state.get("to_call", 0))
    pot = int(state.get("pot", 0))
    min_raise = int(state.get("min_raise", 0))
    max_raise = int(state.get("max_raise", 0))
    key = hand_key(state.get("your_hole_cards", []))

    def has(a: str) -> bool:
        return a in valid_actions

    # Preflop: raise strong hands, call medium, fold junk
    if phase == "preflop":
        if key in STRONG and has("raise") and min_raise > 0:
            amount = min(max(min_raise, 1), max_raise) if max_raise else min_raise
            log(f"Preflop raise with {key} to {amount}")
            return {"action": "raise", "params": {"amount": amount}}
        if to_call > 0 and has("call"):
            log(f"Preflop call ({key})")
            return {"action": "call", "params": {}}
        if has("check"):
            return {"action": "check", "params": {}}
        return {"action": "fold", "params": {}}

    # Postflop: check when free, call small bets, fold large ones
    if has("check"):
        return {"action": "check", "params": {}}
    if has("call") and to_call <= pot * 0.5:
        log(f"Postflop call {to_call} into {pot}")
        return {"action": "call", "params": {}}
    if has("fold"):
        return {"action": "fold", "params": {}}
    # Last-resort fallback (shouldn't reach here — server always offers fold or check)
    return {"action": "check", "params": {}}


# -----------------------------------------------------------------------------
# Protocol helpers
# -----------------------------------------------------------------------------


def extract_match_id(url: str) -> str:
    """Extract match UUID from a ``/ws/.../match/{match_id}/...`` URL."""
    parts = url.rstrip("/").split("/")
    for i, part in enumerate(parts):
        if part == "match" and i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


async def send_json(ws, msg: dict) -> None:
    await ws.send(json.dumps(msg))


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------


async def run(url: str, token: str | None, ticket: str | None, match_id: str) -> None:
    log(f"Connecting to {url}")
    async with websockets.connect(url) as ws:
        log("Connected!")

        # --- Layer 1 handshake ----------------------------------------------
        # 1. Send authenticate (must be the first bot message).
        auth_msg: dict = {"type": "authenticate", "match_id": match_id}
        if token:
            auth_msg["token"] = token
        elif ticket:
            auth_msg["ticket"] = ticket
        else:
            # Sidecar / localhost dev may accept empty token. In production,
            # one of {token, ticket} is required.
            auth_msg["token"] = ""
        await send_json(ws, auth_msg)

        # 2. Receive server hello.
        server_hello = json.loads(await ws.recv())
        if server_hello.get("type") != "hello":
            log(f"Expected 'hello' from server, got {server_hello.get('type')!r}")
            return
        log(
            f"Server hello: version={server_hello.get('selected_version')} "
            f"game_type={server_hello.get('game_type')}"
        )

        # 3. Send client hello.
        await send_json(
            ws,
            {
                "type": "hello",
                "match_id": match_id,
                "supported_versions": PROTOCOL_VERSIONS,
                "client_name": CLIENT_NAME,
                "client_version": CLIENT_VERSION,
            },
        )

        # --- Main message loop ----------------------------------------------
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "ping":
                # Heartbeat: must respond within 5000ms.
                await send_json(ws, {"type": "pong", "match_id": match_id})

            elif mtype == "match_start":
                cfg = msg.get("game_config", {})
                log(
                    f"Match start: blinds {cfg.get('small_blind')}/{cfg.get('big_blind')} "
                    f"stacks {cfg.get('starting_stack')}"
                )

            elif mtype == "round_start":
                state = msg.get("state", {})
                hand_no = state.get("hand_number", "?")
                cards = state.get("your_hole_cards", [])
                log(f"Hand {hand_no} dealt: {cards}")

            elif mtype == "turn_request":
                state = msg.get("state", {})
                valid = msg.get("valid_actions", [])
                request_id = msg.get("request_id")
                action = decide(state, valid)
                log(f"Turn h{state.get('hand_number', '?')} {state.get('phase', '?')}: {action}")
                await send_json(
                    ws,
                    {
                        "type": "turn_action",
                        "match_id": match_id,
                        "request_id": request_id,  # MUST echo
                        "action": action["action"],
                        "params": action.get("params", {}),
                    },
                )

            elif mtype == "action_rejected":
                # Server rejected our action; retry within remaining_ms using
                # the SAME request_id. Fall back to a safe action.
                request_id = msg.get("request_id")
                remaining = msg.get("remaining_ms", 0)
                log(
                    f"Action rejected ({msg.get('reason')}): {msg.get('message')} "
                    f"— {remaining}ms remaining, retrying with safe fallback"
                )
                # Safe fallback: check if allowed, else fold.
                safe = {"action": "check", "params": {}}
                await send_json(
                    ws,
                    {
                        "type": "turn_action",
                        "match_id": match_id,
                        "request_id": request_id,
                        **safe,
                    },
                )

            elif mtype == "turn_result":
                details = msg.get("details", {})
                log(
                    f"Turn result seat={details.get('seat')} "
                    f"action={details.get('action')} amount={details.get('amount', 0)}"
                )

            elif mtype == "phase_change":
                new = msg.get("state", {})
                log(f"Phase -> {new.get('phase')} board={new.get('board')}")

            elif mtype == "round_result":
                result = msg.get("result", {})
                log(
                    f"Hand {result.get('hand_number', '?')} over: "
                    f"winners={result.get('winner_seats')} pot={result.get('pot')}"
                )

            elif mtype == "action_timeout":
                log(f"Timed out; server auto-applied: {msg.get('auto_action')}")

            elif mtype == "session_control":
                log(f"Session control: {msg.get('action')} ({msg.get('reason')})")

            elif mtype == "error":
                log(f"Error [{msg.get('code')}]: {msg.get('message')}")

            elif mtype == "match_end":
                log(f"Match ended: {msg.get('reason')}")
                break

            else:
                # Forward-compat: silently ignore unknown message types.
                pass

    log("Disconnected")


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CHIPZEN_WS_URL", "")
    if not url:
        log("Usage: python bot.py <ws_url>")
        log("Env:   CHIPZEN_WS_URL, CHIPZEN_TOKEN, CHIPZEN_TICKET")
        sys.exit(1)
    token = os.environ.get("CHIPZEN_TOKEN") or None
    ticket = os.environ.get("CHIPZEN_TICKET") or None
    match_id = os.environ.get("CHIPZEN_MATCH_ID") or extract_match_id(url)
    asyncio.run(run(url, token, ticket, match_id))


if __name__ == "__main__":
    main()
