"""A chess bot that plays through a UCI engine you supply. PREVIEW: chess is not live.

:class:`UciBridgeBot` is a :class:`chipzen.ChessBot` that starts a UCI chess
engine (a binary you put in your bot image; none is bundled), and for each
request sends it the game so far and a time limit, then answers with the
engine's ``bestmove``. Nothing here knows the rules of chess: the platform
lists the legal moves, and the bridge only checks the engine's answer against
that list.

The mapping (game-interface spec 6.5.3):

======================  ==============================================================
Step                    UCI
======================  ==============================================================
start                   ``uci`` (wait for ``uciok``), each ``setoption``, ``isready``
new match               ``ucinewgame``, ``isready``
each request            ``position fen <start_fen> moves <moves...>``, then
                        :meth:`~UciBridgeBot.go_command` (default ``go movetime <ms>``)
answer                  ``bestmove e2e4`` becomes ``{"action": "move", "params": {"move": "e2e4"}}``
match end               ``quit``
======================  ==============================================================

**Fallback.** When the engine answers a move that is not in
``state.legal_moves``, answers ``bestmove (none)``, takes longer than its
deadline, or has died, the bridge plays :meth:`~chipzen.ChessBot.fallback_move`
(a move from the list) and logs a warning. A late ``bestmove`` is discarded
before the next search, and a dead engine is restarted at the next request.

Configure it with class attributes, or the environment:

- :attr:`~UciBridgeBot.engine_path` (or ``CHIPZEN_UCI_ENGINE``): the engine
  binary, and any arguments in :attr:`~UciBridgeBot.engine_args`;
- :attr:`~UciBridgeBot.uci_options`: ``setoption`` values, e.g.
  ``{"Threads": 1, "Hash": 64}``.

Example::

    from chipzen.chess_uci import UciBridgeBot

    class MyEngine(UciBridgeBot):
        engine_path = "/bot/engine/my-engine"
        uci_options = {"Threads": 1, "Hash": 64}
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from chipzen.chess import ChessState
from chipzen.chess_bot import ChessBot

logger = logging.getLogger("chipzen")

#: The environment variable that overrides :attr:`UciBridgeBot.engine_path`.
ENGINE_ENV = "CHIPZEN_UCI_ENGINE"

#: Longest line read from the engine. Longer lines are cut (an ``info`` line
#: can be long; a ``bestmove`` line never is).
MAX_LINE = 4096


class UciEngineError(RuntimeError):
    """The engine could not be started, or stopped answering the handshake."""


class UciEngine:
    """A UCI engine process: write commands, read lines with a deadline.

    A reader thread copies the engine's stdout into a queue, so every read can
    time out on any platform. Not thread-safe; one caller at a time.
    """

    def __init__(self, command: Sequence[str]) -> None:
        self.command = list(command)
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._eof = False

    @property
    def alive(self) -> bool:
        """Whether the engine process is running and its output is still open."""
        return self._proc is not None and not self._eof and self._proc.poll() is None

    def start(self) -> None:
        """Start the process.

        Raises:
            UciEngineError: the binary is missing or cannot be run.
        """
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise UciEngineError(f"cannot start the UCI engine {self.command!r}: {exc}") from exc
        self._lines = queue.Queue()
        self._eof = False
        threading.Thread(
            target=self._read, args=(self._proc, self._lines), name="uci-reader", daemon=True
        ).start()

    @staticmethod
    def _read(proc: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
        stdout = proc.stdout
        if stdout is None:  # pragma: no cover - always a pipe here
            lines.put(None)
            return
        try:
            for line in stdout:
                lines.put(line[:MAX_LINE].strip())
        except (OSError, ValueError):  # pragma: no cover - the pipe closed under us
            pass
        lines.put(None)

    def send(self, line: str) -> bool:
        """Write one command. ``False`` if the engine is gone."""
        if self._proc is None or self._proc.stdin is None or not self.alive:
            return False
        try:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def read_until(self, prefix: str, deadline: float) -> str | None:
        """Read lines until one starts with ``prefix``; return it.

        ``None`` when ``deadline`` (a :func:`time.monotonic` value) passes or
        the engine's output ends first. Lines before it are discarded.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                self._eof = True
                self._lines.put(None)  # stay at end of output for later reads
                return None
            if line == prefix or line.startswith(prefix + " "):
                return line

    def close(self, grace_s: float = 0.5) -> None:
        """Send ``quit`` and stop the process."""
        proc = self._proc
        if proc is None:
            return
        self.send("quit")
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover
                    pass
        self._proc = None


