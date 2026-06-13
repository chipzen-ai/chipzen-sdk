//! `chipzen.toml` discovery and parsing for the SDK.
//!
//! Devs running an external-API bot should be able to drop their
//! long-lived API token into a config file once and forget about it,
//! instead of hard-coding `token = "cz_extbot_..."` into source. This
//! module implements the discovery + parsing half of that convention;
//! the [`crate::run_external_bot`] entry point (and the
//! `chipzen-sdk run-external` CLI) consume the result and prefer explicit
//! arguments over config-file values.
//!
//! # Discovery
//!
//! Search order, first match wins:
//!
//! 1. `./chipzen.toml` (current working directory)
//! 2. `~/.chipzen/chipzen.toml` (user-home config)
//! 3. `/etc/chipzen/chipzen.toml` (system config, POSIX only — silently
//!    skipped on Windows where `/etc` does not exist)
//!
//! If no file is found, [`load_chipzen_config`] returns `Ok(None)` and the
//! caller falls back to whatever explicit arguments were passed. A clear
//! error is only raised when a file IS found but is malformed or missing
//! the expected section.
//!
//! # File format
//!
//! ```toml
//! [external_api]
//! token  = "cz_extbot_<32-char-base62-random>"
//! url    = "wss://chipzen.ai/ws/external/bot/<bot_id>"  # optional
//! bot_id = "<bot-uuid>"                                 # optional
//! ```
//!
//! All three fields are optional. `url` (when set) overrides the env-aware
//! lobby URL helper. `bot_id` is the external-API bot UUID; it's consumed
//! by the `run-external` CLI to build the env-derived URL when no explicit
//! `url` is configured.

use crate::error::Error;
use serde::Deserialize;
use std::path::{Path, PathBuf};

/// The config-file name searched for on the discovery path.
pub const CONFIG_FILENAME: &str = "chipzen.toml";
/// The TOML table the SDK reads its settings from.
pub const SECTION_NAME: &str = "external_api";

/// Parsed contents of a `chipzen.toml` file.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ChipzenConfig {
    /// Filesystem path the config was loaded from. `None` for configs
    /// constructed in-memory (e.g. in tests). Useful for error messages.
    pub path: Option<PathBuf>,
    /// Value of `[external_api].token` if present.
    pub token: Option<String>,
    /// Value of `[external_api].url` if present.
    pub url: Option<String>,
    /// Value of `[external_api].bot_id` if present.
    pub bot_id: Option<String>,
}

/// Serde shape of the `[external_api]` table. `deny_unknown_fields` is
/// deliberately NOT set — forward-compat: a newer file may carry keys this
/// SDK version doesn't know about, and that shouldn't be a hard error.
#[derive(Debug, Deserialize)]
struct RawDoc {
    external_api: Option<RawSection>,
}

#[derive(Debug, Deserialize)]
struct RawSection {
    token: Option<String>,
    url: Option<String>,
    bot_id: Option<String>,
}

/// Return the ordered list of candidate config-file locations.
fn search_paths() -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        paths.push(cwd.join(CONFIG_FILENAME));
    }
    if let Some(home) = home_dir() {
        paths.push(home.join(".chipzen").join(CONFIG_FILENAME));
    }
    // POSIX-only system path; `/etc` is not meaningful on Windows.
    if cfg!(not(windows)) {
        paths.push(PathBuf::from("/etc/chipzen").join(CONFIG_FILENAME));
    }
    paths
}

/// Best-effort home-directory lookup without pulling in the `dirs` crate.
/// Honors `$HOME` (POSIX) then `%USERPROFILE%` (Windows).
fn home_dir() -> Option<PathBuf> {
    if let Some(home) = std::env::var_os("HOME").filter(|s| !s.is_empty()) {
        return Some(PathBuf::from(home));
    }
    if let Some(profile) = std::env::var_os("USERPROFILE").filter(|s| !s.is_empty()) {
        return Some(PathBuf::from(profile));
    }
    None
}

/// Return the first existing `chipzen.toml` on the search path, or `None`.
///
/// Pass `Some(paths)` to override the default search order (mostly useful
/// for tests).
pub fn discover_config_path(search: Option<&[PathBuf]>) -> Option<PathBuf> {
    let owned;
    let candidates: &[PathBuf] = match search {
        Some(s) => s,
        None => {
            owned = search_paths();
            &owned
        }
    };
    candidates.iter().find(|p| p.is_file()).cloned()
}

/// Discover and parse a `chipzen.toml` from the search path.
///
/// Pass `Some(paths)` to override the default search order. Returns
/// `Ok(None)` if no file exists (NOT an error — the SDK falls back to
/// explicit arguments). Returns [`Error::Protocol`] if a file is found but
/// is malformed or lacks the `[external_api]` section — a "found but
/// unusable" file is always a hard error so a typo surfaces immediately
/// rather than being silently masked.
pub fn load_chipzen_config(search: Option<&[PathBuf]>) -> Result<Option<ChipzenConfig>, Error> {
    let Some(path) = discover_config_path(search) else {
        return Ok(None);
    };
    load_from_path(&path).map(Some)
}

