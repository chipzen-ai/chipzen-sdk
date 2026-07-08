"""The Chipzen MCP server: tool surface + entrypoint.

Seven tools over the :mod:`chipzen_mcp.bridge` registry (design:
chipzen-ai/Chipzen#3748):

======================  =====================================================
``get_status``          Am I online? Active matches vs the 5-per-token cap.
``wait_for_turn``       Long-poll until some match needs an action (the
                        primary agent loop -- reasoning time IS decision
                        time).
``get_match_state``     Explicit re-read of one match's pending turn.
``act``                 Submit fold/check/call/raise/all_in for a pending
                        turn.
``list_matches``        All in-flight/recent matches at a glance.
``get_last_result``     Round/match outcome (winners, payouts, showdown).
``challenge_house_bot`` START a practice match vs a house bot. STUB until
                        the scoped server endpoint lands
                        (chipzen-ai/Chipzen#3750).
======================  =====================================================

Transport is stdio. Everything written to stdout is protocol traffic, so all
logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from chipzen import Action
from mcp.server.fastmcp import FastMCP

from chipzen_mcp.bridge import ExternalSession, TurnRegistry
from chipzen_mcp.config import McpConfig, McpConfigError, load_config

logger = logging.getLogger("chipzen_mcp.server")

#: Per-token concurrent-match cap enforced platform-side
#: (``external_api_max_concurrent_matches_per_token``).
CONCURRENT_MATCH_CAP = 5

#: Default/maximum long-poll for ``wait_for_turn``. Kept under common MCP
#: client request timeouts (typically 60s) so an idle poll returns cleanly
#: instead of erroring client-side.
DEFAULT_WAIT_TIMEOUT_MS = 55_000
MAX_WAIT_TIMEOUT_MS = 55_000

_VALID_ACTIONS = ("fold", "check", "call", "raise", "all_in")

_INSTRUCTIONS = """\
You are connected to Chipzen (chipzen.ai), an AI poker arena, as an
External-API bot. Matches are dispatched TO you; once seated, the loop is:

    wait_for_turn -> read the state -> act(match_id, action[, amount])

Rules of the road:
- `wait_for_turn` blocks until a match needs your decision. Call it in a
  loop; `{"status": "idle"}` just means nothing needs you yet.
- Decide within `remaining_ms`. If you don't act in time the bridge (and
  the server) auto-plays check/fold and you are just donating chips.
- `raise` amount is the TOTAL bet size, bounded by state.min_raise /
  state.max_raise (it is clamped server-side, never rejected for range).
- Card notation: rank+suit, e.g. "Ah" = ace of hearts, "Td" = ten of
  diamonds. State semantics: docs/protocol/POKER-GAME-STATE-PROTOCOL.md.
"""


# ---------------------------------------------------------------------------
# Tool implementations (module-level, dependency-injected, unit-testable).
# ``build_server`` wraps these as FastMCP tools.
# ---------------------------------------------------------------------------


def get_status_impl(
    registry: TurnRegistry,
    session: ExternalSession | None,
    config: McpConfig | None,
) -> dict[str, Any]:
    status = registry.status()
    status.update(
        {
            "bot_id": config.bot_id if config else None,
            "env": (config.env or "prod") if config else None,
            "session_running": session.running if session else False,
            "session_error": str(session.error) if session and session.error else None,
            "concurrent_match_cap": CONCURRENT_MATCH_CAP,
        }
    )
    return status


def wait_for_turn_impl(registry: TurnRegistry, timeout_ms: int) -> dict[str, Any]:
    timeout_ms = max(0, min(int(timeout_ms), MAX_WAIT_TIMEOUT_MS))
    snapshot = registry.wait_for_any_turn(timeout_ms / 1000.0)
    if snapshot is None:
        return {"status": "idle", "note": "No match is waiting on you; call wait_for_turn again."}
    payload = snapshot.to_payload()
    payload["status"] = "your_turn"
    return payload


def get_match_state_impl(registry: TurnRegistry, match_id: str) -> dict[str, Any]:
    view = registry.get_match(match_id)
    if view is None:
        return {"error": "unknown_match", "match_id": match_id}
    return view


def act_impl(
    registry: TurnRegistry,
    match_id: str,
    action: str,
    amount: int | None = None,
) -> dict[str, Any]:
    if action not in _VALID_ACTIONS:
        return {
            "accepted": False,
            "error": "invalid_action",
            "note": f"action must be one of {', '.join(_VALID_ACTIONS)}",
        }
    if action == "raise":
        if amount is None or amount <= 0:
            return {
                "accepted": False,
                "error": "amount_required",
                "note": "raise needs amount = the TOTAL bet size "
                "(state.min_raise <= amount <= state.max_raise)",
            }
        chosen = Action.raise_to(int(amount))
    else:
        chosen = getattr(Action, action)()

    if not registry.submit_action(match_id, chosen):
        return {
            "accepted": False,
            "error": "no_pending_turn",
            "note": "Nothing is awaiting your action in this match (not your "
            "turn, the match ended, or the decision clock already expired).",
        }
    return {"accepted": True, "action": action, "amount": amount}


def list_matches_impl(registry: TurnRegistry) -> list[dict[str, Any]]:
    return registry.list_matches()


def get_last_result_impl(registry: TurnRegistry, match_id: str | None = None) -> dict[str, Any]:
    result = registry.last_result(match_id)
    if result is None:
        return {"status": "no_results_yet"}
    return result


def challenge_house_bot_impl(bot_name: str | None = None) -> dict[str, Any]:
    """STUB -- requires server support landing in chipzen-ai/Chipzen#3750."""
    return {
        "status": "not_implemented",
        "note": (
            "Starting a match from the agent requires the scoped house-bot "
            "challenge endpoint for External-API tokens, which is tracked in "
            "chipzen-ai/Chipzen#3750 and has not landed yet. Until then: keep "
            "this server connected, then start an UNRANKED challenge against "
            "a house bot from the Chipzen dashboard (/challenges) -- the "
            "match will be dispatched here automatically."
        ),
        "requested_bot": bot_name,
    }


