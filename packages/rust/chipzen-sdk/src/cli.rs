//! Argument parsing + command dispatch for `chipzen-sdk`.

use crate::scaffold::{scaffold_bot, ScaffoldOptions};
use crate::validate::{validate_bot, Severity, ValidateOptions, DEFAULT_MAX_UPLOAD_BYTES};
use anyhow::{bail, Context, Result};
use chipzen_bot::{connect_to_chipzen, load_chipzen_config, resolve_token, ChipzenConfig, EnvName};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

const ABOUT: &str = "Chipzen poker bot SDK — scaffold and validate bot projects";

#[derive(Debug, Parser)]
#[command(name = "chipzen-sdk", version, about = ABOUT, long_about = None)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Scaffold a new bot project from the starter template.
    Init {
        /// Project name (used as the directory name and Cargo.toml `name`).
        name: String,
        /// Parent directory to create the project in (default: current dir).
        #[arg(long)]
        dir: Option<PathBuf>,
    },
    /// Run pre-upload checks: size, file_structure, cargo_metadata,
    /// imports, bot_impl, decide_method.
    ///
    /// Note: there is no `--check-connectivity` flag (cf. the Python
    /// and JavaScript SDKs). In Rust the protocol-conformance harness
    /// ships as a library function — `chipzen_bot::run_conformance_checks`
    /// — which runs 4 canned scenarios:
    ///
    /// * `connectivity_full_match` — handshake + 1 hand + match_end
    /// * `multi_turn_request_id_echo` — 3 turn_requests, request_id echo
    /// * `action_rejected_recovery` — safe-fallback retry on rejection
    /// * `retry_storm_bounded` — reactive response to 3 back-to-back
    ///   action_rejected messages
    ///
    /// Drive your bot through these from `tests/conformance.rs` and run
    /// via `cargo test`. The scaffolded starter at
    /// `packages/rust/starters/rust/` includes a working template.
    ///
    /// The validator is a courtesy linter — the authoritative gate is
    /// server-side seccomp + cap-drop on the bot container.
    Validate {
        /// Path to the bot project directory.
        path: PathBuf,
        /// Override max upload size in MB (default: 250).
        #[arg(long)]
        max_size_mb: Option<u64>,
        /// Disable colored output.
        #[arg(long)]
        no_color: bool,
    },
    /// Resolve + print the external-API remote-play connection (lobby URL,
    /// env, token presence) from chipzen.toml + flags, without connecting.
    ///
    /// Unlike Python's `chipzen run-external my_bot.py`, the Rust `chipzen-sdk`
    /// binary cannot dynamically load and run a bot from a file — a Rust bot
    /// is compiled into its own binary. So this subcommand is a config doctor:
    /// it does the same chipzen.toml discovery + env-aware URL resolution
    /// `run_external_bot` does and reports what it found, so you can verify
    /// your setup before wiring `chipzen_bot::run_external_cli` into your bot
    /// binary's `main` (the scaffolded starter ships a `run-external` mode
    /// that calls it). The token is never printed.
    RunExternal {
        /// Target environment for the lobby URL. Defaults to $CHIPZEN_ENV if
        /// set, otherwise 'prod'.
        #[arg(long, value_parser = ["prod", "staging", "local"])]
        env: Option<String>,
        /// External-API token (cz_extbot_...). Overrides [external_api].token
        /// in chipzen.toml. Only its presence is reported, never its value.
        #[arg(long)]
        token: Option<String>,
        /// External-API bot UUID. Overrides [external_api].bot_id in
        /// chipzen.toml. Required when no [external_api].url is set.
        #[arg(long)]
        bot_id: Option<String>,
    },
}

pub fn run(cli: Cli) -> Result<()> {
    match cli.command {
        Command::Init { name, dir } => run_init(&name, dir),
        Command::Validate {
            path,
            max_size_mb,
            no_color,
        } => run_validate(&path, max_size_mb, no_color),
        Command::RunExternal { env, token, bot_id } => {
            run_external(env.as_deref(), token.as_deref(), bot_id.as_deref())
        }
    }
}

