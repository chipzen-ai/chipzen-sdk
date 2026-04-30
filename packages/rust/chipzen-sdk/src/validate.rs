//! `chipzen-sdk validate <path>` — pre-upload conformance checks.
//!
//! Mirrors the Python and JavaScript validators' check shape and
//! severity model so a `(severity, name, message)` tuple from any of
//! the three SDKs renders identically in client tooling.
//!
//! Smoke-test / conformance scenarios are deferred to Phase 3 PR 3
//! (the conformance harness needs the IP-protected starter to land
//! first so it knows what binary shape to drive).

use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::HashSet;
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Pass,
    Warn,
    Fail,
}

#[derive(Debug, Clone)]
pub struct ValidationResult {
    pub severity: Severity,
    pub name: String,
    pub message: String,
}

#[derive(Debug, Clone)]
pub struct ValidateOptions {
    /// Hard-fail upload size threshold, in bytes. Defaults to 500 MB
    /// (platform cap).
    pub max_upload_bytes: u64,
}

impl Default for ValidateOptions {
    fn default() -> Self {
        Self {
            max_upload_bytes: DEFAULT_MAX_UPLOAD_BYTES,
        }
    }
}

pub const DEFAULT_MAX_UPLOAD_BYTES: u64 = 500 * 1024 * 1024;

/// Crates whose presence in `Cargo.toml` indicates a class of bot we
/// don't allow. Mirrors the BLOCKED_MODULES sets in the Python and
/// JavaScript validators; not exhaustive — caught here as a fast
/// pre-flight. The platform sandbox is the authoritative gate.
const BLOCKED_DEPS: &[&str] = &[
    // Process spawning / OS escape
    "subprocess", // (placeholder; cargo deps don't usually have this name)
    "duct",
    "command-group",
    // Raw socket / packet-level networking
    "pnet",
    "pcap",
    "raw_socket",
];

const WARN_DEPS: &[&str] = &[
    // FS access — usable but the sandbox is restrictive about
    // reads/writes outside /bot/.
    "tempfile", "tempdir", "memmap2",
];

pub fn validate_bot(path: &Path, opts: &ValidateOptions) -> Result<Vec<ValidationResult>> {
    let mut results = Vec::new();

    let metadata = match fs::metadata(path) {
        Ok(m) => m,
        Err(_) => {
            results.push(fail(
                "file_structure",
                format!("Path not found: {}", path.display()),
            ));
            return Ok(results);
        }
    };
    if !metadata.is_dir() {
        results.push(fail(
            "file_structure",
            format!("Path is not a directory: {}", path.display()),
        ));
        return Ok(results);
    }

    results.extend(check_size(path, opts.max_upload_bytes)?);

    let cargo_toml_path = path.join("Cargo.toml");
    let main_rs = path.join("src").join("main.rs");
    let lib_rs = path.join("src").join("lib.rs");
    if !cargo_toml_path.is_file() {
        results.push(fail(
            "file_structure",
            "Cargo.toml not found in project root".to_string(),
        ));
        return Ok(results);
    }
    if !main_rs.is_file() && !lib_rs.is_file() {
        results.push(fail(
            "file_structure",
            "Neither src/main.rs nor src/lib.rs found".to_string(),
        ));
        return Ok(results);
    }
    results.push(pass(
        "file_structure",
        format!(
            "Cargo.toml + {} present",
            if main_rs.is_file() {
                "src/main.rs"
            } else {
                "src/lib.rs"
            }
        ),
    ));

    let cargo_text = fs::read_to_string(&cargo_toml_path).context("reading Cargo.toml")?;
    let manifest: CargoManifest = match toml::from_str(&cargo_text) {
        Ok(m) => m,
        Err(e) => {
            results.push(fail(
                "cargo_metadata",
                format!("Cargo.toml is not parseable: {e}"),
            ));
            return Ok(results);
        }
    };

    results.extend(check_cargo_metadata(&manifest));
    results.extend(check_dependencies(&manifest));

    let entry = if main_rs.is_file() { main_rs } else { lib_rs };
    let source =
        fs::read_to_string(&entry).with_context(|| format!("reading {}", entry.display()))?;
    results.push(check_bot_impl(&source));
    results.push(check_decide_method(&source));

    Ok(results)
}

// ---------------------------------------------------------------------------
// Per-check implementations
// ---------------------------------------------------------------------------

fn check_size(dir: &Path, max_bytes: u64) -> Result<Vec<ValidationResult>> {
    let total = dir_total_bytes(dir)?;
    let mb = total as f64 / (1024.0 * 1024.0);
    let limit_mb = max_bytes / (1024 * 1024);
    if total > max_bytes {
        return Ok(vec![fail(
            "size",
            format!("Directory is {mb:.1} MB, exceeds {limit_mb} MB upload limit"),
        )]);
    }
    Ok(vec![pass(
        "size",
        format!("Size OK ({mb:.1} MB uncompressed / {limit_mb} MB limit)"),
    )])
}