# ---------------------------------------------------------------------------
# Server assembly
# ---------------------------------------------------------------------------


def build_server(
    registry: TurnRegistry,
    session: ExternalSession | None = None,
    config: McpConfig | None = None,
) -> FastMCP:
    """Assemble the FastMCP server over an injected registry/session.

    Kept separate from :func:`main` so tests can build a server around a
    fake registry without touching the network or the environment.
    """
    mcp = FastMCP("chipzen", instructions=_INSTRUCTIONS)

    @mcp.tool()
    def get_status() -> dict[str, Any]:
        """Connection/session status: am I online, how many matches are active
        (per-token cap is 5), and did the background session hit an error."""
        return get_status_impl(registry, session, config)

    @mcp.tool()
    async def wait_for_turn(timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS) -> dict[str, Any]:
        """Block until it's your turn in ANY match, then return that match's
        full decision state (hole cards, board, pot, valid_actions,
        remaining_ms). Returns {"status": "idle"} on timeout -- just call it
        again. This is your primary loop; think, then call act()."""
        return await asyncio.to_thread(wait_for_turn_impl, registry, timeout_ms)

    @mcp.tool()
    def get_match_state(match_id: str) -> dict[str, Any]:
        """Re-read one match: pending turn (if it's your move), last hand
        result, and final result when the match is over."""
        return get_match_state_impl(registry, match_id)

    @mcp.tool()
    def act(match_id: str, action: str, amount: int | None = None) -> dict[str, Any]:
        """Play your pending turn. action is one of fold/check/call/raise/
        all_in; `raise` needs amount = the TOTAL bet size (min_raise <=
        amount <= max_raise; clamped server-side)."""
        return act_impl(registry, match_id, action, amount)

    @mcp.tool()
    def list_matches() -> list[dict[str, Any]]:
        """All matches this session: which are live, which await your action,
        which finished."""
        return list_matches_impl(registry)

    @mcp.tool()
    def get_last_result(match_id: str | None = None) -> dict[str, Any]:
        """Latest hand/match outcome (winners, payouts, showdown) for one
        match, or the most recent across all matches."""
        return get_last_result_impl(registry, match_id)

    @mcp.tool()
    def challenge_house_bot(bot_name: str | None = None) -> dict[str, Any]:
        """Start an UNRATED practice match against a Chipzen house bot.
        NOT FUNCTIONAL YET: requires server support landing in
        chipzen-ai/Chipzen#3750; until then start the challenge from the
        dashboard and it will be dispatched to this session."""
        return challenge_house_bot_impl(bot_name)

    return mcp


def main() -> int:
    """Console entrypoint (``chipzen-mcp``): stdio transport."""
    # stdout belongs to the MCP protocol -- log to stderr only.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except McpConfigError as exc:
        print(f"chipzen-mcp: {exc}", file=sys.stderr)
        return 2

    registry = TurnRegistry()
    session = ExternalSession(config, registry)
    session.start()
    logger.info(
        "chipzen-mcp: session starting (bot_id=%s env=%s); serving MCP on stdio",
        config.bot_id,
        config.env or "prod",
    )
    server = build_server(registry, session, config)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
