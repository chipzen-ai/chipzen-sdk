"""Tests for :class:`chipzen.retry.RetryPolicy` + integration with ``run_bot``.

Covers the External-API Issue 26 spec
(``management/ophir-track/external-api-issue-breakdown.md`` §26):

- default knob values match the spec
- backoff progression: initial -> multiplier behavior -> max cap
- attempt cap honored (and 0 disables reconnection)
- validation of constructor args
- ``run_bot`` honors a custom ``retry_policy`` (pacing + cap)
- legacy ``max_retries=`` kw still works and overrides the policy cap
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
from chipzen.bot import ChipzenBot
from chipzen.client import run_bot
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

# ---------------------------------------------------------------------------
# RetryPolicy: defaults + validation
# ---------------------------------------------------------------------------


def test_default_values_match_spec():
    """Defaults exactly match the External-API Issue 26 spec."""
    p = RetryPolicy()
    assert p.max_reconnect_attempts == 5
    assert p.initial_backoff_ms == 500
    assert p.max_backoff_ms == 30_000
    assert p.backoff_multiplier == 2.0


def test_default_policy_constant_is_a_retry_policy():
    """The module-level default constant uses spec defaults."""
    assert isinstance(DEFAULT_RETRY_POLICY, RetryPolicy)
    assert DEFAULT_RETRY_POLICY == RetryPolicy()


def test_retry_policy_is_frozen():
    """Policy is immutable so callers can share a single instance."""
    p = RetryPolicy()
    with pytest.raises((AttributeError, Exception)):
        p.max_reconnect_attempts = 99  # type: ignore[misc]


def test_retry_policy_is_exported_from_chipzen():
    """``from chipzen import RetryPolicy`` works -- canonical public name."""
    assert chipzen.RetryPolicy is RetryPolicy
    assert "RetryPolicy" in chipzen.__all__


def test_negative_max_attempts_raises():
    with pytest.raises(ValueError, match="max_reconnect_attempts"):
        RetryPolicy(max_reconnect_attempts=-1)


def test_negative_initial_backoff_raises():
    with pytest.raises(ValueError, match="initial_backoff_ms"):
        RetryPolicy(initial_backoff_ms=-1)


def test_max_smaller_than_initial_raises():
    with pytest.raises(ValueError, match="max_backoff_ms"):
        RetryPolicy(initial_backoff_ms=1000, max_backoff_ms=500)


def test_multiplier_below_one_raises():
    with pytest.raises(ValueError, match="backoff_multiplier"):
        RetryPolicy(backoff_multiplier=0.5)


def test_zero_max_attempts_is_valid():
    """``max_reconnect_attempts=0`` disables reconnection -- the first
    failure raises immediately. This is a legitimate configuration for
    short-lived test bots."""
    p = RetryPolicy(max_reconnect_attempts=0)
    assert p.max_reconnect_attempts == 0


def test_zero_initial_backoff_is_valid():
    """``initial_backoff_ms=0`` immediately retries -- useful for tests
    that don't want to wait 500ms per attempt."""
    p = RetryPolicy(initial_backoff_ms=0, max_backoff_ms=0)
    assert p.backoff_ms(1) == 0
    assert p.backoff_ms(5) == 0


def test_multiplier_exactly_one_is_constant_backoff():
    """``backoff_multiplier=1.0`` gives constant backoff -- no growth."""
    p = RetryPolicy(initial_backoff_ms=750, backoff_multiplier=1.0)
    assert p.backoff_ms(1) == 750
    assert p.backoff_ms(2) == 750
    assert p.backoff_ms(10) == 750


# ---------------------------------------------------------------------------
# RetryPolicy.backoff_ms: progression + cap
# ---------------------------------------------------------------------------


def test_backoff_progression_default_policy():
    """First attempt = initial; doubles each attempt up to the cap.

    Default policy: initial=500, multiplier=2.0, max=30000.
    Progression: 500 -> 1000 -> 2000 -> 4000 -> 8000 -> 16000 -> 30000 (capped)
    """
    p = RetryPolicy()
    assert p.backoff_ms(1) == 500
    assert p.backoff_ms(2) == 1000
    assert p.backoff_ms(3) == 2000
    assert p.backoff_ms(4) == 4000
    assert p.backoff_ms(5) == 8000
    # Beyond the configured max (5 attempts), the formula still produces
    # values; the cap kicks in eventually.
    assert p.backoff_ms(6) == 16_000
    assert p.backoff_ms(7) == 30_000  # min(32000, 30000)
    assert p.backoff_ms(8) == 30_000  # min(64000, 30000) -- pinned at cap
    assert p.backoff_ms(20) == 30_000  # far past the cap -- still pinned


