"""Push->pull bridge between the External-API WS session and MCP tools.

The External-API is a persistent WebSocket that PUSHES ``turn_request``
frames ("your turn -- decide within N ms"). MCP is PULL: the agent calls a
tool and gets a response, on its own schedule. This module is the seam
between the two:

* The SDK session (``chipzen.run_external_bot``) runs in a **background
  thread** with its own event loop, playing every match the platform
  dispatches. Its ``Bot.decide()`` is synchronous and blocks that thread's
  loop while waiting -- which is exactly why it gets a thread of its own.
* Each ``turn_request`` lands in a thread-safe :class:`TurnRegistry` (keyed
  by ``match_id``, because a single token can be in up to 5 concurrent
  matches). :class:`BridgeBot.decide` publishes the turn and blocks on a
  per-turn event until an MCP ``act`` call supplies the action or the local
  deadline elapses.
* MCP tools read/write the registry from the server's own event loop. The
  ergonomic core is ``wait_for_turn`` -- a long-poll that blocks (in a
  worker thread) until some match needs an action, so the agent's reasoning
  time IS the decision time.

Implementation status (chipzen-ai/Chipzen#3748 skeleton): the registry and
:class:`BridgeBot` are functional (they are pure-Python and unit-tested);
:class:`ExternalSession` is a minimal thread wrapper whose lifecycle edges
(cooperative stop, lobby-presence surfacing) are explicitly phase-3.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from chipzen import Action, Bot, GameState

from chipzen_mcp.config import McpConfig

logger = logging.getLogger("chipzen_mcp.bridge")

#: Decision clock the bridge assumes when a match doesn't announce one.
#: The agent-division target is ~30s unrated (chipzen-ai/Chipzen#3750);
#: the authoritative value is whatever ``match_start.turn_timeout_ms`` says.
DEFAULT_TURN_TIMEOUT_MS = 30_000

#: Subtracted from the announced clock before the bridge gives up waiting
#: for an ``act`` call, so the fallback action still reaches the server
#: comfortably inside the real deadline.
DEFAULT_SAFETY_MARGIN_MS = 1_500


def state_payload(state: GameState) -> dict[str, Any]:
    """Serialize an SDK :class:`chipzen.GameState` into a JSON-safe dict.

    Field names mirror the Layer-2 ``turn_request.state`` wire schema
    (see ``docs/protocol/POKER-GAME-STATE-PROTOCOL.md``) so agents can be
    pointed at one document for semantics.
    """
    return {
        "hand_number": state.hand_number,
        "phase": state.phase,
        "your_hole_cards": [f"{c.rank}{c.suit}" for c in state.hole_cards],
        "board": [f"{c.rank}{c.suit}" for c in state.board],
        "pot": state.pot,
        "your_stack": state.your_stack,
        "opponent_stacks": list(state.opponent_stacks),
        "your_seat": state.your_seat,
        "dealer_seat": state.dealer_seat,
        "to_call": state.to_call,
        "min_raise": state.min_raise,
        "max_raise": state.max_raise,
        "valid_actions": list(state.valid_actions),
        "action_history": list(state.action_history),
    }


@dataclass(frozen=True)
class TurnSnapshot:
    """One pending decision, as exposed to MCP tools.

    ``deadline_at`` is a local monotonic-free wall-clock estimate (epoch
    seconds) of when the bridge will stop waiting and fall back -- derived
    from the match's announced ``turn_timeout_ms`` minus a safety margin,
    NOT a server-authoritative value.
    """

    match_id: str
    request_id: str
    published_at: float
    deadline_at: float
    state: dict[str, Any]

    def remaining_ms(self, now: float | None = None) -> int:
        """Milliseconds until the bridge's local fallback deadline (>= 0)."""
        now = time.time() if now is None else now
        return max(0, int((self.deadline_at - now) * 1000))

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe view returned by ``wait_for_turn`` / ``get_match_state``."""
        return {
            "match_id": self.match_id,
            "request_id": self.request_id,
            "remaining_ms": self.remaining_ms(),
            "state": self.state,
        }


@dataclass
class _PendingTurn:
    """Internal rendezvous between ``BridgeBot.decide`` and ``act``."""

    snapshot: TurnSnapshot
    event: threading.Event = field(default_factory=threading.Event)
    action: Action | None = None


@dataclass
class _MatchRecord:
    """Everything the registry tracks for one match."""

    match_id: str
    rated: bool | None = None
    hand_number: int = 0
    last_turn: TurnSnapshot | None = None
    pending: _PendingTurn | None = None
    last_round_result: dict[str, Any] | None = None
    match_end: dict[str, Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "rated": self.rated,
            "hand_number": self.hand_number,
            "my_turn": self.pending is not None,
            "finished": self.match_end is not None,
        }


class TurnRegistry:
    """Thread-safe registry of matches and pending turns.

    Written by the SDK session thread (via :class:`BridgeBot` hooks), read
    and answered by MCP tool calls on the server loop. All public methods
    are safe to call from any thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turn_available = threading.Condition(self._lock)
        self._matches: dict[str, _MatchRecord] = {}

    # -- written by the session thread (BridgeBot hooks) -------------------

    def match_started(self, match_id: str, *, rated: bool | None = None) -> None:
        """Record a match the platform dispatched to us."""
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            if rated is not None:
                record.rated = rated

    def publish_turn(self, snapshot: TurnSnapshot) -> _PendingTurn:
        """Expose a pending decision and return the rendezvous to block on."""
        pending = _PendingTurn(snapshot=snapshot)
        with self._turn_available:
            record = self._matches.setdefault(
                snapshot.match_id, _MatchRecord(match_id=snapshot.match_id)
            )
            record.last_turn = snapshot
            record.pending = pending
            record.hand_number = int(snapshot.state.get("hand_number", record.hand_number))
            self._turn_available.notify_all()
        return pending

    def clear_pending(self, match_id: str) -> None:
        """Drop the pending turn (decide() timed out and fell back locally)."""
        with self._lock:
            record = self._matches.get(match_id)
            if record is not None:
                record.pending = None

    def record_round_result(self, match_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            record.last_round_result = result

    def record_match_end(self, match_id: str, end: dict[str, Any]) -> None:
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            record.match_end = end
            record.pending = None

    # -- read/answered by MCP tools ----------------------------------------

    def wait_for_any_turn(self, timeout_s: float) -> TurnSnapshot | None:
        """Block until any match has a pending turn; ``None`` on timeout.

        Returns the pending turn with the EARLIEST local deadline when
        several matches are waiting, so a multi-tabling agent naturally
        serves the most urgent seat first. Safe to call repeatedly: an
        unanswered turn is returned again on the next call.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._turn_available:
            while True:
                pending = [r.pending for r in self._matches.values() if r.pending is not None]
                if pending:
                    return min(pending, key=lambda p: p.snapshot.deadline_at).snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._turn_available.wait(timeout=remaining)

    def submit_action(self, match_id: str, action: Action) -> bool:
        """Answer the pending turn for ``match_id``.

        Returns ``False`` when there is nothing pending (not your turn, the
        match ended, or the bridge already fell back on timeout).
        """
        with self._lock:
            record = self._matches.get(match_id)
            if record is None or record.pending is None:
                return False
            pending = record.pending
            record.pending = None
        pending.action = action
        pending.event.set()
        return True

    def get_match(self, match_id: str) -> dict[str, Any] | None:
        """Full JSON view of one match (or ``None`` if unknown)."""
        with self._lock:
            record = self._matches.get(match_id)
            if record is None:
                return None
            view = record.to_summary()
            view["turn"] = (
                record.last_turn.to_payload()
                if record.pending is not None and record.last_turn is not None
                else None
            )
            view["last_round_result"] = record.last_round_result
            view["match_end"] = record.match_end
            return view

    def list_matches(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_summary() for r in self._matches.values()]

    def last_result(self, match_id: str | None = None) -> dict[str, Any] | None:
        """Latest round/match result -- for ``match_id``, or any match."""
        with self._lock:
            records = (
                [self._matches[match_id]]
                if match_id is not None and match_id in self._matches
                else list(self._matches.values())
            )
            for record in reversed(records):
                if record.match_end is not None or record.last_round_result is not None:
                    return {
                        "match_id": record.match_id,
                        "match_end": record.match_end,
                        "last_round_result": record.last_round_result,
                    }
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = [r for r in self._matches.values() if r.match_end is None]
            return {
                "active_matches": len(active),
                "pending_turns": sum(1 for r in active if r.pending is not None),
                "finished_matches": len(self._matches) - len(active),
            }


class BridgeBot(Bot):
    """SDK bot whose "strategy" is the connected MCP agent.

    One instance per match (``run_external_bot`` is given the class-factory
    form, so overlapping matches never share per-match state). ``decide``
    publishes the turn to the registry and blocks the session thread until
    ``act`` answers or the local deadline passes; the lifecycle hooks feed
    results back into the registry.
    """

    def __init__(
        self,
        registry: TurnRegistry,
        *,
        safety_margin_ms: int = DEFAULT_SAFETY_MARGIN_MS,
    ) -> None:
        self._registry = registry
        self._safety_margin_ms = safety_margin_ms
        self._match_id: str = ""
        self._turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS

    # -- lifecycle hooks (SDK session thread) -------------------------------

    def on_match_start(self, match_info: dict) -> None:
        """Capture match identity + the authoritative decision clock."""
        self._match_id = str(match_info.get("match_id", ""))
        announced = match_info.get("turn_timeout_ms")
        if isinstance(announced, (int, float)) and announced > 0:
            self._turn_timeout_ms = int(announced)
        self._registry.match_started(self._match_id)

    def on_round_result(self, message: dict) -> None:
        result = dict(message.get("result", {}) or {})
        self._registry.record_round_result(self._match_id, result)

    def on_match_end(self, results: dict) -> None:
        self._registry.record_match_end(self._match_id, results)

    # -- the decision itself ------------------------------------------------

    def decide(self, state: GameState) -> Action:
        """Publish the turn, then block until ``act`` answers or time runs out.

        On local timeout the bridge plays the same fallback the server
        would auto-apply anyway (``check`` if legal, else ``fold``) -- but
        does it EARLY enough to stay inside the clock, and logs loudly so a
        consistently-slow agent is visible rather than silently folding.
        """
        budget_ms = max(0, self._turn_timeout_ms - self._safety_margin_ms)
        now = time.time()
        snapshot = TurnSnapshot(
            match_id=self._match_id,
            request_id=state.request_id,
            published_at=now,
            deadline_at=now + budget_ms / 1000.0,
            state=state_payload(state),
        )
        pending = self._registry.publish_turn(snapshot)

        if pending.event.wait(timeout=budget_ms / 1000.0) and pending.action is not None:
            return pending.action

        self._registry.clear_pending(self._match_id)
        fallback = Action.check() if "check" in state.valid_actions else Action.fold()
        logger.warning(
            "match %s: no act() within %dms budget; falling back to %s",
            self._match_id,
            budget_ms,
            fallback.action,
        )
        return fallback


class ExternalSession:
    """Background thread running the SDK's External-API session.

    Owns a daemon thread that runs ``chipzen.run_external_bot`` with a
    :class:`BridgeBot`-per-match factory. Starting the session is what puts
    the bot "online" in the lobby so the platform can dispatch matches to it.

    Phase-3 notes (deliberately NOT implemented in the skeleton):

    * **Cooperative stop** -- ``run_external_bot`` has no external stop
      signal today; ``stop()`` is best-effort (the daemon thread dies with
      the process). Phase 3 either adds a stop event upstream in the SDK or
      drives the session loop directly here.
    * **Lobby-presence surfacing** -- the SDK logs lobby connect/evict but
      exposes no hook; ``get_status`` currently reports thread liveness,
      not lobby state. Phase 3 adds a small SDK hook (or log-handler tap)
      so ``lobby_connected`` is truthful.
    """

    def __init__(self, config: McpConfig, registry: TurnRegistry) -> None:
        self._config = config
        self._registry = registry
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        """Start the session thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="chipzen-mcp-session", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio

        from chipzen import run_external_bot

        try:
            asyncio.run(
                run_external_bot(
                    lambda: BridgeBot(self._registry),
                    bot_id=self._config.bot_id or None,
                    env=self._config.env,  # type: ignore[arg-type]
                    url=self._config.lobby_url,
                    token=self._config.token,
                    client_name="chipzen-mcp",
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via .error
            logger.exception("external session thread died")
            self._error = exc

    def stop(self) -> None:
        """Best-effort teardown -- see the phase-3 note in the class docstring."""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> BaseException | None:
        """The exception that killed the session thread, if any."""
        return self._error
