"""Transport-layer regression tests.

Covers the two shutdown/resilience bugs found in MCP prod-readiness
verification:

* chipzen-ai/Chipzen#3887 — closing the transport while a ``wait_for_turn``
  long-poll blocks must cancel it promptly (server exits << the poll budget)
  and still run the cooperative ``session.stop()`` cleanup path.
* chipzen-ai/Chipzen#3888 — the Windows stdio server must survive a
  malformed + oversized + unknown-method burst and always exit when stdin
  closes (never a lingering child), guarded by our own reader + frame-size
  cap + stdin-EOF watchdog.

The unit tests exercise the transport primitives directly; the two
subprocess tests drive the real console entrypoint end-to-end (the shape the
verification harnesses `scenario9.py` / `rawhost.py` reproduced).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from chipzen_mcp import stdio_guard
from chipzen_mcp.bridge import TurnRegistry, TurnSnapshot
from chipzen_mcp.server import wait_for_turn_async
from chipzen_mcp.stdio_guard import (
    _EOF,
    DEFAULT_MAX_FRAME_BYTES,
    WATCHDOG_EXIT_CODE,
    _blocking_stdin_reader,
    _OversizeFrame,
    _read_capped_line,
    _resolve_eof_grace,
    _serve,
    _start_watchdog,
    run_guarded_stdio,
)

# ---------------------------------------------------------------------------
# #3887 — cancellable wait_for_turn
# ---------------------------------------------------------------------------


def _publish(registry: TurnRegistry, match_id: str = "m-1") -> None:
    now = time.time()
    registry.publish_turn(
        TurnSnapshot(
            match_id=match_id,
            request_id="req-1",
            published_at=now,
            deadline_at=now + 30.0,
            state={"hand_number": 1, "valid_actions": ["check"]},
        )
    )


async def test_wait_for_turn_async_returns_your_turn_immediately() -> None:
    registry = TurnRegistry()
    _publish(registry)
    out = await wait_for_turn_async(registry, timeout_ms=55_000)
    assert out["status"] == "your_turn"
    assert out["match_id"] == "m-1"


async def test_wait_for_turn_async_idle_on_timeout() -> None:
    registry = TurnRegistry()
    out = await wait_for_turn_async(registry, timeout_ms=10)
    assert out["status"] == "idle"


async def test_wait_for_turn_async_cancels_promptly() -> None:
    """The regression for #3887: a long-poll blocked in a worker thread must
    surrender its coroutine to cancellation well within the ~55s budget."""
    import asyncio

    registry = TurnRegistry()
    task = asyncio.create_task(wait_for_turn_async(registry, timeout_ms=55_000))
    await asyncio.sleep(0.1)  # let it enter the blocking slice loop
    assert not task.done()

    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"cancellation took {elapsed:.2f}s (must be < 2s)"


async def test_wait_for_turn_async_wakes_on_publish_without_polling_latency() -> None:
    """A turn published mid-wait is returned promptly (the Condition still
    wakes the current slice) — slicing must not add poll latency."""
    import asyncio

    registry = TurnRegistry()
    task = asyncio.create_task(wait_for_turn_async(registry, timeout_ms=55_000))
    await asyncio.sleep(0.05)
    _publish(registry)
    out = await asyncio.wait_for(task, timeout=2.0)
    assert out["status"] == "your_turn"


# ---------------------------------------------------------------------------
# #3888 — frame-size guard
# ---------------------------------------------------------------------------


def test_read_capped_line_normal() -> None:
    buf = io.BytesIO(b'{"a":1}\n{"b":2}\n')
    assert _read_capped_line(buf, 1024) == b'{"a":1}\n'
    assert _read_capped_line(buf, 1024) == b'{"b":2}\n'
    assert _read_capped_line(buf, 1024) is None  # EOF


def test_read_capped_line_rejects_oversized_and_realigns() -> None:
    cap = 32
    big = b"z" * 200
    buf = io.BytesIO(big + b"\n" + b'{"next":true}\n')
    first = _read_capped_line(buf, cap)
    assert isinstance(first, _OversizeFrame)
    # The oversized line's remainder is drained; the NEXT frame is intact.
    assert _read_capped_line(buf, cap) == b'{"next":true}\n'
    assert _read_capped_line(buf, cap) is None


def test_read_capped_line_line_exactly_at_cap_is_kept() -> None:
    cap = 8
    buf = io.BytesIO(b"12345678\n")  # 8 payload bytes + newline == not oversized
    out = _read_capped_line(buf, cap)
    assert out == b"12345678\n"


def test_blocking_stdin_reader_enqueues_parses_rejects_and_arms_eof() -> None:
    import queue

    frames = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'  # valid
        b"this is not json {{{\n"  # malformed -> Exception
        + b"y" * (DEFAULT_MAX_FRAME_BYTES + 10)
        + b"\n"  # oversized -> ValueError, rejected before the parser
        + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'  # valid again
    )
    q: queue.Queue[object] = queue.Queue()
    eof = threading.Event()
    _blocking_stdin_reader(q, io.BytesIO(frames), DEFAULT_MAX_FRAME_BYTES, eof)

    items = []
    while True:
        it = q.get_nowait()
        if it is _EOF:
            break
        items.append(it)

    assert eof.is_set()
    from mcp.shared.message import SessionMessage

    assert isinstance(items[0], SessionMessage)
    assert isinstance(items[1], Exception)  # malformed frame
    assert isinstance(items[2], ValueError)  # oversized, rejected early
    assert "exceeds" in str(items[2])
    assert isinstance(items[3], SessionMessage)


# ---------------------------------------------------------------------------
# #3888 — stdin-EOF watchdog + grace resolution
# ---------------------------------------------------------------------------


def test_watchdog_fires_after_grace_when_not_disarmed() -> None:
    fired: list[float] = []
    eof = threading.Event()
    _start_watchdog(eof, grace_s=0.15, on_fire=lambda g: fired.append(g))
    eof.set()  # stdin closed
    time.sleep(0.5)  # grace elapses without a disarm
    assert fired == [0.15]


def test_watchdog_does_not_fire_when_disarmed_in_time() -> None:
    fired: list[float] = []
    eof = threading.Event()
    disarm = _start_watchdog(eof, grace_s=0.5, on_fire=lambda g: fired.append(g))
    eof.set()
    disarm.set()  # clean teardown wins the race
    time.sleep(0.8)
    assert fired == []


def test_watchdog_idle_until_eof() -> None:
    fired: list[float] = []
    eof = threading.Event()
    _start_watchdog(eof, grace_s=0.1, on_fire=lambda g: fired.append(g))
    time.sleep(0.3)  # no EOF yet -> must not fire
    assert fired == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 5.0), ("", 5.0), ("2.5", 2.5), ("0", 5.0), ("-1", 5.0), ("nope", 5.0)],
)
def test_resolve_eof_grace(monkeypatch: pytest.MonkeyPatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv(stdio_guard.ENV_EOF_GRACE, raising=False)
    else:
        monkeypatch.setenv(stdio_guard.ENV_EOF_GRACE, value)
    assert _resolve_eof_grace() == expected


# ---------------------------------------------------------------------------
# transport plumbing (_serve / run_guarded_stdio) with injected streams
# ---------------------------------------------------------------------------


class _FakeLowLevel:
    def __init__(self) -> None:
        self.received: list[object] = []

    def create_initialization_options(self) -> dict:
        return {}

    async def run(self, read_stream, write_stream, _opts) -> None:
        async for item in read_stream:
            self.received.append(item)


class _FakeServer:
    def __init__(self) -> None:
        self._mcp_server = _FakeLowLevel()


async def test_serve_pumps_frames_and_tears_down_on_eof() -> None:
    server = _FakeServer()
    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
        b"garbage not json\n"
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    eof = threading.Event()
    # BytesIO reaches EOF at end of buffer -> transport must tear down.
    await _serve(server, DEFAULT_MAX_FRAME_BYTES, eof, stdin=stdin, stdout=io.BytesIO())

    assert eof.is_set()
    from mcp.shared.message import SessionMessage

    kinds = [type(x).__name__ for x in server._mcp_server.received]
    assert kinds.count("SessionMessage") == 2
    assert any(isinstance(x, Exception) for x in server._mcp_server.received)
    assert all(isinstance(x, (SessionMessage, Exception)) for x in server._mcp_server.received)


def test_run_guarded_stdio_returns_on_eof_and_disarms(monkeypatch: pytest.MonkeyPatch) -> None:
    # No EOF-watchdog force-exit should occur: EOF is immediate and the run
    # loop returns, disarming the watchdog.
    fired: list[float] = []
    monkeypatch.setattr(stdio_guard, "_force_exit", lambda g: fired.append(g))
    server = _FakeServer()
    run_guarded_stdio(server, stdin=io.BytesIO(b""), stdout=io.BytesIO(), eof_grace_s=0.5)
    time.sleep(0.2)
    assert fired == []


# ---------------------------------------------------------------------------
# End-to-end subprocess tests against the real console entrypoint
# ---------------------------------------------------------------------------

_SERVER_CMD = [
    sys.executable,
    "-c",
    "import sys; from chipzen_mcp.server import main; sys.exit(main())",
]
_ENV = {
    **os.environ,
    "CHIPZEN_EXTBOT_TOKEN": "cz_extbot_test",
    "CHIPZEN_BOT_ID": "00000000-0000-0000-0000-000000000001",
    "CHIPZEN_ENV": "local",
}


class _StdioClient:
    """Minimal raw JSON-RPC-over-stdio client for a server subprocess."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.responses: dict[object, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        # Drain stderr so a full pipe never backpressures the child.
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    self.responses[msg["id"]] = msg

    def _drain_stderr(self) -> None:
        for _ in self.proc.stderr:
            pass

    def send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def send_raw(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()

    def wait_resp(self, rid: object, timeout: float = 15.0) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if rid in self.responses:
                    return self.responses[rid]
            time.sleep(0.02)
        return None

    def handshake(self) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        assert self.wait_resp(1) is not None, "server did not answer initialize"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(
        _SERVER_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=_ENV,
    )


def test_transport_close_during_wait_for_turn_exits_promptly_and_cleans_up() -> None:
    """#3887 end-to-end: close stdin while a wait_for_turn long-poll blocks —
    the server must exit well under the poll budget AND run session.stop()."""
    proc = _spawn()
    stderr_chunks: list[bytes] = []

    def drain_err() -> None:
        for line in proc.stderr:
            stderr_chunks.append(line)

    threading.Thread(target=drain_err, daemon=True).start()

    responses: dict = {}

    def read_out() -> None:
        for raw in proc.stdout:
            try:
                m = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(m, dict) and "id" in m:
                responses[m["id"]] = m

    threading.Thread(target=read_out, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
    )
    deadline = time.time() + 10
    while 1 not in responses and time.time() < deadline:
        time.sleep(0.02)
    assert 1 in responses, "no initialize response"
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send(
        {
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {"name": "wait_for_turn", "arguments": {"timeout_ms": 55000}},
        }
    )
    time.sleep(1.0)  # let the long-poll block

    start = time.time()
    proc.stdin.close()
    try:
        code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("server did not exit within 15s of stdin close (mid-poll)")
    elapsed = time.time() - start
    time.sleep(0.2)  # let stderr drain
    err = b"".join(stderr_chunks).decode("utf-8", "replace")

    assert elapsed < 2.0, f"exit took {elapsed:.2f}s (must be < 2s), code={code}"
    assert code == 0
    assert "session stopped cleanly" in err, "cooperative cleanup path did not run"


def test_hostile_frame_sequence_then_live_request_then_clean_exit() -> None:
    """#3888 end-to-end: a malformed + oversized + unknown-method burst must
    not wedge the server — a following live request is answered and closing
    stdin exits the process (never a lingering child)."""
    proc = _spawn()
    client = _StdioClient(proc)
    client.handshake()

    # A spread of offline-safe calls first (mirrors rawhost.py's preamble).
    rid = 10
    for name, args in [
        ("get_status", {}),
        ("list_matches", {}),
        ("get_match_state", {"match_id": "does-not-exist"}),
        ("act", {"match_id": "x", "action": "fold"}),
        ("wait_for_turn", {"timeout_ms": 200}),
    ]:
        rid += 1
        client.send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        assert client.wait_resp(rid) is not None, f"no response to {name}"

    # Hostile burst: malformed line, oversized (>1 MiB) arg frame, unknown method.
    client.send_raw("this is not json at all {{{")
    rid += 1
    client.send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "get_status", "arguments": {}},
        }
    )
    assert client.wait_resp(rid) is not None, "server died on malformed frame"

    rid += 1
    big = "z" * (2 * 1024 * 1024)
    client.send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "get_match_state", "arguments": {"match_id": big}},
        }
    )
    # Oversized frame is rejected (a JSON-RPC parse error, not tied to `rid`),
    # so we do not require a keyed response — only that the server survives it.

    rid += 1
    client.send({"jsonrpc": "2.0", "id": rid, "method": "no/such/method", "params": {}})
    unknown = client.wait_resp(rid, timeout=10)

    # Live request AFTER the hostile burst must still be answered.
    rid += 1
    client.send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "get_status", "arguments": {}},
        }
    )
    live = client.wait_resp(rid, timeout=10)
    assert live is not None, "server stopped answering after hostile burst"
    assert unknown is not None and "error" in unknown, "unknown method not rejected with an error"

    # Closing stdin must exit the process — cleanly, or via the watchdog — but
    # never leave it lingering.
    start = time.time()
    proc.stdin.close()
    try:
        code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("server did not exit within 15s of stdin close (lingering child)")
    assert code in (0, WATCHDOG_EXIT_CODE), f"unexpected exit code {code}"
    assert time.time() - start < 15
