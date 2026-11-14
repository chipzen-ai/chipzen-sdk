"""Match-socket close codes that end the match for this bot (no reconnect).

A match socket that closes without a ``match_end`` is normally a drop: the SDK
reconnects within the retry policy and the platform resumes the seat. Some
4xxx closes are not drops. They say the server will not take this seat back on
this match, whatever the client does, so reconnecting only loops until the
retry budget runs out (``docs/protocol/TRANSPORT-PROTOCOL.md`` section 15,
``docs/ERROR-CODES.md``).

On one of the codes in :data:`TERMINAL_CLOSE_CODES` the SDK stops at once: it
logs one ERROR line with the code and the server's reason and raises
:class:`ProtocolCloseError`, which carries both. ``4013`` (no mutually
supported protocol version) is the first the platform sends through to remote
bots.

Left out on purpose, so they stay a drop and are retried as before:

* ``4000``: seat taken over by another connection; it has its own path
  (:class:`chipzen.client.SeatReplacedError`).
* ``4001`` / ``4002``: the external-API gateway sends these itself, and a
  ``4002`` can mean the executor is not registered yet, a race a retry fixes.
* ``4004``, ``4007``, ``4008``, ``4009``, ``4011``: each has a use where a
  reconnect can succeed (for example ``4011 duplicate_participant`` while the
  old socket is still being reaped, or a connect rate limit that passes).

Normal closes (``1000`` / ``1001``) and closes with no code are unchanged.
"""

from __future__ import annotations

#: Symbolic names from TRANSPORT-PROTOCOL.md section 15 for the terminal codes.
TERMINAL_CLOSE_NAMES: dict[int, str] = {
    4003: "match_full",
    4005: "match_ended",
    4006: "participant_not_found",
    4010: "reconnect_expired",
    4012: "responsible_gaming",
    4013: "protocol_mismatch",
}

#: Close codes after which the SDK does not reconnect the match socket.
TERMINAL_CLOSE_CODES: frozenset[int] = frozenset(TERMINAL_CLOSE_NAMES)

#: The one line logged when a terminal close ends a match.
PROTOCOL_CLOSE_LOG = "match %s: %s; not reconnecting"


class ProtocolCloseError(Exception):
    """The server closed the match socket with a terminal 4xxx code.

    The match is over for this bot: reconnecting would be refused the same
    way. ``code`` is the close code and ``reason`` the server's close reason
    (``""`` when it sent none), for example ``4013`` and ``"No mutually
    supported protocol version"``.
    """

    def __init__(self, code: int, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        name = TERMINAL_CLOSE_NAMES.get(code, "")
        label = f"{code} {name}".strip()
        super().__init__(
            f"server closed the match socket with {label}: {reason or 'no reason given'}"
        )


def protocol_close_from(exc: BaseException) -> ProtocolCloseError | None:
    """The :class:`ProtocolCloseError` for a websockets close exception, if terminal.

    Reads the close frame the server sent (``exc.rcvd``). Returns ``None`` for
    any other exception, a close with no frame, or a code that is not in
    :data:`TERMINAL_CLOSE_CODES`.
    """
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if isinstance(code, bool) or not isinstance(code, int) or code not in TERMINAL_CLOSE_CODES:
        return None
    reason = getattr(rcvd, "reason", "")
    return ProtocolCloseError(code, reason if isinstance(reason, str) else "")
