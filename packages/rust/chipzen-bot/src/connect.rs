//! Environment-aware connection helper for the external-API lobby.
//!
//! Devs running an external-API bot against Chipzen shouldn't need to
//! remember the exact lobby WebSocket URL per environment.
//! [`connect_to_chipzen`] returns a fully-populated [`ConnectionConfig`]
//! with the resolved `url`, `token`, and `retry_policy`, ready to hand off
//! to [`crate::run_external_bot`].
//!
//! # Environment → URL mapping
//!
//! ```text
//! prod    -> wss://chipzen.ai/ws/external/bot/{bot_id}
//! staging -> wss://staging.chipzen.ai/ws/external/bot/{bot_id}
//! local   -> ws://localhost:8001/ws/external/bot/{bot_id}
//! ```
//!
//! # Precedence
//!
//! Resolution order for the final WebSocket URL, highest priority first:
//!
//! 1. `[external_api].url` from a discovered `chipzen.toml`. A config-file
//!    URL ALWAYS wins.
//! 2. An **explicitly passed** `env` argument (`Some(...)`).
//! 3. The `CHIPZEN_ENV` environment variable, if set to a recognized value.
//! 4. The default of `prod`.
//!
//! The explicit `url=` override valve lives one layer up, on
//! [`crate::run_external_bot`].

use crate::config::{load_chipzen_config, resolve_token, resolve_url, ChipzenConfig};
use crate::error::Error;
use crate::retry::RetryPolicy;

/// Recognized environment names. Kept as a constant so error messages can
/// list the valid values and the env-var path can validate at runtime.
pub const ENV_NAMES: &[&str] = &["prod", "staging", "local"];

/// Name of the environment variable consulted when `env` is not explicitly
/// passed.
pub const CHIPZEN_ENV_VAR: &str = "CHIPZEN_ENV";

/// One of the three recognized Chipzen environments.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnvName {
    Prod,
    Staging,
    Local,
}

impl EnvName {
    /// Parse a recognized env name. Returns `None` for anything else so the
    /// caller can build a clear error listing the legal values.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "prod" => Some(EnvName::Prod),
            "staging" => Some(EnvName::Staging),
            "local" => Some(EnvName::Local),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            EnvName::Prod => "prod",
            EnvName::Staging => "staging",
            EnvName::Local => "local",
        }
    }

    /// Format the canonical lobby URL for this env + bot id.
    fn lobby_url(&self, bot_id: &str) -> String {
        match self {
            EnvName::Prod => format!("wss://chipzen.ai/ws/external/bot/{bot_id}"),
            EnvName::Staging => format!("wss://staging.chipzen.ai/ws/external/bot/{bot_id}"),
            EnvName::Local => format!("ws://localhost:8001/ws/external/bot/{bot_id}"),
        }
    }
}

/// Fully-resolved connection parameters ready for [`crate::run_external_bot`].
#[derive(Debug, Clone)]
pub struct ConnectionConfig {
    /// WebSocket URL the bot should connect to. Either env-derived or the
    /// verbatim `[external_api].url` from a discovered `chipzen.toml`.
    pub url: String,
    /// Long-lived API token from a discovered `chipzen.toml`, or `None`.
    /// `None` doesn't mean "auth-less"; just that the helper didn't find
    /// one — the caller may pass an explicit token separately.
    pub token: Option<String>,
    /// Reconnect-pacing policy.
    pub retry_policy: RetryPolicy,
    /// The resolved environment name, or `None` if the URL came verbatim
    /// from a config file (no env mapping applied). Mostly for logs/debug.
    pub env: Option<EnvName>,
    /// The [`ChipzenConfig`] discovered during resolution (if any). Exposed
    /// so callers can pass it through and avoid a second filesystem stat.
    pub config: Option<ChipzenConfig>,
}

/// Pick the env name to use: explicit arg, then `$CHIPZEN_ENV`, then `prod`.
///
/// Returns [`Error::Protocol`] if an explicit `env` or the env-var value
/// isn't a recognized name, so a typo (`prd`) surfaces immediately.
fn resolve_env_name(explicit: Option<EnvName>, env_var: Option<&str>) -> Result<EnvName, Error> {
    if let Some(env) = explicit {
        return Ok(env);
    }
    // Empty string is treated as "not set" so an accidental `CHIPZEN_ENV=`
    // falls through to the default rather than tripping the unknown-env error.
    if let Some(val) = env_var.filter(|s| !s.is_empty()) {
        return EnvName::parse(val).ok_or_else(|| {
            Error::Protocol(format!(
                "{CHIPZEN_ENV_VAR}={val:?} is not a recognized environment. Valid values: {}.",
                ENV_NAMES.join(", ")
            ))
        });
    }
    Ok(EnvName::Prod)
}