def test_backoff_progression_initial_value_is_first_attempt():
    """Spec: ``initial_backoff_ms`` is the delay before the FIRST
    reconnect attempt, not the second."""
    p = RetryPolicy(initial_backoff_ms=1234, max_backoff_ms=999_999)
    assert p.backoff_ms(1) == 1234


def test_backoff_progression_multiplier_applies_each_attempt():
    """Each attempt multiplies the previous (uncapped) delay by ``backoff_multiplier``."""
    p = RetryPolicy(initial_backoff_ms=100, backoff_multiplier=3.0, max_backoff_ms=999_999)
    assert p.backoff_ms(1) == 100
    assert p.backoff_ms(2) == 300
    assert p.backoff_ms(3) == 900
    assert p.backoff_ms(4) == 2_700
    assert p.backoff_ms(5) == 8_100


def test_backoff_cap_clamps_at_max():
    """``max_backoff_ms`` clamps the delay regardless of attempt number."""
    p = RetryPolicy(
        initial_backoff_ms=1000,
        backoff_multiplier=10.0,
        max_backoff_ms=5_000,
    )
    assert p.backoff_ms(1) == 1_000
    assert p.backoff_ms(2) == 5_000  # min(10000, 5000)
    assert p.backoff_ms(3) == 5_000  # min(100000, 5000)
    assert p.backoff_ms(100) == 5_000  # min(huge, 5000)


def test_backoff_with_fractional_multiplier():
    """Float multipliers like 1.5 are honored (rounded to int ms)."""
    p = RetryPolicy(initial_backoff_ms=1000, backoff_multiplier=1.5, max_backoff_ms=999_999)
    assert p.backoff_ms(1) == 1000
    assert p.backoff_ms(2) == 1500
    assert p.backoff_ms(3) == 2250
    assert p.backoff_ms(4) == 3375


def test_backoff_attempt_zero_raises():
    """Attempt numbering is 1-indexed -- attempt 0 is nonsense."""
    p = RetryPolicy()
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        p.backoff_ms(0)


def test_backoff_negative_attempt_raises():
    p = RetryPolicy()
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        p.backoff_ms(-1)


def test_backoff_returns_int_type():
    """``backoff_ms`` always returns ``int`` (not float) so it can feed
    directly into ``asyncio.sleep(ms / 1000)`` without surprises."""
    p = RetryPolicy(initial_backoff_ms=333, backoff_multiplier=1.7, max_backoff_ms=100_000)
    for attempt in range(1, 10):
        assert isinstance(p.backoff_ms(attempt), int)


# ---------------------------------------------------------------------------
# run_bot integration: pacing + attempt cap
# ---------------------------------------------------------------------------


class _NoopBot(ChipzenBot):
    """Minimal bot for run_bot integration tests."""

    def decide(self, state):  # type: ignore[override]
        from chipzen.models import Action

        return Action.fold()


class _ConnectStub:
    """Stub for ``websockets.connect`` that fails N times then succeeds.

    Records every call so tests can assert the call count, and on the
    successful Nth call yields a context manager that exits cleanly
    (which makes ``run_bot``'s session-loop fall through to ``return``).
    """

    def __init__(self, fail_count: int):
        self.fail_count = fail_count
        self.calls = 0

    def __call__(self, _url):  # noqa: D401 -- mimics websockets.connect signature
        self.calls += 1
        if self.calls <= self.fail_count:
            return _RaisingCM()
        return _SuccessCM()


class _RaisingCM:
    """Async context manager that raises on ``__aenter__``."""

    async def __aenter__(self):
        raise ConnectionError("simulated drop")

    async def __aexit__(self, *args):
        return False