/// Parse a specific `chipzen.toml`. Public for the CLI / tests that already
/// know the path.
pub fn load_from_path(path: &Path) -> Result<ChipzenConfig, Error> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| Error::Protocol(format!("failed to read {}: {e}", path.display())))?;

    let doc: RawDoc = toml::from_str(&raw).map_err(|e| {
        Error::Protocol(format!(
            "failed to parse {}: {e}. Fix the syntax or delete the file to fall \
             back to explicit run_external_bot arguments.",
            path.display()
        ))
    })?;

    let Some(section) = doc.external_api else {
        return Err(Error::Protocol(format!(
            "{} has no [{SECTION_NAME}] section. Add one with at least:\n\n  \
             [{SECTION_NAME}]\n  token = \"cz_extbot_...\"\n",
            path.display()
        )));
    };

    Ok(ChipzenConfig {
        path: Some(path.to_path_buf()),
        token: section.token,
        url: section.url,
        bot_id: section.bot_id,
    })
}

/// Return the token to use, honoring precedence:
///
/// 1. An explicit `token` (even an empty string — the dev was explicit).
/// 2. Else, the config-file token.
/// 3. Else, `None` (the caller decides whether that's a hard error).
pub fn resolve_token(
    explicit_token: Option<&str>,
    config: Option<&ChipzenConfig>,
) -> Option<String> {
    if let Some(t) = explicit_token {
        return Some(t.to_string());
    }
    config.and_then(|c| c.token.clone())
}

/// Return the URL override to use, honoring precedence:
///
/// 1. An explicit `url`.
/// 2. Else, the config-file url.
/// 3. Else, `None` (the caller falls back to its own default).
pub fn resolve_url(explicit_url: Option<&str>, config: Option<&ChipzenConfig>) -> Option<String> {
    if let Some(u) = explicit_url {
        return Some(u.to_string());
    }
    config.and_then(|c| c.url.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_toml(dir: &std::path::Path, body: &str) -> PathBuf {
        let path = dir.join(CONFIG_FILENAME);
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(body.as_bytes()).unwrap();
        path
    }

    #[test]
    fn loads_all_external_api_fields() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_toml(
            dir.path(),
            "[external_api]\ntoken = \"cz_extbot_abc\"\nurl = \"wss://x/y\"\nbot_id = \"uuid-1\"\n",
        );
        let cfg = load_from_path(&path).unwrap();
        assert_eq!(cfg.token.as_deref(), Some("cz_extbot_abc"));
        assert_eq!(cfg.url.as_deref(), Some("wss://x/y"));
        assert_eq!(cfg.bot_id.as_deref(), Some("uuid-1"));
        assert_eq!(cfg.path.as_deref(), Some(path.as_path()));
    }

    #[test]
    fn all_fields_optional() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_toml(dir.path(), "[external_api]\n");
        let cfg = load_from_path(&path).unwrap();
        assert!(cfg.token.is_none() && cfg.url.is_none() && cfg.bot_id.is_none());
    }

    #[test]
    fn missing_section_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_toml(dir.path(), "[other]\nfoo = 1\n");
        let err = load_from_path(&path).unwrap_err();
        assert!(format!("{err}").contains("has no [external_api] section"));
    }

    #[test]
    fn malformed_toml_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_toml(dir.path(), "[external_api\ntoken = ");
        let err = load_from_path(&path).unwrap_err();
        assert!(format!("{err}").contains("failed to parse"));
    }

    #[test]
    fn unknown_keys_are_tolerated_forward_compat() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_toml(
            dir.path(),
            "[external_api]\ntoken = \"t\"\nfuture_field = \"ignored\"\n",
        );
        let cfg = load_from_path(&path).unwrap();
        assert_eq!(cfg.token.as_deref(), Some("t"));
    }

    #[test]
    fn discovery_returns_first_existing_in_order() {
        let dir = tempfile::tempdir().unwrap();
        let present = write_toml(dir.path(), "[external_api]\ntoken = \"t\"\n");
        let missing = dir.path().join("nope").join(CONFIG_FILENAME);
        // First entry missing, second present → second is picked.
        let found = discover_config_path(Some(&[missing, present.clone()]));
        assert_eq!(found.as_deref(), Some(present.as_path()));
    }

    #[test]
    fn no_file_on_path_is_ok_none() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("absent").join(CONFIG_FILENAME);
        assert!(load_chipzen_config(Some(&[missing])).unwrap().is_none());
    }

    #[test]
    fn resolve_precedence() {
        let cfg = ChipzenConfig {
            token: Some("cfg_tok".into()),
            url: Some("cfg_url".into()),
            ..Default::default()
        };
        // Explicit wins, even an empty string.
        assert_eq!(
            resolve_token(Some("explicit"), Some(&cfg)).as_deref(),
            Some("explicit")
        );
        assert_eq!(resolve_token(Some(""), Some(&cfg)).as_deref(), Some(""));
        // Else config.
        assert_eq!(resolve_token(None, Some(&cfg)).as_deref(), Some("cfg_tok"));
        // Else None.
        assert_eq!(resolve_token(None, None), None);

        assert_eq!(
            resolve_url(Some("ex_url"), Some(&cfg)).as_deref(),
            Some("ex_url")
        );
        assert_eq!(resolve_url(None, Some(&cfg)).as_deref(), Some("cfg_url"));
        assert_eq!(resolve_url(None, None), None);
    }
}
