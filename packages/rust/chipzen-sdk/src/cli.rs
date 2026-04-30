//! Argument parsing + command dispatch for `chipzen-sdk`.

use crate::scaffold::{scaffold_bot, ScaffoldOptions};
use crate::validate::{validate_bot, Severity, ValidateOptions, DEFAULT_MAX_UPLOAD_BYTES};
use anyhow::{Context, Result};
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
    Validate {
        /// Path to the bot project directory.
        path: PathBuf,
        /// Override max upload size in MB (default: 500).
        #[arg(long)]
        max_size_mb: Option<u64>,
        /// Disable colored output.
        #[arg(long)]
        no_color: bool,
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
    }
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
