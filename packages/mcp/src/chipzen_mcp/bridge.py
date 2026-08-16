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

Lifecycle (phase 3, chipzen-ai/Chipzen#3748): :class:`ExternalSession` owns
the SDK thread end-to-end — cooperative stop (``stop()`` cancels the session
from any thread and joins it), and truthful lobby-presence / reconnect-state
surfacing via :class:`SdkLogTap`, which derives session state from the SDK's
own log events (the SDK's public surface is ``Bot``; it exposes no lifecycle
hook, and the tap avoids forking the protocol code just to observe it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Coroutine
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

#: How many FINISHED matches the registry retains before evicting the oldest
#: (by result recency). This bounds ``TurnRegistry._matches`` over a long-lived
#: multi-match session so ``list_matches`` / memory don't grow monotonically
#: with lifetime match count (chipzen-ai/Chipzen#3939). Active matches are
#: NEVER counted against this cap nor evicted (the platform separately caps
#: concurrent active matches at 5 per token); the cap only sweeps terminal
#: records that were never explicitly consumed via ``get_last_result``. The
#: N most-recently-resulted finished matches are kept, so ``last_result()``'s
#: "most recent across all matches" answer is always retained.
MAX_FINISHED_MATCHES = 50

#: Outcomes of :meth:`TurnRegistry.submit_action` (mapped 1:1 onto ``act``'s
#: response by :func:`chipzen_mcp.server.act_impl`).
#:
#: ``SUBMIT_STALE_TURN`` is the optimistic-concurrency verdict added for
#: chipzen-ai/Chipzen#3906: the caller quoted a ``request_id`` that is NOT the
#: turn currently pending on that match, so the action was computed against a
#: state the table has already moved past. Answering it would silently apply a
#: decision to the WRONG turn, so it is refused instead.
SUBMIT_ACCEPTED = "accepted"
SUBMIT_NO_PENDING_TURN = "no_pending_turn"
SUBMIT_STALE_TURN = "stale_turn"


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
    """Internal rendezvous between ``BridgeBot.decide`` and ``act``.

    ``cancelled`` is the shutdown sentinel (chipzen-ai/Chipzen#3900): the
    registry sets it (and the event) when the session is closing, so
    ``decide()`` stops waiting immediately instead of holding the SDK session
    thread for the rest of its decision budget. It is a FLAG, never a fake
    :class:`chipzen.Action` -- nothing that could be mistaken for a real
    decision is ever handed to the SDK; ``decide()`` plays its normal
    check/fold fallback on this path.
    """

    snapshot: TurnSnapshot
    event: threading.Event = field(default_factory=threading.Event)
    action: Action | None = None
    cancelled: bool = False


#: Per-match gateway connection states surfaced in match summaries.
#: ``pending`` (dispatched, gateway not opened yet) -> ``connected`` ->
#: possibly ``reconnecting`` (mid-match drop, resume in progress) ->
#: ``abandoned`` (reconnect budget exhausted; the match is forfeit).
MATCH_CONN_PENDING = "pending"
MATCH_CONN_CONNECTED = "connected"
MATCH_CONN_RECONNECTING = "reconnecting"
MATCH_CONN_ABANDONED = "abandoned"


@dataclass
class _MatchRecord:
    """Everything the registry tracks for one match."""

    match_id: str
    rated: bool | None = None
    hand_number: int = 0
    connection: str = MATCH_CONN_PENDING
    reconnect_attempt: int | None = None
    turn_timeout_ms: int | None = None
    last_turn: TurnSnapshot | None = None
    pending: _PendingTurn | None = None
    last_round_result: dict[str, Any] | None = None
    match_end: dict[str, Any] | None = None
    #: Registry-wide monotonic rank of this match's most recent result
    #: (round result or match end). 0 means "no result yet". Used to answer
    #: "most recent across all matches" by recency of the RESULT rather than
    #: by dict-insertion order of the match (chipzen-ai/Chipzen#3884).
    result_seq: int = 0

    def to_summary(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "rated": self.rated,
            "hand_number": self.hand_number,
            "connection": self.connection,
            "reconnect_attempt": self.reconnect_attempt,
            "turn_timeout_ms": self.turn_timeout_ms,
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
        #: Monotonic counter stamped onto each result as it lands, so
        #: ``last_result()`` can pick the most-recently-produced result
        #: across matches instead of the first in insertion order.
        self._result_counter = 0
        #: Set by :meth:`close` when the session is shutting down: no turn can
        #: be pending any more, so waiters return at once and any turn the SDK
        #: publishes mid-teardown is born cancelled (chipzen-ai/Chipzen#3900).
        self._closed = False

    def _next_result_seq(self) -> int:
        """Next monotonic result rank. Caller MUST hold ``self._lock``."""
        self._result_counter += 1
        return self._result_counter

    # -- written by the session thread (BridgeBot hooks) -------------------

    def match_started(
        self,
        match_id: str,
        *,
        rated: bool | None = None,
        turn_timeout_ms: int | None = None,
    ) -> None:
        """Record a match the platform dispatched to us.

        ``turn_timeout_ms`` is the match's announced decision clock (from
        ``match_start``; the casual agent division advertises + enforces
        ~30s per chipzen-ai/Chipzen#3750), surfaced in match summaries so
        the agent can see the clock it is playing under.
        """
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            if rated is not None:
                record.rated = rated
            if turn_timeout_ms is not None:
                record.turn_timeout_ms = turn_timeout_ms

    def set_match_connection(
        self, match_id: str, state: str, *, attempt: int | None = None
    ) -> None:
        """Record the gateway-connection state of one match (see MATCH_CONN_*)."""
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            record.connection = state
            record.reconnect_attempt = attempt

    def publish_turn(self, snapshot: TurnSnapshot) -> _PendingTurn:
        """Expose a pending decision and return the rendezvous to block on.

        After :meth:`close` the returned rendezvous is already cancelled and is
        NOT registered as pending: a ``turn_request`` that lands mid-teardown
        must not park the session thread on an event nobody will ever set
        (chipzen-ai/Chipzen#3900).
        """
        pending = _PendingTurn(snapshot=snapshot)
        with self._turn_available:
            if self._closed:
                pending.cancelled = True
                pending.event.set()
                return pending
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

    def close(self) -> int:
        """Cancel every pending turn and refuse new ones (shutdown).

        Called by :meth:`ExternalSession.stop` so a session that dies with a
        turn in flight unwinds promptly (chipzen-ai/Chipzen#3900). Without
        this, ``BridgeBot.decide`` sits on its per-turn event for the rest of
        the ~28.5s decision budget while blocking the SDK session thread, so
        ``stop()``'s ``thread.join(timeout)`` times out and the process lingers
        to the stop cap (~10s) before abandoning the thread.

        Resolving each :class:`_PendingTurn` with the ``cancelled`` flag makes
        ``decide()`` return its normal check/fold fallback immediately -- no
        synthetic action is invented, and the turn is no longer answerable via
        ``act`` (it is dropped from its match record here).

        Returns:
            How many pending turns were cancelled.

        Terminal for the session; :meth:`reopen` (called by
        :meth:`ExternalSession.start`) puts the registry back in service.
        """
        with self._turn_available:
            self._closed = True
            cancelled = []
            for record in self._matches.values():
                if record.pending is not None:
                    cancelled.append(record.pending)
                    record.pending = None
            # Wake any wait_for_any_turn sleeper so it sees the closed registry.
            self._turn_available.notify_all()
        for pending in cancelled:
            pending.cancelled = True
            pending.event.set()
        return len(cancelled)

    def reopen(self) -> None:
        """Undo :meth:`close` so a restarted session can serve turns again."""
        with self._lock:
            self._closed = False

    def record_round_result(self, match_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            record.last_round_result = result
            record.result_seq = self._next_result_seq()

    def record_match_end(self, match_id: str, end: dict[str, Any]) -> None:
        with self._lock:
            record = self._matches.setdefault(match_id, _MatchRecord(match_id=match_id))
            record.match_end = end
            record.pending = None
            # A finished match never serves another pending turn, so its last
            # in-flight turn snapshot (with full action_history) is dead weight
            # -- drop it to shrink the retained per-record footprint (#3939).
            record.last_turn = None
            record.result_seq = self._next_result_seq()
            self._evict_finished_over_cap()

    def _evict_finished_over_cap(self) -> None:
        """Evict oldest FINISHED matches beyond ``MAX_FINISHED_MATCHES``.

        Caller MUST hold ``self._lock``. Keeps the most-recently-resulted
        finished matches (by :attr:`_MatchRecord.result_seq`) so
        :meth:`last_result`'s "most recent across all matches" answer is never
        the one evicted; active matches (``match_end is None``) are never
        counted against the cap nor removed (chipzen-ai/Chipzen#3939).
        """
        finished = [r for r in self._matches.values() if r.match_end is not None]
        excess = len(finished) - MAX_FINISHED_MATCHES
        if excess <= 0:
            return
        finished.sort(key=lambda r: r.result_seq)  # oldest result first
        for record in finished[:excess]:
            del self._matches[record.match_id]

    # -- read/answered by MCP tools ----------------------------------------

    def wait_for_any_turn(self, timeout_s: float) -> TurnSnapshot | None:
        """Block until any match has a pending turn; ``None`` on timeout.

        Returns the pending turn with the EARLIEST local deadline when
        several matches are waiting, so a multi-tabling agent naturally
        serves the most urgent seat first. Safe to call repeatedly: an
        unanswered turn is returned again on the next call. Returns ``None``
        at once on a :meth:`close`\\ d registry -- no further turn can arrive.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._turn_available:
            while True:
                if self._closed:
                    return None
                pending = [r.pending for r in self._matches.values() if r.pending is not None]
                if pending:
                    return min(pending, key=lambda p: p.snapshot.deadline_at).snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._turn_available.wait(timeout=remaining)

    def submit_action(self, match_id: str, action: Action, *, request_id: str | None = None) -> str:
        """Answer the pending turn for ``match_id``.

        ``request_id`` is the optimistic-concurrency token from the turn the
        caller actually observed (``wait_for_turn`` / ``get_match_state``
        surface it as ``request_id``). When supplied it MUST match the turn
        currently pending on that match; otherwise the submission is refused
        as :data:`SUBMIT_STALE_TURN` and the pending turn is left untouched.

        That guard closes chipzen-ai/Chipzen#3906: keyed on ``match_id`` alone,
        an ``act`` that arrives after the bridge's local fallback already
        cleared turn A lands on the NEXT turn B of the same match -- applying a
        decision computed against A's state, and reporting success. The token
        makes that detectable instead of silent.

        ``request_id=None`` preserves the pre-#3906 behaviour (answer whatever
        is pending) so existing agents keep working; passing it is strongly
        recommended.

        Returns:
            :data:`SUBMIT_ACCEPTED` when the pending turn was answered,
            :data:`SUBMIT_NO_PENDING_TURN` when nothing is pending (not your
            turn, match ended, or the bridge already fell back on timeout), or
            :data:`SUBMIT_STALE_TURN` when ``request_id`` names a turn that is
            no longer the pending one.
        """
        with self._lock:
            record = self._matches.get(match_id)
            if record is None or record.pending is None:
                return SUBMIT_NO_PENDING_TURN
            pending = record.pending
            if request_id is not None and pending.snapshot.request_id != request_id:
                return SUBMIT_STALE_TURN
            record.pending = None
        pending.action = action
        pending.event.set()
        return SUBMIT_ACCEPTED

    def pending_request_id(self, match_id: str) -> str | None:
        """``request_id`` of the turn currently pending on ``match_id``, if any."""
        with self._lock:
            record = self._matches.get(match_id)
            if record is None or record.pending is None:
                return None
            return record.pending.snapshot.request_id

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
        """Latest round/match result -- for ``match_id``, or the most recent
        across all matches.

        With no ``match_id`` this returns the result that landed most recently
        (by :attr:`_MatchRecord.result_seq`), NOT the first match in insertion
        order -- so a multi-tabling agent reads the outcome it actually just
        produced (chipzen-ai/Chipzen#3884).

        When ``match_id`` names a match the registry has never seen, this
        returns ``None`` (surfaced as ``no_results_yet``) rather than silently
        falling back to some other match's result -- a typo'd id must never be
        answered with a different match's data.

        Consumption eviction (chipzen-ai/Chipzen#3939): an EXPLICIT read of a
        FINISHED match collects its terminal outcome, so the record is dropped
        right after it is returned -- a long-lived multi-tabling agent releases
        each finished match as soon as it has read the result, instead of
        waiting for the :data:`MAX_FINISHED_MATCHES` cap sweep. The no-``match_id``
        "most recent" peek NEVER evicts: repeated peeks stay stable and keep
        answering the latest result (the #3884 recency contract).
        """
        with self._lock:
            if match_id is not None:
                record = self._matches.get(match_id)
                if record is None:
                    return None
                candidates: list[_MatchRecord] = [record]
            else:
                candidates = list(self._matches.values())
            best: _MatchRecord | None = None
            for record in candidates:
                if record.match_end is None and record.last_round_result is None:
                    continue
                if best is None or record.result_seq > best.result_seq:
                    best = record
            if best is None:
                return None
            result = {
                "match_id": best.match_id,
                "match_end": best.match_end,
                "last_round_result": best.last_round_result,
            }
            if match_id is not None and best.match_end is not None:
                # Terminal outcome consumed via an explicit read -> evict now.
                self._matches.pop(best.match_id, None)
            return result

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
        # Surface the announced clock on the match record too, so
        # list_matches / get_match_state show what clock each match runs
        # under (~30s for the casual agent division, 2s rated).
        self._registry.match_started(self._match_id, turn_timeout_ms=self._turn_timeout_ms)

    def on_reconnected(self, message: dict) -> None:
        """Re-learn match identity from a ``reconnected`` frame (chipzen-ai/chipzen-sdk#119).

        On re-attach to an in-flight match the server sends ``reconnected``,
        never ``match_start`` -- so a fresh BridgeBot would keep
        ``_match_id == ""`` and publish every subsequent turn under an empty
        match record, corrupting turn routing for any multi-match client.
        Populate the same fields :meth:`on_match_start` does, from the same
        envelope shape.
        """
        match_id = str(message.get("match_id", ""))
        if match_id:
            self._match_id = match_id
        # ``reconnected`` is not documented to carry ``turn_timeout_ms``, but
        # honour it when present (additionalProperties: true); otherwise keep
        # the current clock (the default, or whatever match_start announced).
        announced = message.get("turn_timeout_ms")
        if isinstance(announced, (int, float)) and announced > 0:
            self._turn_timeout_ms = int(announced)
        if self._match_id:
            self._registry.match_started(self._match_id, turn_timeout_ms=self._turn_timeout_ms)

    def on_round_result(self, message: dict) -> None:
        result = dict(message.get("result", {}) or {})
        self._registry.record_round_result(self._match_id, result)

    def on_match_end(self, results: dict) -> None:
        self._registry.record_match_end(self._match_id, results)

    # -- the decision itself ------------------------------------------------

    @staticmethod
    def _fallback(state: GameState) -> Action:
        """The action the server would auto-apply for us: check if legal, else fold."""
        return Action.check() if "check" in state.valid_actions else Action.fold()

    def decide(self, state: GameState) -> Action:
        """Publish the turn, then block until ``act`` answers or time runs out.

        On local timeout the bridge plays the same fallback the server
        would auto-apply anyway (``check`` if legal, else ``fold``) -- but
        does it EARLY enough to stay inside the clock, and logs loudly so a
        consistently-slow agent is visible rather than silently folding.

        Shutdown is a third exit (chipzen-ai/Chipzen#3900): when
        :meth:`TurnRegistry.close` cancels the pending turn this returns the
        same fallback IMMEDIATELY rather than parking the SDK session thread
        for the rest of the budget -- which is what used to make process exit
        linger to ``ExternalSession.stop``'s ~10s cap. Logged at INFO, not
        WARNING: a host that went away is not a slow agent.
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

        if pending.event.wait(timeout=budget_ms / 1000.0):
            if pending.cancelled:
                fallback = self._fallback(state)
                logger.info(
                    "match %s: session shutting down mid-turn; playing %s and returning now",
                    self._match_id,
                    fallback.action,
                )
                return fallback
            if pending.action is not None:
                return pending.action

        self._registry.clear_pending(self._match_id)
        fallback = self._fallback(state)
        logger.warning(
            "match %s: no act() within %dms budget; falling back to %s",
            self._match_id,
            budget_ms,
            fallback.action,
        )
        return fallback


# ---------------------------------------------------------------------------
# Lobby presence + reconnect visibility (derived from SDK log events)
# ---------------------------------------------------------------------------

#: Lobby-presence states surfaced by ``get_status``.
LOBBY_STARTING = "starting"  # session thread up, lobby handshake not seen yet
LOBBY_CONNECTED = "connected"  # lobby `hello` received; matches can dispatch
LOBBY_RECONNECTING = "reconnecting"  # lobby dropped; SDK is retrying
LOBBY_EVICTED = "evicted"  # replaced by a newer connection (terminal)
LOBBY_ENDED = "ended"  # session over (stop, budget exhausted, or clean end)

#: The SDK logger the tap listens to (all lobby/gateway lifecycle events).
SDK_LOGGER_NAME = "chipzen.external"


class SessionPresence:
    """Thread-safe lobby-presence state, written by the log tap.

    ``connected`` answers "can the platform dispatch a match to me right
    now"; ``snapshot()`` is folded into ``get_status`` so the agent can tell
    connected / reconnecting / evicted apart instead of guessing from thread
    liveness.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = LOBBY_STARTING
        self._detail = ""
        self._since = time.time()

    def transition(self, state: str, detail: str = "") -> None:
        with self._lock:
            if self._state == state and self._detail == detail:
                return
            self._state = state
            self._detail = detail
            self._since = time.time()

    def session_over(self, detail: str) -> None:
        """Mark the session ended. Idempotent: the FIRST terminal verdict
        (``evicted``, or the first ``ended`` detail) wins; later generic
        teardown calls don't overwrite it."""
        with self._lock:
            if self._state in (LOBBY_EVICTED, LOBBY_ENDED):
                return
            self._state = LOBBY_ENDED
            self._detail = detail
            self._since = time.time()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._state == LOBBY_CONNECTED

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "lobby_state": self._state,
                "lobby_detail": self._detail,
                "lobby_state_since": self._since,
            }


class SdkLogTap(logging.Handler):
    """Derive lobby presence + per-match reconnect state from SDK log events.

    The SDK's public surface is :class:`chipzen.Bot` -- it deliberately has no
    lobby/connection lifecycle hooks, and reimplementing the protocol here
    just to observe it would fork battle-tested code. Instead this handler
    listens on the ``chipzen.external`` logger and routes on ``record.msg``
    (the pre-format TEMPLATE, a stable identity -- not the rendered text).

    Template drift is the obvious risk, so the templates are pinned two ways:
    ``tests/test_bridge.py`` asserts every REQUIRED template appears verbatim
    in the installed SDK's source (fails loudly on an SDK release that
    rewords them), and OPTIONAL templates cover events newer SDKs emit
    (mid-match gateway reconnect, added after chipzen-bot 0.3.0) -- with an
    older SDK those simply never fire and match ``connection`` stays at its
    last known state.
    """

    def __init__(self, registry: TurnRegistry, presence: SessionPresence) -> None:
        super().__init__(level=logging.DEBUG)
        self._registry = registry
        self._presence = presence

    # -- template routes ----------------------------------------------------
    # Emitted by every supported SDK (chipzen-bot >= 0.3.0).
    T_LOBBY_CONNECTING = "external: connecting lobby %s"
    T_LOBBY_CONNECTED = "lobby: connected (endpoint=%s)"
    T_LOBBY_CLOSED = "lobby: connection closed"
    T_LOBBY_RETRYING = "lobby: closed; reconnecting in %.1fs"
    T_LOBBY_CONNECT_FAILED = "lobby: connect failed (%s); retrying in %.1fs (attempt %d/%d)"
    T_LOBBY_EVICTED = "lobby: evicted (replaced by a newer connection)"
    T_LOBBY_GAVE_UP = "lobby: closed and reconnect budget exhausted; done"
    T_SESSION_ENDED = "external: session ended (%d match(es) played)"
    T_MATCHED = "lobby: matched -> match %s (rated=%s); playing"
    T_GATEWAY_CONNECTING = "match %s: connecting gateway"
    # Emitted by SDKs newer than the published 0.3.0 wheel (mid-match
    # gateway reconnect); absent templates simply never fire.
    T_MATCH_RECONNECTING = "match %s: reconnecting in %.1fs (attempt %d/%d; %s)"
    T_MATCH_ABANDONED = "match %s: reconnect budget exhausted (%s); abandoning"

    REQUIRED_TEMPLATES: tuple[str, ...] = (
        T_LOBBY_CONNECTING,
        T_LOBBY_CONNECTED,
        T_LOBBY_CLOSED,
        T_LOBBY_RETRYING,
        T_LOBBY_CONNECT_FAILED,
        T_LOBBY_EVICTED,
        T_LOBBY_GAVE_UP,
        T_SESSION_ENDED,
        T_MATCHED,
        T_GATEWAY_CONNECTING,
    )
    OPTIONAL_TEMPLATES: tuple[str, ...] = (
        T_MATCH_RECONNECTING,
        T_MATCH_ABANDONED,
    )

    def emit(self, record: logging.LogRecord) -> None:  # noqa: C901 - flat dispatch
        try:
            template = record.msg if isinstance(record.msg, str) else ""
            args: tuple[Any, ...] = record.args if isinstance(record.args, tuple) else ()
            presence = self._presence
            if template == self.T_LOBBY_CONNECTED:
                presence.transition(LOBBY_CONNECTED)
            elif template == self.T_LOBBY_CONNECTING:
                presence.transition(LOBBY_STARTING, "connecting")
            elif template == self.T_LOBBY_CLOSED:
                presence.transition(LOBBY_RECONNECTING, "lobby connection closed")
            elif template == self.T_LOBBY_RETRYING:
                presence.transition(LOBBY_RECONNECTING, "retrying")
            elif template == self.T_LOBBY_CONNECT_FAILED:
                attempt = f"attempt {args[2]}/{args[3]}" if len(args) >= 4 else "retrying"
                presence.transition(LOBBY_RECONNECTING, attempt)
            elif template == self.T_LOBBY_EVICTED:
                presence.transition(LOBBY_EVICTED, "replaced by a newer connection")
            elif template == self.T_LOBBY_GAVE_UP:
                presence.session_over("lobby reconnect budget exhausted")
            elif template == self.T_SESSION_ENDED:
                presence.session_over("session ended")
            elif template == self.T_MATCHED and args:
                rated = args[1] if len(args) >= 2 and isinstance(args[1], bool) else None
                self._registry.match_started(str(args[0]), rated=rated)
            elif template == self.T_GATEWAY_CONNECTING and args:
                self._registry.set_match_connection(str(args[0]), MATCH_CONN_CONNECTED)
            elif template == self.T_MATCH_RECONNECTING and args:
                attempt_n = int(args[2]) if len(args) >= 3 else None
                self._registry.set_match_connection(
                    str(args[0]), MATCH_CONN_RECONNECTING, attempt=attempt_n
                )
            elif template == self.T_MATCH_ABANDONED and args:
                self._registry.set_match_connection(str(args[0]), MATCH_CONN_ABANDONED)
        except Exception:  # noqa: BLE001 - a tap must never break SDK logging
            self.handleError(record)


# ---------------------------------------------------------------------------
# The background SDK session
# ---------------------------------------------------------------------------

#: How long ``stop()`` lets in-flight work unwind inside the session loop
#: before cancelling stragglers (mirrors the SDK's own teardown grace).
DEFAULT_STOP_DRAIN_GRACE_S = 5.0


class ExternalSession:
    """Background thread running the SDK's External-API session.

    Owns a thread that runs ``chipzen.run_external_bot`` with a
    :class:`BridgeBot`-per-match factory on its own event loop. Starting the
    session is what puts the bot "online" in the lobby so the platform can
    dispatch matches to it.

    Lifecycle:

    * :meth:`start` spins the thread up (idempotent) and installs the
      :class:`SdkLogTap` so lobby presence / reconnect state stay truthful.
    * :meth:`stop` is a cooperative, thread-safe shutdown: it cancels the
      session task on the session loop, gives in-flight match tasks a short
      grace to unwind (sockets close cleanly), then joins the thread. Called
      from ``main()`` when the MCP transport closes, and safe to call twice.
    * ``run_external_bot`` returning on its own (lobby closed for good,
      eviction, fatal error) just ends the thread; ``error`` carries any
      exception and presence reports the terminal state.
    """

    def __init__(
        self,
        config: McpConfig,
        registry: TurnRegistry,
        *,
        runner: Callable[[], Coroutine[Any, Any, Any]] | None = None,
        drain_grace_s: float = DEFAULT_STOP_DRAIN_GRACE_S,
    ) -> None:
        """``runner`` overrides the session coroutine (tests inject a fake
        instead of monkeypatching the SDK); ``None`` runs the real
        ``chipzen.run_external_bot``."""
        self._config = config
        self._registry = registry
        self._runner = runner
        self._drain_grace_s = drain_grace_s
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._presence = SessionPresence()
        self._tap = SdkLogTap(registry, self._presence)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop_ready = threading.Event()
        self._stop_requested = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the session thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_requested.clear()
        self._loop_ready.clear()
        self._error = None
        # A previous stop() closed the registry; put it back in service.
        self._registry.reopen()
        self._presence.transition(LOBBY_STARTING, "session thread starting")
        sdk_logger = logging.getLogger(SDK_LOGGER_NAME)
        # The tap feeds on INFO-level SDK events; an embedding app that never
        # configured logging would leave the effective level at WARNING and
        # silently blind lobby presence. Opening the logger (not the root)
        # is the minimal intervention.
        if sdk_logger.getEffectiveLevel() > logging.INFO:
            sdk_logger.setLevel(logging.INFO)
        sdk_logger.addHandler(self._tap)
        self._thread = threading.Thread(target=self._run, name="chipzen-mcp-session", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Cooperatively stop the session and join the thread.

        Cancels the session task on ITS loop (thread-safe), which closes the
        lobby/gateway sockets and lets :meth:`_main` drain in-flight tasks
        for up to ``drain_grace_s`` before cancelling them. Idempotent; safe
        to call when the session never started or already finished.

        First, though, it :meth:`TurnRegistry.close`\\ s the registry
        (chipzen-ai/Chipzen#3900). A ``decide()`` that is blocking the session
        thread on a pending turn cannot be interrupted by cancelling the
        asyncio task -- so cancelling the turn is what lets that thread reach
        the loop's cancellation and join promptly, instead of the join timing
        out at ``timeout`` and the daemon thread being abandoned (the ~10s
        linger + "still winding down at exit" warning).

        Returns:
            ``True`` when the thread has exited within ``timeout`` seconds
            (or was never running); ``False`` if it is still winding down --
            it is a daemon thread either way, so process exit is never held
            hostage.
        """
        self._stop_requested.set()
        thread = self._thread
        stopped = True
        if thread is not None and thread.is_alive():
            # Unblock a decide() that is parked on a pending turn BEFORE
            # joining -- otherwise the session thread cannot notice the
            # cancellation until its decision budget expires (#3900).
            cancelled = self._registry.close()
            if cancelled:
                logger.info("stop: cancelled %d pending turn(s) mid-shutdown", cancelled)
            # Wait for the loop to exist (start() may have just been called),
            # then set the stop event ON the session loop.
            if self._loop_ready.wait(timeout=min(timeout, 5.0)):
                loop, stop_event = self._loop, self._stop_event
                if loop is not None and stop_event is not None:
                    with contextlib.suppress(RuntimeError):  # loop already closed
                        loop.call_soon_threadsafe(stop_event.set)
            thread.join(timeout)
            stopped = not thread.is_alive()
        if stopped:
            self._presence.session_over("stopped")
        logging.getLogger(SDK_LOGGER_NAME).removeHandler(self._tap)
        return stopped

    # -- session thread ------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._loop_ready.set()
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001 - surfaced via .error
            logger.exception("external session thread died")
            self._error = exc
            self._presence.session_over(f"error: {exc}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            asyncio.set_event_loop(None)
            loop.close()
            self._presence.session_over("session thread exited")

    async def _main(self) -> None:
        """Run the session, racing it against the cooperative stop signal."""
        assert self._stop_event is not None
        if self._stop_requested.is_set():  # stop() won the race with start()
            return
        session_task = asyncio.ensure_future(self._session_coro())
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        done, _ = await asyncio.wait({session_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if session_task in done:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            session_task.result()  # propagate a session error to _run
            return
        # Cooperative stop: cancel the session (closes lobby + gateway
        # sockets), then give any straggler tasks a grace window to unwind
        # before cancelling them -- mirroring the SDK's own teardown.
        session_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session_task
        await self._drain_stragglers()

    async def _drain_stragglers(self) -> None:
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=self._drain_grace_s)
        for task in still_pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _session_coro(self) -> Coroutine[Any, Any, Any]:
        if self._runner is not None:
            return self._runner()
        from chipzen import run_external_bot

        return run_external_bot(
            lambda: BridgeBot(self._registry),
            bot_id=self._config.bot_id or None,
            env=self._config.env,  # type: ignore[arg-type]
            url=self._config.lobby_url,
            token=self._config.token,
            client_name="chipzen-mcp",
        )

    # -- state surfaced to get_status ------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def lobby_connected(self) -> bool:
        """Truthful lobby presence: thread alive AND lobby handshake seen."""
        return self.running and self._presence.connected

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def presence_snapshot(self) -> dict[str, Any]:
        """Lobby-presence view folded into ``get_status``."""
        snap = self._presence.snapshot()
        snap["lobby_connected"] = self.lobby_connected
        return snap

    @property
    def error(self) -> BaseException | None:
        """The exception that killed the session thread, if any."""
        return self._error