/// Resolve a [`ConnectionConfig`] for the external-API lobby.
///
/// Maps `env` to a canonical lobby URL and combines it with whatever
/// config-file token / URL / retry policy the dev has set up.
///
/// # Arguments
///
/// * `bot_id` — external-API bot UUID issued by the platform. Required +
///   non-empty.
/// * `env` — `None` consults `$CHIPZEN_ENV` then defaults to `prod`.
/// * `retry_policy` — `None` uses [`RetryPolicy::default`].
/// * `config` — `Some` avoids a second filesystem stat; `None` triggers
///   discovery.
///
/// # Errors
///
/// Returns [`Error::Protocol`] if `bot_id` is empty, `env`/`$CHIPZEN_ENV`
/// is unrecognized, or a discovered `chipzen.toml` is malformed.
pub fn connect_to_chipzen(
    bot_id: &str,
    env: Option<EnvName>,
    retry_policy: Option<RetryPolicy>,
    config: Option<ChipzenConfig>,
) -> Result<ConnectionConfig, Error> {
    if bot_id.is_empty() {
        return Err(Error::Protocol(
            "connect_to_chipzen() requires a non-empty bot_id. Pass the external-API \
             bot UUID issued by the Chipzen platform."
                .to_string(),
        ));
    }

    // Resolve the env name first so an explicit `env`/`$CHIPZEN_ENV` typo is
    // validated even when a config-file URL ends up overriding the derived URL.
    let env_var = std::env::var(CHIPZEN_ENV_VAR).ok();
    let resolved_env = resolve_env_name(env, env_var.as_deref())?;
    let env_derived_url = resolved_env.lobby_url(bot_id);

    // Discover the config exactly once and thread it through.
    let config = match config {
        Some(c) => Some(c),
        None => load_chipzen_config(None)?,
    };

    let (url, env_for_return) = match resolve_url(None, config.as_ref()) {
        Some(config_url) => (config_url, None),
        None => (env_derived_url, Some(resolved_env)),
    };

    let token = resolve_token(None, config.as_ref());
    let policy = retry_policy.unwrap_or_default();

    Ok(ConnectionConfig {
        url,
        token,
        retry_policy: policy,
        env: env_for_return,
        config,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_name_parse_round_trips() {
        for name in ENV_NAMES {
            assert_eq!(EnvName::parse(name).unwrap().as_str(), *name);
        }
        assert!(EnvName::parse("prd").is_none());
    }

    #[test]
    fn lobby_url_templates_match_python() {
        assert_eq!(
            EnvName::Prod.lobby_url("b"),
            "wss://chipzen.ai/ws/external/bot/b"
        );
        assert_eq!(
            EnvName::Staging.lobby_url("b"),
            "wss://staging.chipzen.ai/ws/external/bot/b"
        );
        assert_eq!(
            EnvName::Local.lobby_url("b"),
            "ws://localhost:8001/ws/external/bot/b"
        );
    }

    #[test]
    fn resolve_env_explicit_wins() {
        // Explicit arg beats the env var.
        let env = resolve_env_name(Some(EnvName::Local), Some("staging")).unwrap();
        assert_eq!(env, EnvName::Local);
    }

    #[test]
    fn resolve_env_uses_env_var_then_default() {
        assert_eq!(
            resolve_env_name(None, Some("staging")).unwrap(),
            EnvName::Staging
        );
        // Empty env var → default prod.
        assert_eq!(resolve_env_name(None, Some("")).unwrap(), EnvName::Prod);
        // Unset → default prod.
        assert_eq!(resolve_env_name(None, None).unwrap(), EnvName::Prod);
    }

    #[test]
    fn resolve_env_rejects_unknown_env_var() {
        let err = resolve_env_name(None, Some("prd")).unwrap_err();
        assert!(format!("{err}").contains("not a recognized environment"));
    }

    #[test]
    fn connect_requires_bot_id() {
        let err = connect_to_chipzen("", Some(EnvName::Prod), None, None).unwrap_err();
        assert!(format!("{err}").contains("non-empty bot_id"));
    }

    #[test]
    fn connect_builds_env_derived_url() {
        let conn = connect_to_chipzen(
            "abc",
            Some(EnvName::Staging),
            None,
            Some(ChipzenConfig::default()),
        )
        .unwrap();
        assert_eq!(conn.url, "wss://staging.chipzen.ai/ws/external/bot/abc");
        assert_eq!(conn.env, Some(EnvName::Staging));
    }

    #[test]
    fn connect_config_url_overrides_env_derived() {
        let cfg = ChipzenConfig {
            url: Some("wss://verbatim/url".into()),
            token: Some("cz_extbot_t".into()),
            ..Default::default()
        };
        let conn = connect_to_chipzen("abc", Some(EnvName::Prod), None, Some(cfg)).unwrap();
        assert_eq!(conn.url, "wss://verbatim/url");
        // env is None when the URL came verbatim from config.
        assert_eq!(conn.env, None);
        assert_eq!(conn.token.as_deref(), Some("cz_extbot_t"));
    }
}
