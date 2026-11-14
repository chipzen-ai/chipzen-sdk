//! Match-socket close codes that end the match for this bot (no reconnect).
//!
//! A match socket that closes without a `match_end` is normally a drop: the
//! SDK reconnects within the retry policy and the platform resumes the seat.
//! Some 4xxx closes are not drops. They say the server will not take this
//! seat back on this match, whatever the client does, so reconnecting only
//! loops until the retry budget runs out (`docs/protocol/TRANSPORT-PROTOCOL.md`
//! section 15, `docs/ERROR-CODES.md`).
//!
//! On a code in [`TERMINAL_CLOSE_CODES`] the SDK stops at once: it logs one
//! line with the code and the server's reason and returns
//! [`Error::ProtocolClose`], which carries both. `4013` (no mutually supported
//! protocol version) is the first the platform sends through to remote bots.
//!
//! Left out on purpose, so they stay a drop and are retried as before:
//!
//! * `4000`: seat taken over by another connection; it has its own path
//!   ([`Error::SeatReplaced`]).
//! * `4001` / `4002`: the external-API gateway sends these itself, and a
//!   `4002` can mean the executor is not registered yet, a race a retry fixes.
//! * `4004`, `4007`, `4008`, `4009`, `4011`: each has a use where a reconnect
//!   can succeed (for example `4011 duplicate_participant` while the old
//!   socket is still being reaped, or a connect rate limit that passes).
//!
//! Normal closes (`1000` / `1001`) and closes with no code are unchanged.

use crate::client::MessageReader;
use crate::error::Error;

/// Close codes after which the SDK does not reconnect the match socket.
pub const TERMINAL_CLOSE_CODES: &[u16] = &[4003, 4005, 4006, 4010, 4012, 4013];

/// True if `code` is in [`TERMINAL_CLOSE_CODES`].
pub fn is_terminal_close(code: u16) -> bool {
    TERMINAL_CLOSE_CODES.contains(&code)
}

/// The symbolic name from TRANSPORT-PROTOCOL.md section 15 for a terminal
/// code, or `""` for any other code.
pub(crate) fn terminal_close_name(code: u16) -> &'static str {
    match code {
        4003 => "match_full",
        4005 => "match_ended",
        4006 => "participant_not_found",
        4010 => "reconnect_expired",
        4012 => "responsible_gaming",
        4013 => "protocol_mismatch",
        _ => "",
    }
}

/// `"4013 protocol_mismatch"`, or just the number for a code with no name.
pub(crate) fn close_label(code: u16) -> String {
    format!("{code} {}", terminal_close_name(code))
        .trim_end()
        .to_string()
}

/// The reason as sent, or `"no reason given"` when it is empty.
pub(crate) fn reason_or_default(reason: &str) -> &str {
    if reason.is_empty() {
        "no reason given"
    } else {
        reason
    }
}

/// The [`Error::ProtocolClose`] for the close `reader` saw, if it is terminal.
pub(crate) fn terminal_close_of<R: MessageReader + ?Sized>(reader: &R) -> Option<Error> {
    let code = reader
        .close_code()
        .filter(|code| is_terminal_close(*code))?;
    Some(Error::ProtocolClose {
        code,
        reason: reader.close_reason().unwrap_or_default(),
    })
}

/// The one line logged when a terminal close ends a match.
pub(crate) fn protocol_close_log(match_id: &str, err: &Error) -> String {
    format!("chipzen-sdk: match {match_id}: {err}; not reconnecting")
}