/// Resolve the external-API connection from chipzen.toml + flags and print a
/// summary. Mirrors the resolution `run_external_bot` does so a dev can
/// confirm their setup before wiring the bot binary. Never prints the token.
fn run_external(
    env: Option<&str>,
    explicit_token: Option<&str>,
    explicit_bot_id: Option<&str>,
) -> Result<()> {
    let config: Option<ChipzenConfig> =
        load_chipzen_config(None).context("reading chipzen.toml")?;

    // Branch 1: a verbatim url in chipzen.toml wins outright.
    let config_url = config.as_ref().and_then(|c| c.url.clone());
    let (url, resolved_env) = if let Some(url) = config_url {
        (url, None)
    } else {
        // Branch 2: env-derived url. Need a bot_id.
        let bot_id = explicit_bot_id
            .map(str::to_string)
            .or_else(|| config.as_ref().and_then(|c| c.bot_id.clone()));
        let Some(bot_id) = bot_id.filter(|s| !s.is_empty()) else {
            bail!(
                "No lobby URL is configured. Either:\n  \
                 - Pass --bot-id <id>, or\n  \
                 - Set [external_api].bot_id in chipzen.toml, or\n  \
                 - Set [external_api].url in chipzen.toml for a verbatim URL."
            );
        };
        let env_name = env
            .map(|e| EnvName::parse(e).ok_or_else(|| anyhow::anyhow!("unknown env {e:?}")))
            .transpose()?;
        let conn = connect_to_chipzen(&bot_id, env_name, None, config.clone())
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        (conn.url, conn.env.map(|e| e.as_str().to_string()))
    };

    let token = resolve_token(explicit_token, config.as_ref());

    println!("Chipzen external-API connection");
    println!("{}", "=".repeat(50));
    println!("  lobby URL : {url}");
    if let Some(env) = resolved_env {
        println!("  env       : {env}");
    }
    println!(
        "  token     : {}",
        if token.is_some() {
            "present (cz_extbot_…)"
        } else {
            "MISSING — pass --token or set [external_api].token in chipzen.toml"
        }
    );
    if let Some(cfg) = config.as_ref().and_then(|c| c.path.as_ref()) {
        println!("  config    : {}", cfg.display());
    } else {
        println!("  config    : none found on the search path");
    }
    println!();
    if token.is_none() {
        bail!("no external-API token resolved — cannot run a remote-play session");
    }
    println!(
        "Setup looks good. Wire chipzen_bot::run_external_cli(|| MyBot, args) into your bot\n\
         binary's main to play (the scaffolded starter's `run-external` mode does this)."
    );
    Ok(())
}

fn run_init(name: &str, parent_dir: Option<PathBuf>) -> Result<()> {
    let opts = ScaffoldOptions { parent_dir };
    let created =
        scaffold_bot(name, &opts).with_context(|| format!("scaffolding project {name:?}"))?;
    println!("Created bot project: {}", created.display());
    println!();
    println!("Next steps:");
    println!("  cd {name}");
    println!("  cargo build");
    println!("  # Edit src/main.rs to implement your strategy");
    println!("  chipzen-sdk validate .");
    Ok(())
}

fn run_validate(path: &std::path::Path, max_size_mb: Option<u64>, no_color: bool) -> Result<()> {
    let opts = ValidateOptions {
        max_upload_bytes: max_size_mb
            .map(|mb| mb * 1024 * 1024)
            .unwrap_or(DEFAULT_MAX_UPLOAD_BYTES),
    };
    let results = validate_bot(path, &opts)?;
    print_results(&results, !no_color);

    let fails = results
        .iter()
        .filter(|r| matches!(r.severity, Severity::Fail))
        .count();
    if fails > 0 {
        std::process::exit(1);
    }
    Ok(())
}

fn print_results(results: &[crate::validate::ValidationResult], color: bool) {
    let supports_color = color && std::io::IsTerminal::is_terminal(&std::io::stdout());
    let green = if supports_color { "\x1b[92m" } else { "" };
    let yellow = if supports_color { "\x1b[93m" } else { "" };
    let red = if supports_color { "\x1b[91m" } else { "" };
    let reset = if supports_color { "\x1b[0m" } else { "" };

    println!();
    println!("Chipzen Bot Validation");
    println!("{}", "=".repeat(50));
    for r in results {
        let icon = match r.severity {
            Severity::Pass => format!("{green}PASS{reset}"),
            Severity::Warn => format!("{yellow}WARN{reset}"),
            Severity::Fail => format!("{red}FAIL{reset}"),
        };
        println!("  [{icon}] {}: {}", r.name, r.message);
    }

    println!();
    let fails = results
        .iter()
        .filter(|r| matches!(r.severity, Severity::Fail))
        .count();
    if fails > 0 {
        let plural = if fails == 1 { "" } else { "s" };
        println!("{red}{fails} check{plural} failed.{reset}");
    } else {
        println!("{green}All checks passed! Your bot is ready to upload.{reset}");
    }
}