class _StubWS:
    """Mock websocket whose ``recv`` returns a server-error frame so that
    ``_run_session`` exits cleanly after the authenticate step."""

    def __init__(self):
        self.sent: list[str] = []
        self._first = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def recv(self) -> str:
        # Return an "error" frame so _run_session bails after authenticate
        # without us having to script a full handshake. _run_session's
        # selected_version branch logs + returns; that's enough.
        return '{"type":"error","code":"x","message":"test exit"}'

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _SuccessCM:
    """Async context manager that yields a stub websocket on ``__aenter__``."""

    async def __aenter__(self):
        return _StubWS()

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_run_bot_uses_policy_for_pacing(monkeypatch):
    """``run_bot`` calls ``asyncio.sleep`` with the policy's backoff
    progression on successive reconnect attempts."""
    # Stub websockets.connect to fail twice then succeed.
    stub = _ConnectStub(fail_count=2)
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    # Custom policy: small numbers so the math is easy to assert.
    policy = RetryPolicy(
        max_reconnect_attempts=5,
        initial_backoff_ms=100,
        max_backoff_ms=10_000,
        backoff_multiplier=2.0,
    )

    await run_bot(
        "ws://localhost/ws/match/abc/bot",
        _NoopBot(),
        retry_policy=policy,
        token="t",
    )

    # Two failed attempts -> two sleeps before the third (successful) connect.
    assert len(sleeps) == 2
    # Attempt 1 backoff = 100ms = 0.1s; attempt 2 = 200ms = 0.2s.
    assert sleeps[0] == pytest.approx(0.1)
    assert sleeps[1] == pytest.approx(0.2)
    # Connect was called 3 times total (2 failures + 1 success).
    assert stub.calls == 3


@pytest.mark.asyncio
async def test_run_bot_gives_up_after_max_attempts(monkeypatch):
    """When every reconnect fails, the last exception propagates."""
    stub = _ConnectStub(fail_count=999)  # always fail
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)

    async def _noop_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    policy = RetryPolicy(
        max_reconnect_attempts=3,
        initial_backoff_ms=1,
        max_backoff_ms=1,
    )

    with pytest.raises(ConnectionError, match="simulated drop"):
        await run_bot(
            "ws://localhost/ws/match/abc/bot",
            _NoopBot(),
            retry_policy=policy,
            token="t",
        )

    # 1 initial connect + 3 retries = 4 calls.
    assert stub.calls == 4


@pytest.mark.asyncio
async def test_run_bot_zero_attempts_no_retry(monkeypatch):
    """``max_reconnect_attempts=0`` -- the first failure raises with
    no retries."""
    stub = _ConnectStub(fail_count=999)
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    policy = RetryPolicy(max_reconnect_attempts=0, initial_backoff_ms=0, max_backoff_ms=0)

    with pytest.raises(ConnectionError):
        await run_bot(
            "ws://localhost/ws/match/abc/bot",
            _NoopBot(),
            retry_policy=policy,
            token="t",
        )

    # Exactly one connect attempt; no sleep call (no retry).
    assert stub.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_run_bot_default_policy_when_none_passed(monkeypatch):
    """No ``retry_policy`` argument -> the default policy (5 attempts)
    is used and the first-attempt backoff is 500ms = 0.5s."""
    stub = _ConnectStub(fail_count=1)
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    await run_bot(
        "ws://localhost/ws/match/abc/bot",
        _NoopBot(),
        token="t",
    )

    # 1 failure -> 1 sleep using default initial backoff = 500ms.
    assert sleeps == [pytest.approx(0.5)]
    assert stub.calls == 2


@pytest.mark.asyncio
async def test_legacy_max_retries_kw_overrides_policy_cap(monkeypatch):
    """Backward compat: passing ``max_retries=N`` overrides only the
    attempt cap; other knobs come from the default policy."""
    stub = _ConnectStub(fail_count=999)
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)

    async def _noop_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(ConnectionError):
        await run_bot(
            "ws://localhost/ws/match/abc/bot",
            _NoopBot(),
            max_retries=2,  # legacy override
            token="t",
        )

    # 1 initial + 2 retries = 3 calls.
    assert stub.calls == 3


@pytest.mark.asyncio
async def test_max_retries_overrides_explicit_policy(monkeypatch):
    """If both ``max_retries`` and ``retry_policy`` are passed,
    ``max_retries`` wins for the attempt cap but the policy's other
    knobs survive."""
    stub = _ConnectStub(fail_count=999)
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    policy = RetryPolicy(
        max_reconnect_attempts=10,  # overridden
        initial_backoff_ms=50,  # preserved
        max_backoff_ms=10_000,
        backoff_multiplier=3.0,  # preserved
    )

    with pytest.raises(ConnectionError):
        await run_bot(
            "ws://localhost/ws/match/abc/bot",
            _NoopBot(),
            max_retries=1,  # caps at 1 despite policy.max_reconnect_attempts=10
            retry_policy=policy,
            token="t",
        )

    assert stub.calls == 2  # 1 initial + 1 retry
    assert sleeps == [pytest.approx(0.05)]  # policy.initial_backoff_ms preserved
