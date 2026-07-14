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
        # Surface the announced clock on the match record too, so
        # list_matches / get_match_state show what clock each match runs
        # under (~30s for the casual agent division, 2s rated).
        self._registry.match_started(self._match_id, turn_timeout_ms=self._turn_timeout_ms)

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
