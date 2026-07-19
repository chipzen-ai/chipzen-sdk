"""Windows-resilient stdio transport for the Chipzen MCP server.

Works around chipzen-ai/Chipzen#3888: on **Windows**, the stdio transport the
MCP SDK ships (``mcp.server.stdio.stdio_server`` over anyio) can wedge after a
burst of hostile frames — a malformed (non-JSON) line, an oversized (~2 MB)
argument frame, then an unknown JSON-RPC method. Once wedged the server stops
answering *and does not exit when stdin closes*, leaving a lingering child
process. The identical harness is clean on Linux, so the root cause is in the
bundled Windows stdio reader (blocking ``TextIOWrapper`` readline pumped
through anyio's non-cancellable worker-thread iterator), not in chipzen-mcp's
own tool code — but chipzen-mcp ships that transport and lists Windows MCP
hosts (Claude Desktop / Claude Code) as a target, so we defend at our layer.

This module re-implements the stdio transport with three defenses we fully
own:

* **We own the stdin reader.** Frames are read on a dedicated worker thread
  reading ``sys.stdin.buffer`` in binary and splitting lines ourselves, so
  EOF (the host closed the pipe) is observed directly by our code and always
  tears the server down — the deterministic exit path the upstream reader
  loses under the wedge.
* **Frame-size guard.** A line longer than ``max_frame_bytes`` is drained and
  rejected with a JSON-RPC parse error *before* it reaches the upstream
  pydantic parser — the oversized frame that appears to tip the Windows reader
  over never gets there. Legitimate inbound frames (tool calls) are tiny, so
  the 1 MiB default never touches real traffic.
* **stdin-EOF watchdog.** A daemon thread armed on EOF force-exits the process
  if graceful teardown has not completed within a short grace window — a
  last-resort guarantee that closing stdin never leaves a lingering process,
  even if the async run loop itself wedges.

The message contract on the wire is byte-for-byte identical to the upstream
transport (newline-delimited JSON-RPC; parse failures surfaced as exceptions
the session turns into error responses), so nothing downstream changes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from typing import BinaryIO, cast

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

logger = logging.getLogger("chipzen_mcp.stdio_guard")

#: Largest inbound frame we hand to the parser. Inbound frames are agent tool
#: calls (a match id, an action, an amount) — kilobytes at most — so this is
#: generous headroom for real traffic while rejecting the ~2 MB abuse frame in
#: chipzen-ai/Chipzen#3888 before it reaches the upstream reader.
DEFAULT_MAX_FRAME_BYTES = 1024 * 1024

#: Grace after stdin EOF before the watchdog force-exits. Normal teardown
#: (transport unwinds, ``session.stop()`` runs) completes in well under this;
#: the watchdog only fires if the run loop itself wedged. Overridable via
#: ``CHIPZEN_MCP_STDIN_EOF_GRACE`` (seconds).
DEFAULT_EOF_GRACE_S = 5.0
ENV_EOF_GRACE = "CHIPZEN_MCP_STDIN_EOF_GRACE"


def _resolve_eof_grace(default: float = DEFAULT_EOF_GRACE_S) -> float:
    raw = os.environ.get(ENV_EOF_GRACE, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: Exit code used by the watchdog force-exit, so a lingering-process kill is
#: distinguishable from a clean exit in logs/telemetry.
WATCHDOG_EXIT_CODE = 3888

#: Inbound read-stream buffer between the async drain and the server run loop.
#: EOF detection no longer depends on it (the dedicated reader thread owns
#: that), but a small buffer keeps the drain from lock-stepping with the run
#: loop frame-by-frame. Frames are size-capped and tiny, so it is negligible
#: memory.
READ_STREAM_BUFFER = 64


class _OversizeFrame:
    """Sentinel: a line exceeded ``max_frame_bytes`` and was drained."""


_OVERSIZE = _OversizeFrame()


def _read_capped_line(buf: BinaryIO, cap: int) -> bytes | None | _OversizeFrame:
    """Read one newline-delimited frame, bounding memory at ``cap`` bytes.

    Returns the raw line (including any trailing newline) for a normal frame,
    ``None`` at EOF, or :data:`_OVERSIZE` when the line exceeded ``cap`` (its
    remainder is drained up to the next newline so the stream stays aligned).
    """
    line = buf.readline(cap + 1)
    if line == b"":
        return None
    if len(line) > cap and not line.endswith(b"\n"):
        # Oversized: discard the rest of this line so the next read starts on
        # a fresh frame boundary rather than mid-garbage.
        while True:
            extra = buf.readline(cap + 1)
            if extra == b"" or extra.endswith(b"\n"):
                break
        return _OVERSIZE
    return line


#: Queue sentinel signalling stdin EOF to the async drain.
_EOF = object()


def _blocking_stdin_reader(
    q: queue.Queue[SessionMessage | Exception | object],
    stdin: BinaryIO,
    max_frame_bytes: int,
    eof_event: threading.Event,
) -> None:
    """Read + parse frames on a dedicated OS thread; enqueue for the drain.

    This thread touches *no async machinery* — it only blocks on ``readline``
    and puts results on a plain queue. That independence is the whole point:
    the chipzen-ai/Chipzen#3888 wedge stalls the event loop, so a reader that
    had to bridge back into it (portal / anyio worker) would block forever and
    never observe EOF. Here EOF is a bare ``readline`` returning empty, which
    always happens when the host closes stdin — so :data:`eof_event` is set and
    the watchdog arms regardless of the loop's state.
    """
    try:
        while True:
            line = _read_capped_line(stdin, max_frame_bytes)
            if line is None:  # EOF: host closed stdin.
                return
            if isinstance(line, _OversizeFrame):
                q.put(ValueError(f"inbound frame exceeds {max_frame_bytes} bytes; rejected"))
                continue
            try:
                message = types.JSONRPCMessage.model_validate_json(line)
            except Exception as exc:  # noqa: BLE001 - surfaced as an error response
                q.put(exc)
                continue
            q.put(SessionMessage(message))
    finally:
        eof_event.set()
        q.put(_EOF)


def _make_streams() -> tuple[
    MemoryObjectSendStream[SessionMessage | Exception],
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
    MemoryObjectReceiveStream[SessionMessage],
]:
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        READ_STREAM_BUFFER
    )
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)
    return read_writer, read_stream, write_stream, write_reader


def _best_effort_warn(grace_s: float) -> None:
    """Emit the watchdog warning off the exit path. Kept in its own daemon
    thread because ``stderr`` may itself be a full/blocked pipe — a contributor
    to the wedge — and writing to it must never delay the force-exit."""
    with contextlib.suppress(Exception):
        logger.warning(
            "chipzen-mcp: stdin closed but transport did not exit within %.1fs; "
            "forcing exit (chipzen-ai/Chipzen#3888 watchdog)",
            grace_s,
        )


def _force_exit(grace_s: float) -> None:
    """Force-terminate the process after a wedge. No blocking I/O on this path:
    the wedge can leave the event loop *and* stderr stuck, so we log only via a
    fire-and-forget daemon thread, then ``os._exit`` unconditionally — the one
    call that always terminates the process regardless of loop/pipe state."""
    threading.Thread(target=_best_effort_warn, args=(grace_s,), daemon=True).start()
    # Give the best-effort log a brief chance, but never wait on it.
    threading.Event().wait(0.1)
    os._exit(WATCHDOG_EXIT_CODE)


def _start_watchdog(
    eof_event: threading.Event,
    grace_s: float,
    *,
    on_fire: Callable[[float], None] = _force_exit,
) -> threading.Event:
    """Arm the stdin-EOF force-exit watchdog. Returns a 'disarm' event the
    caller sets once the transport has torn down cleanly.

    When stdin EOF is observed but the transport has not disarmed within
    ``grace_s`` (the run loop wedged — chipzen-ai/Chipzen#3888), ``on_fire`` is
    called to terminate the process, guaranteeing stdin-close never leaves a
    lingering child. ``on_fire`` is injectable so the arm/disarm timing can be
    tested without actually exiting.
    """
    done = threading.Event()

    def _watch() -> None:
        eof_event.wait()
        if done.wait(grace_s):
            return  # clean teardown won the race
        on_fire(grace_s)

    threading.Thread(target=_watch, name="chipzen-mcp-stdin-watchdog", daemon=True).start()
    return done


async def _serve(
    server: FastMCP,
    max_frame_bytes: int,
    eof_event: threading.Event,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    read_writer, read_stream, write_stream, write_reader = _make_streams()

    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout

    async def stdout_writer() -> None:
        try:
            async with write_reader:
                async for session_message in write_reader:
                    data = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    await anyio.to_thread.run_sync(stdout.write, (data + "\n").encode("utf-8"))
                    await anyio.to_thread.run_sync(stdout.flush)
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    q: queue.Queue[SessionMessage | Exception | object] = queue.Queue()
    reader_thread = threading.Thread(
        target=_blocking_stdin_reader,
        args=(q, stdin, max_frame_bytes, eof_event),
        name="chipzen-mcp-stdin-reader",
        daemon=True,
    )

    async def stdin_reader() -> None:
        try:
            async with read_writer:
                while True:
                    # abandon_on_cancel: on teardown, stop waiting on the queue
                    # get without joining the daemon reader thread.
                    item = await anyio.to_thread.run_sync(q.get, abandon_on_cancel=True)
                    if item is _EOF:
                        return
                    # Everything not the EOF sentinel is a SessionMessage or an
                    # Exception (a parse/oversize rejection the session answers).
                    await read_writer.send(cast("SessionMessage | Exception", item))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        reader_thread.start()
        tg.start_soon(stdout_writer)
        tg.start_soon(stdin_reader)
        await server._mcp_server.run(
            read_stream,
            write_stream,
            server._mcp_server.create_initialization_options(),
        )
        # Server run loop returned (read stream closed on EOF): unwind the
        # transport tasks so the process can exit.
        tg.cancel_scope.cancel()


def run_guarded_stdio(
    server: FastMCP,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    eof_grace_s: float = DEFAULT_EOF_GRACE_S,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    """Run ``server`` on the Windows-resilient stdio transport (blocking).

    Drop-in for ``FastMCP.run()`` on stdio. Returns when the host closes stdin
    and the transport unwinds; the EOF watchdog guarantees the process exits
    even if the underlying run loop wedges (chipzen-ai/Chipzen#3888).

    ``stdin`` / ``stdout`` default to the process's own binary std streams and
    are injectable for tests.
    """
    grace = _resolve_eof_grace(eof_grace_s)
    eof_event = threading.Event()
    disarm = _start_watchdog(eof_event, grace)
    try:
        anyio.run(lambda: _serve(server, max_frame_bytes, eof_event, stdin=stdin, stdout=stdout))
    finally:
        disarm.set()
