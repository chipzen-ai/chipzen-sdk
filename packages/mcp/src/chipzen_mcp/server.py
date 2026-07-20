"""The Chipzen MCP server: tool surface + entrypoint.

Ten tools over the :mod:`chipzen_mcp.bridge` registry (design:
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
``challenge_house_bot`` START an unrated ~30s-clock practice match vs a house
                        bot via the scoped extbot-token endpoint
                        (chipzen-ai/Chipzen#3750; on environments without it
                        the tool reports the dashboard fallback).
``join_rated_queue``    OPT INTO a RATED heads-up match vs another remote
                        agent via the matchmaking queue (chipzen-ai/Chipzen
                        #3907). Pairs immediately or queues until a partner
                        joins; the match seats via the same lobby push as
                        house-bot -> wait_for_turn.
``rated_queue_status``  Poll the rated queue: queued / idle / timed_out.
``leave_rated_queue``   Cancel: drop out of the rated queue (idempotent).
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
from chipzen_mcp.housebot import HttpPost, request_house_bot_challenge
from chipzen_mcp.matchmaking import (
    HttpRequest,
    request_join_rated_queue,
    request_leave_rated_queue,
    request_rated_queue_status,
)
from chipzen_mcp.stdio_guard import run_guarded_stdio

logger = logging.getLogger("chipzen_mcp.server")

#: Per-token concurrent-match cap enforced platform-side
#: (``external_api_max_concurrent_matches_per_token``).
CONCURRENT_MATCH_CAP = 5

#: Default/maximum long-poll for ``wait_for_turn``. Kept under common MCP
#: client request timeouts (typically 60s) so an idle poll returns cleanly
#: instead of erroring client-side.
DEFAULT_WAIT_TIMEOUT_MS = 55_000
MAX_WAIT_TIMEOUT_MS = 55_000

#: ``wait_for_turn`` blocks in a worker thread (the registry's ``Condition``
#: wait is not async), and a worker thread cannot be interrupted mid-wait when
#: the awaiting coroutine is cancelled on transport close. So the long-poll is
#: sliced into short blocking waits and re-driven from the coroutine: each
#: slice's ``Condition`` wait still wakes *immediately* when a turn is
#: published (no added latency for real turns), but a pending cancellation is
#: delivered between slices — bounding shutdown to one slice instead of the
#: full ~55s budget (chipzen-ai/Chipzen#3887).
WAIT_POLL_SLICE_S = 0.25

_VALID_ACTIONS = ("fold", "check", "call", "raise", "all_in")

_INSTRUCTIONS = """\
You are connected to Chipzen (chipzen.ai), an AI poker arena, as an
External-API bot. You can start a match yourself two ways -- practice with
`challenge_house_bot` (UNRATED, ~30s clock, vs a house bot) or ranked with
`join_rated_queue` (RATED, vs another remote agent; pairs immediately or
queues until a partner joins) -- or just wait for a match started from the
dashboard. Either way, once seated the loop is:

    wait_for_turn -> read the state -> act(match_id, action[, amount])

Rules of the road:
- Check `get_status` first: `lobby_connected` must be true before a match
  can reach you (or be started by `challenge_house_bot` / `join_rated_queue`).
- `join_rated_queue` may answer `queued` (no partner yet) -- then just call
  `wait_for_turn`; the rated match seats itself when a partner joins. If
  `wait_for_turn` stays idle, `rated_queue_status` tells you `timed_out` (no
  partner in time -> re-join) vs still `queued`.
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
    if session is not None:
        status.update(session.presence_snapshot())
    else:
        status.update({"lobby_connected": False, "lobby_state": None, "lobby_detail": None})
    return status


def _idle_response() -> dict[str, Any]:
    return {"status": "idle", "note": "No match is waiting on you; call wait_for_turn again."}


def _turn_response(snapshot: Any) -> dict[str, Any]:
    payload = snapshot.to_payload()
    payload["status"] = "your_turn"
    return payload


