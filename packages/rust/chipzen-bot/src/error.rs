use thiserror::Error;

/// Top-level error type for SDK operations. Variants describe the
/// failure surface in terms users care about (connect failed, server
/// dropped us mid-handshake, peer sent something unparseable) rather
/// than transport internals.
///
/// Large variants (`Transport`, `Json`) are boxed so `Result<T, Error>`
/// stays small on the success path. The boxing is invisible to callers
/// thanks to the `From` impls below.
#[derive(Debug, Error)]
pub enum Error {
    #[error("WebSocket transport error: {0}")]
    Transport(Box<tokio_tungstenite::tungstenite::Error>),

    #[error("connection closed before {context}")]
    ConnectionClosed { context: &'static str },

    #[error("invalid URL: {0}")]
    InvalidUrl(#[from] url::ParseError),

    #[error("malformed protocol message: {0}")]
    Protocol(String),

    #[error("JSON serialization failed: {0}")]
    Json(Box<serde_json::Error>),

    #[error("retry budget exhausted ({attempts} attempts) — last error: {last_error}")]
    RetriesExhausted { attempts: u32, last_error: String },
}

impl From<tokio_tungstenite::tungstenite::Error> for Error {
    fn from(err: tokio_tungstenite::tungstenite::Error) -> Self {
        Error::Transport(Box::new(err))
    }
}

impl From<serde_json::Error> for Error {
    fn from(err: serde_json::Error) -> Self {
        Error::Json(Box::new(err))
    }
}