fn check_cargo_metadata(manifest: &CargoManifest) -> Vec<ValidationResult> {
    let mut out = Vec::new();
    let pkg = match manifest.package.as_ref() {
        Some(p) => p,
        None => {
            out.push(fail(
                "cargo_metadata",
                "Cargo.toml is missing the [package] table".to_string(),
            ));
            return out;
        }
    };
    if pkg.name.is_empty() {
        out.push(fail("cargo_metadata", "package.name is empty".to_string()));
    } else {
        out.push(pass(
            "cargo_metadata",
            format!("package.name = {:?}, version = {:?}", pkg.name, pkg.version),
        ));
    }
    out
}

fn check_dependencies(manifest: &CargoManifest) -> Vec<ValidationResult> {
    let mut out = Vec::new();
    let deps: HashSet<&str> = manifest.dependencies.keys().map(String::as_str).collect();

    if !deps.contains("chipzen-bot") {
        out.push(fail(
            "imports",
            "chipzen-bot dependency missing from Cargo.toml — add `chipzen-bot = \"0.2\"`"
                .to_string(),
        ));
        return out;
    }

    let blocked: Vec<&&str> = BLOCKED_DEPS.iter().filter(|d| deps.contains(*d)).collect();
    if !blocked.is_empty() {
        let names: Vec<String> = blocked.iter().map(|d| (***d).to_string()).collect();
        out.push(fail(
            "imports",
            format!(
                "Blocked dependencies detected in Cargo.toml: {}",
                names.join(", ")
            ),
        ));
    } else {
        out.push(pass(
            "imports",
            "No blocked dependencies detected".to_string(),
        ));
    }

    for w in WARN_DEPS.iter().filter(|d| deps.contains(*d)) {
        out.push(warn(
            "imports",
            format!("Depends on {w:?} — usable but the platform sandbox restricts what it can do"),
        ));
    }
    out
}

fn check_bot_impl(source: &str) -> ValidationResult {
    // Look for `impl Bot for X` (with or without a path prefix). We
    // strip line and block comments first so a comment about Bot
    // doesn't false-match.
    let stripped = strip_comments(source);
    let re_present = stripped
        .lines()
        .any(|l| l.contains("impl") && l.contains("Bot for"));
    if re_present {
        pass("bot_impl", "impl Bot for ... found".to_string())
    } else {
        fail(
            "bot_impl",
            "No `impl Bot for ...` found in entry point".to_string(),
        )
    }
}

fn check_decide_method(source: &str) -> ValidationResult {
    // Imperfect (regex-based) — a smoke test in PR 3 will catch the
    // actual runtime case via the conformance harness. For now,
    // verify the symbol appears outside comments.
    let stripped = strip_comments(source);
    if stripped.contains("fn decide") {
        pass("decide_method", "fn decide(...) found".to_string())
    } else {
        fail(
            "decide_method",
            "Entry point does not implement fn decide(...)".to_string(),
        )
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn pass(name: &str, message: String) -> ValidationResult {
    ValidationResult {
        severity: Severity::Pass,
        name: name.to_string(),
        message,
    }
}

fn warn(name: &str, message: String) -> ValidationResult {
    ValidationResult {
        severity: Severity::Warn,
        name: name.to_string(),
        message,
    }
}

fn fail(name: &str, message: String) -> ValidationResult {
    ValidationResult {
        severity: Severity::Fail,
        name: name.to_string(),
        message,
    }
}

fn strip_comments(source: &str) -> String {
    // Strip /* ... */ block comments and // ... line comments. Doesn't
    // try to be string-literal-aware — false positives there are
    // harmless for the regex-ish checks that come after.
    let no_block = strip_block_comments(source);
    no_block
        .lines()
        .map(|l| {
            // Find // outside of a string literal — for an alpha tier
            // check we just drop everything after the first // that
            // isn't inside the trivial `"..."` window. Good enough.
            match l.find("//") {
                Some(idx) => &l[..idx],
                None => l,
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn strip_block_comments(source: &str) -> String {
    let mut out = String::with_capacity(source.len());
    let mut chars = source.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if c == '/' && source[i..].starts_with("/*") {
            chars.next(); // consume '*'
            while let Some((_, c2)) = chars.next() {
                if c2 == '*' && chars.peek().is_some_and(|(_, n)| *n == '/') {
                    chars.next();
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

fn dir_total_bytes(dir: &Path) -> Result<u64> {
    let mut total: u64 = 0;
    for entry in fs::read_dir(dir).with_context(|| format!("reading {}", dir.display()))? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name == "target" || name == ".git" {
            continue;
        }
        let ft = entry.file_type()?;
        let path = entry.path();
        if ft.is_dir() {
            total = total.saturating_add(dir_total_bytes(&path)?);
        } else if ft.is_file() {
            total = total.saturating_add(entry.metadata()?.len());
        }
    }
    Ok(total)
}

// ---------------------------------------------------------------------------
// Cargo.toml deserialization (only the fields we care about)
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct CargoManifest {
    package: Option<PackageMetadata>,
    #[serde(default)]
    dependencies: std::collections::BTreeMap<String, toml::Value>,
}

#[derive(Debug, Deserialize)]
struct PackageMetadata {
    name: String,
    #[serde(default)]
    version: String,
}