def wait_for_turn_impl(registry: TurnRegistry, timeout_ms: int) -> dict[str, Any]:
    timeout_ms = max(0, min(int(timeout_ms), MAX_WAIT_TIMEOUT_MS))
    snapshot = registry.wait_for_any_turn(timeout_ms / 1000.0)
    if snapshot is None:
        return _idle_response()
    return _turn_response(snapshot)


async def wait_for_turn_async(registry: TurnRegistry, timeout_ms: int) -> dict[str, Any]:
    """Cancellable long-poll: the primary agent loop.

    Semantically identical to :func:`wait_for_turn_impl`, but the blocking wait
    runs one short :data:`WAIT_POLL_SLICE_S` slice at a time, re-driven from
    this coroutine. That keeps the poll interruptible: when the MCP transport
    closes and FastMCP cancels this coroutine, the ``CancelledError`` lands
    between slices (within ~one slice) instead of being stuck behind the full
    ~55s budget — so ``main()``'s cooperative ``session.stop()`` runs promptly
    (chipzen-ai/Chipzen#3887). A published turn still wakes the current slice's
    ``Condition`` wait instantly, so real turns incur no extra latency.
    """
    timeout_ms = max(0, min(int(timeout_ms), MAX_WAIT_TIMEOUT_MS))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return _idle_response()
        slice_s = min(remaining, WAIT_POLL_SLICE_S)
        snapshot = await asyncio.to_thread(registry.wait_for_any_turn, slice_s)
        if snapshot is not None:
            return _turn_response(snapshot)


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


def challenge_house_bot_impl(
    config: McpConfig | None,
    session: ExternalSession | None,
    bot_name: str | None = None,
    *,
    post: HttpPost | None = None,
) -> dict[str, Any]:
    """Start an unrated house-bot challenge via the #3750 endpoint.

    The HTTP contract lives entirely in :mod:`chipzen_mcp.housebot`. The
    server is the authority on every precondition (token validity, lobby
    presence, concurrency cap) -- this impl only refuses locally when there
    is no configuration to authenticate with, and annotates the one local
    fact the server cannot know better than us: whether our own lobby
    session looks down (a dispatch to a disconnected bot cannot succeed).
    """
    if config is None:
        return {
            "status": "error",
            "error": "not_configured",
            "note": (
                "The server was started without Chipzen credentials; set "
                "CHIPZEN_EXTBOT_TOKEN and CHIPZEN_BOT_ID in the MCP config."
            ),
        }
    result = request_house_bot_challenge(config, bot_name, post=post)
    if (
        result.get("status") == "challenge_created"
        and session is not None
        and not session.lobby_connected
    ):
        result["warning"] = (
            "Challenge accepted, but this session's lobby connection does "
            "not look live -- check get_status; the match can only be "
            "dispatched to a connected session."
        )
    return result


def _not_configured() -> dict[str, Any]:
    """The local config gate shared by the rated-queue tools."""
    return {
        "status": "error",
        "error": "not_configured",
        "note": (
            "The server was started without Chipzen credentials; set "
            "CHIPZEN_EXTBOT_TOKEN and CHIPZEN_BOT_ID in the MCP config."
        ),
    }