class UciBridgeBot(ChessBot):
    """A :class:`~chipzen.ChessBot` that plays the moves of a UCI engine you supply.

    Preview: chess is not live on Chipzen yet. Override :meth:`movetime_ms`
    or :meth:`go_command` to change how time is spent.
    """

    #: The engine binary. ``CHIPZEN_UCI_ENGINE`` in the environment overrides it.
    engine_path: ClassVar[str | None] = None

    #: Extra command-line arguments for the engine.
    engine_args: ClassVar[Sequence[str]] = ()

    #: ``setoption name <key> value <value>`` for each entry, sent at start-up.
    uci_options: ClassVar[Mapping[str, Any]] = {}

    #: How long the engine may take to answer ``uci`` and ``isready`` at start-up.
    startup_timeout_ms: ClassVar[int] = 10_000

    #: Time kept back from every search, on top of :attr:`send_margin_ms`, for
    #: the engine to print ``bestmove`` and the bridge to read it.
    engine_margin_ms: ClassVar[int] = 100

    #: Shortest search ever asked for.
    min_movetime_ms: ClassVar[int] = 20

    #: The bank is spent as if this many more moves were to come.
    moves_to_go: ClassVar[int] = 30

    #: After asking a late engine to ``stop``, how long to wait for its ``bestmove``.
    stop_grace_ms: ClassVar[int] = 50

    def __init__(self) -> None:
        self._engine: UciEngine | None = None
        self._stale_search = False
        # Held while the engine is in use: by the warm-up thread at match
        # start and by choose_move (which runs on a worker thread).
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def engine_command(self) -> list[str]:
        """The command that starts the engine.

        Raises:
            UciEngineError: no engine is configured.
        """
        path = os.environ.get(ENGINE_ENV) or self.engine_path
        if not path:
            raise UciEngineError(
                f"no UCI engine configured: set {type(self).__name__}.engine_path or {ENGINE_ENV}"
            )
        return [path, *self.engine_args]

    def movetime_ms(self, state: ChessState) -> int:
        """How long the engine may search this move, in ms.

        The free window plus an even share of the bank, less the margins, and
        never more than the request allows.
        """
        clock = state.clock
        share = clock.my_bank_ms // max(self.moves_to_go, 1)
        spend = clock.window_ms + share - self.send_margin_ms - self.engine_margin_ms
        ceiling = state.timeout_ms - self.send_margin_ms - self.engine_margin_ms
        return max(self.min_movetime_ms, min(spend, ceiling))

    def go_command(self, state: ChessState) -> str:
        """The ``go`` command for this move. Default: ``go movetime <movetime_ms>``.

        To let the engine manage its own time instead, return the clock form
        of the spec's mapping, e.g. ``go wtime <ms> btime <ms> winc <window>
        binc <window>``, with White's and Black's banks taken from
        ``state.clock.my_bank_ms`` / ``opp_bank_ms`` by ``state.side``.
        """
        return f"go movetime {self.movetime_ms(state)}"

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    def _ensure_engine(self) -> UciEngine:
        engine = self._engine
        if engine is not None and engine.alive:
            return engine
        if engine is not None:
            logger.warning("the UCI engine stopped; restarting it")
            engine.close()
        engine = UciEngine(self.engine_command())
        engine.start()
        deadline = time.monotonic() + self.startup_timeout_ms / 1000.0
        engine.send("uci")
        if engine.read_until("uciok", deadline) is None:
            engine.close()
            raise UciEngineError("the UCI engine did not answer 'uci' with 'uciok'")
        for name, value in self.uci_options.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            engine.send(f"setoption name {name} value {value}")
        engine.send("ucinewgame")
        engine.send("isready")
        if engine.read_until("readyok", deadline) is None:
            engine.close()
            raise UciEngineError("the UCI engine did not answer 'isready' with 'readyok'")
        self._engine = engine
        self._stale_search = False
        return engine

    def close_engine(self) -> None:
        """Stop the engine process (``quit``). The next move starts it again."""
        if self._engine is not None:
            self._engine.close()
            self._engine = None

    def on_match_start(self, match_info: dict) -> None:
        """Start the engine in the background, so the first move does not pay for it.

        Runs on a thread: the session loop must keep answering the server
        while a large engine loads.
        """
        super().on_match_start(match_info)
        threading.Thread(target=self._warm_up, name="uci-warm-up", daemon=True).start()

    def _warm_up(self) -> None:
        with self._lock:
            try:
                engine = self._ensure_engine()
            except UciEngineError as exc:
                logger.error("%s; moves will be the fallback until it starts", exc)
                return
            engine.send("ucinewgame")
            engine.send("isready")
            engine.read_until("readyok", time.monotonic() + self.startup_timeout_ms / 1000.0)

    def on_match_end(self, results: dict) -> None:
        """Stop the engine."""
        self.close_engine()

    # ------------------------------------------------------------------
    # One move
    # ------------------------------------------------------------------

    def choose_move(self, state: ChessState) -> str:
        """Ask the engine for its move; fall back to a listed move on any problem."""
        started = time.monotonic()
        # The bridge's own deadline ends before the SDK's (timeout_ms less
        # send_margin_ms), so the bridge always answers first.
        deadline = started + max(state.timeout_ms - self.send_margin_ms, 1) / 1000.0
        if not self._lock.acquire(timeout=max(deadline - time.monotonic(), 0.001)):
            logger.warning("the UCI engine is still starting; playing the fallback move")
            return self.fallback_move(state)
        try:
            return self._search(state, started, deadline)
        finally:
            self._lock.release()

    def _search(self, state: ChessState, started: float, deadline: float) -> str:
        try:
            engine = self._ensure_engine()
        except UciEngineError as exc:
            logger.error("%s; playing the fallback move", exc)
            return self.fallback_move(state)

        if self._stale_search:
            # A search from an earlier request may still be printing. Wait for
            # it to finish, and drop its late bestmove.
            engine.send("stop")
            engine.send("isready")
            if engine.read_until("readyok", deadline) is None:
                logger.warning("the UCI engine is not ready; restarting it next move")
                self.close_engine()
                return self.fallback_move(state)
            self._stale_search = False

        position = f"position fen {state.start_fen}"
        if state.moves:
            position += " moves " + " ".join(state.moves)
        go = self.go_command(state)
        if not (engine.send(position) and engine.send(go)):
            logger.warning("the UCI engine is gone; playing the fallback move")
            self.close_engine()
            return self.fallback_move(state)

        search_deadline = min(
            deadline - self.engine_margin_ms / 1000.0,
            started + (self.movetime_ms(state) + self.engine_margin_ms) / 1000.0,
        )
        line = engine.read_until("bestmove", max(search_deadline, started + 0.001))
        if line is None and engine.alive:
            engine.send("stop")
            line = engine.read_until(
                "bestmove", min(deadline, time.monotonic() + self.stop_grace_ms / 1000.0)
            )
            if line is None:
                self._stale_search = True
        if line is None:
            logger.warning(
                "the UCI engine gave no bestmove in time (%s); playing the fallback move", go
            )
            return self.fallback_move(state)

        parts = line.split()
        move = parts[1] if len(parts) > 1 else ""
        if move not in state.legal_moves:
            logger.warning(
                "the UCI engine answered %r, which is not one of the legal moves; "
                "playing the fallback move",
                line,
            )
            return self.fallback_move(state)
        return move


__all__ = ["ENGINE_ENV", "UciBridgeBot", "UciEngine", "UciEngineError"]
