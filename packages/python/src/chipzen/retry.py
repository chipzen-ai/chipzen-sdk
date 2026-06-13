"""Retry / backoff policy for the WebSocket client.

When the connection to the Chipzen server drops (TCP reset, heartbeat
miss, transient network failure, etc.) the SDK reconnects within the
server's reconnect grace window. The pacing of those reconnect attempts
is configurable via :class:`RetryPolicy`, accepted by both
:func:`chipzen.client.run_bot` and :func:`chipzen.external.run_external_bot`.

Typical use::

    from chipzen import Bot, run_external_bot
    from chipzen.retry import RetryPolicy

    policy = RetryPolicy(
        max_reconnect_attempts=5,
        initial_backoff_ms=500,
        max_backoff_ms=30_000,
        backoff_multiplier=2.0,
    )

    await run_external_bot(MyBot(), bot_id="<your-bot-uuid>", env="staging",
                           token="cz_extbot_...", retry_policy=policy)

The default policy mirrors the spec from External-API Issue 26:
5 attempts, 500ms initial backoff, doubling each attempt, capped at
30 seconds. The defaults are sensible for the typical home-network
deployment; devs on noisy connections may want a longer backoff or
more attempts.

Note: this policy controls **only** how reconnect attempts are paced.
The 30-second server-side grace window itself is unchanged; if the
reconnects burn through the window the session is considered lost and
the server terminates the match-side state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff knobs applied to reconnect attempts.

    Attributes:
        max_reconnect_attempts: Maximum number of reconnection attempts
            after a connection drop or heartbeat miss. Must be ``>= 0``.
            ``0`` disables reconnection entirely (the first connect
            failure raises). Default ``5``.
        initial_backoff_ms: Delay before the **first** reconnect attempt,
            in milliseconds. Must be ``>= 0``. Default ``500``.
        max_backoff_ms: Upper bound for any single backoff delay, in
            milliseconds. Must be ``>= initial_backoff_ms``. Default
            ``30_000`` (30 seconds — matches the server-side grace
            window so a single backoff never exceeds the window itself).
        backoff_multiplier: Exponential factor applied between attempts.
            ``2.0`` doubles the delay each attempt. Must be ``>= 1.0``;
            ``1.0`` produces constant backoff. Default ``2.0``.

    Backoff progression for attempt ``n`` (1-indexed) is::

        min(initial_backoff_ms * backoff_multiplier ** (n - 1),
            max_backoff_ms)

    Examples (defaults)::

        attempt 1: 500 ms
        attempt 2: 1000 ms
        attempt 3: 2000 ms
        attempt 4: 4000 ms
        attempt 5: 8000 ms
        attempt 6: 16000 ms  (would be next, but capped by attempts=5)
    """

    max_reconnect_attempts: int = 5
    initial_backoff_ms: int = 500
    max_backoff_ms: int = 30_000
    backoff_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_reconnect_attempts < 0:
            raise ValueError(
                f"max_reconnect_attempts must be >= 0, got {self.max_reconnect_attempts}"
            )
        if self.initial_backoff_ms < 0:
            raise ValueError(f"initial_backoff_ms must be >= 0, got {self.initial_backoff_ms}")
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError(
                "max_backoff_ms must be >= initial_backoff_ms "
                f"({self.max_backoff_ms} < {self.initial_backoff_ms})"
            )
        if self.backoff_multiplier < 1.0:
            raise ValueError(f"backoff_multiplier must be >= 1.0, got {self.backoff_multiplier}")

    def backoff_ms(self, attempt: int) -> int:
        """Return the delay (in ms) to wait **before** the given attempt.

        Args:
            attempt: 1-indexed attempt number. ``attempt=1`` is the first
                reconnect after a drop, ``attempt=2`` the second, etc.

        Returns:
            The delay in milliseconds, capped at ``max_backoff_ms``.

        Raises:
            ValueError: if ``attempt < 1``.
        """
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        # Compute initial * multiplier ** (attempt - 1) using float math
        # then clamp + round to int. Using ``min(..., max_backoff_ms)``
        # before the int cast keeps the cap exact even when the float
        # product overflows the cap by a fraction.
        raw = self.initial_backoff_ms * (self.backoff_multiplier ** (attempt - 1))
        return int(min(raw, self.max_backoff_ms))


DEFAULT_RETRY_POLICY = RetryPolicy()
"""The default :class:`RetryPolicy` used when ``run_bot`` is called
without an explicit ``retry_policy`` argument."""