def join_rated_queue_impl(
    config: McpConfig | None,
    session: ExternalSession | None,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Opt into the rated matchmaking queue via the #3907 endpoint.

    The HTTP contract lives entirely in :mod:`chipzen_mcp.matchmaking`. The
    server is the authority on every precondition (token validity, eligibility,
    lobby presence) -- this impl only refuses locally when there is no
    configuration to authenticate with, and annotates the one local fact the
    server cannot know better than us: whether our own lobby session looks down
    (a rated match, like a house-bot match, can only be dispatched to a
    connected session, and the ``matched`` seating push is delivered there).
    """
    if config is None:
        return _not_configured()
    result = request_join_rated_queue(config, request=request)
    if (
        result.get("status") in ("queued", "matched")
        and session is not None
        and not session.lobby_connected
    ):
        result["warning"] = (
            "Queued/matched, but this session's lobby connection does not "
            "look live -- check get_status; the match can only be dispatched "
            "to a connected session."
        )
    return result


def rated_queue_status_impl(
    config: McpConfig | None,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Poll the rated-queue state via the #3907 endpoint."""
    if config is None:
        return _not_configured()
    return request_rated_queue_status(config, request=request)


def leave_rated_queue_impl(
    config: McpConfig | None,
    *,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Cancel / drop out of the rated queue via the #3907 endpoint."""
    if config is None:
        return _not_configured()
    return request_leave_rated_queue(config, request=request)


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
        return await wait_for_turn_async(registry, timeout_ms)

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
    async def challenge_house_bot(bot_name: str | None = None) -> dict[str, Any]:
        """Start an UNRATED practice match against a Chipzen house bot on a
        relaxed ~30s decision clock (never affects ratings). On success the
        match is dispatched to this session -- go straight into the
        wait_for_turn loop. bot_name optionally names the house bot;
        omitted, the server picks the default first opponent. If this
        environment doesn't have the endpoint yet you get
        error=endpoint_not_available with a dashboard fallback. Both success
        and error results carry request_id -- the platform correlator; quote
        it when reporting a problem (for a pre-match failure it is the only
        id that exists)."""
        return await asyncio.to_thread(challenge_house_bot_impl, config, session, bot_name)

    @mcp.tool()
    async def join_rated_queue() -> dict[str, Any]:
        """Opt into the RATED matchmaking queue to play another remote agent
        heads-up for real Glicko rating. Returns status="matched" (an opponent
        was waiting -- a rated match is dispatching to this session now, go
        straight into the wait_for_turn loop) or status="queued" with your
        1-based position (no partner yet). When queued, the match seats itself
        via the lobby the moment a partner joins -- just call wait_for_turn; if
        it stays idle, poll rated_queue_status (status="timed_out" means no
        partner arrived within queue_ttl_seconds -- call join_rated_queue again
        to re-enter). Use leave_rated_queue to cancel. Unlike
        challenge_house_bot this is RATED and peer-vs-peer; no match id is
        returned (seating comes via the lobby, same as house-bot). If this
        environment lacks the endpoint you get error=endpoint_not_available."""
        return await asyncio.to_thread(join_rated_queue_impl, config, session)

    @mcp.tool()
    async def rated_queue_status() -> dict[str, Any]:
        """Poll your rated matchmaking queue state without changing it.
        status is "queued" (still waiting; position/waiting_seconds included),
        "idle" (not in the queue -- never joined, already matched, or left), or
        "timed_out" (no partner within queue_ttl_seconds; your entry has now
        been dropped -- reading timed_out is one-shot, re-join to retry). Use
        this to detect a timeout while you wait; a live match still arrives via
        wait_for_turn, not here."""
        return await asyncio.to_thread(rated_queue_status_impl, config)

    @mcp.tool()
    async def leave_rated_queue() -> dict[str, Any]:
        """Cancel: remove yourself from the rated matchmaking queue. Idempotent
        -- returns status="left" if you were waiting, "not_queued" if you
        weren't. Does nothing to a match that has already been dispatched (that
        one plays out via wait_for_turn)."""
        return await asyncio.to_thread(leave_rated_queue_impl, config)

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
    try:
        # Windows-resilient stdio transport (chipzen-ai/Chipzen#3888): owns the
        # stdin reader so EOF always tears the server down, guards oversized
        # frames, and watchdogs stdin-close so the process never lingers.
        run_guarded_stdio(server)
    except KeyboardInterrupt:
        logger.info("chipzen-mcp: interrupted")
    finally:
        # The MCP transport is gone (client exited, stdin closed, or ^C):
        # cooperatively stop the SDK session so the lobby/gateway sockets
        # close cleanly instead of dying with the daemon thread.
        if session.stop():
            logger.info("chipzen-mcp: session stopped cleanly")
        else:
            logger.warning("chipzen-mcp: session still winding down at exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
