use chipzen_sdk::{scaffold_bot, validate_bot, ScaffoldOptions, Severity, ValidateOptions};
use std::fs;
use std::path::Path;
use tempfile::tempdir;

fn scaffolded(name: &str, parent: &Path) -> std::path::PathBuf {
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.to_path_buf()),
    };
    scaffold_bot(name, &opts).unwrap()
}

fn write(path: &Path, contents: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, contents).unwrap();
}

fn names_with_severity(results: &[chipzen_sdk::ValidationResult], sev: Severity) -> Vec<&str> {
    results
        .iter()
        .filter(|r| r.severity == sev)
        .map(|r| r.name.as_str())
        .collect()
}

#[test]
fn scaffolded_bot_passes_every_static_check() {
    let parent = tempdir().unwrap();
    let dir = scaffolded("scaffold_for_validate", parent.path());
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();

    let passes = names_with_severity(&results, Severity::Pass);
    let fails = names_with_severity(&results, Severity::Fail);
    assert!(fails.is_empty(), "expected no fails, got: {fails:?}");
    assert!(passes.contains(&"size"));
    assert!(passes.contains(&"file_structure"));
    assert!(passes.contains(&"cargo_metadata"));
    assert!(passes.contains(&"imports"));
    assert!(passes.contains(&"bot_impl"));
    assert!(passes.contains(&"decide_method"));
}

#[test]
fn validate_reports_missing_path() {
    let parent = tempdir().unwrap();
    let results = validate_bot(
        &parent.path().join("does-not-exist"),
        &ValidateOptions::default(),
    )
    .unwrap();
    assert_eq!(results[0].severity, Severity::Fail);
    assert_eq!(results[0].name, "file_structure");
}

#[test]
fn validate_reports_missing_cargo_toml() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("empty_dir");
    fs::create_dir(&dir).unwrap();
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let fails = names_with_severity(&results, Severity::Fail);
    assert!(fails.contains(&"file_structure"));
}

#[test]
fn validate_reports_missing_chipzen_bot_dependency() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("no_dep_bot");
    fs::create_dir(&dir).unwrap();
    write(
        &dir.join("Cargo.toml"),
        r#"[package]
name = "no_dep_bot"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = "1"
"#,
    );
    write(
        &dir.join("src").join("main.rs"),
        "fn main() {} impl Bot for X {} fn decide() {}",
    );
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let imports_fail = results
        .iter()
        .find(|r| r.name == "imports" && r.severity == Severity::Fail)
        .expect("expected imports fail when chipzen-bot is missing");
    assert!(imports_fail.message.contains("chipzen-bot"));
}

#[test]
fn validate_reports_blocked_dependencies() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("blocked_deps");
    fs::create_dir(&dir).unwrap();
    write(
        &dir.join("Cargo.toml"),
        r#"[package]
name = "blocked_deps"
version = "0.1.0"
edition = "2021"

[dependencies]
chipzen-bot = "0.2"
pnet = "0.34"
"#,
    );
    write(
        &dir.join("src").join("main.rs"),
        "use chipzen_bot::*; impl Bot for X {} fn decide() {}",
    );
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let imports = results.iter().find(|r| r.name == "imports").unwrap();
    assert_eq!(imports.severity, Severity::Fail);
    assert!(imports.message.contains("pnet"));
}

#[test]
fn validate_warns_on_fs_friendly_dependencies_without_failing_run() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("warn_deps");
    fs::create_dir(&dir).unwrap();
    write(
        &dir.join("Cargo.toml"),
        r#"[package]
name = "warn_deps"
version = "0.1.0"
edition = "2021"

[dependencies]
chipzen-bot = "0.2"
tempfile = "3"
"#,
    );
    write(
        &dir.join("src").join("main.rs"),
        "impl Bot for X {} fn decide() {}",
    );
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let warns = names_with_severity(&results, Severity::Warn);
    assert!(warns.contains(&"imports"));
    assert!(names_with_severity(&results, Severity::Fail).is_empty());
}

#[test]
fn validate_reports_missing_bot_impl() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("no_bot_impl");
    fs::create_dir(&dir).unwrap();
    write(
        &dir.join("Cargo.toml"),
        r#"[package]
name = "no_bot_impl"
version = "0.1.0"
edition = "2021"

[dependencies]
chipzen-bot = "0.2"
"#,
    );
    write(
        &dir.join("src").join("main.rs"),
        "// Just a comment about Bot for ... but no actual impl.\nfn main() {}",
    );
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let bot_impl = results.iter().find(|r| r.name == "bot_impl").unwrap();
    assert_eq!(bot_impl.severity, Severity::Fail);
}

#[test]
fn validate_reports_missing_decide_method() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("no_decide");
    fs::create_dir(&dir).unwrap();
    write(
        &dir.join("Cargo.toml"),
        r#"[package]
name = "no_decide"
version = "0.1.0"
edition = "2021"

[dependencies]
chipzen-bot = "0.2"
"#,
    );
    write(
        &dir.join("src").join("main.rs"),
        "impl Bot for X { /* fn decide -- in a comment, not real */ }",
    );
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let decide = results.iter().find(|r| r.name == "decide_method").unwrap();
    assert_eq!(decide.severity, Severity::Fail);
}

#[test]
fn validate_handles_unparseable_cargo_toml() {
    let parent = tempdir().unwrap();
    let dir = parent.path().join("bad_toml");
    fs::create_dir(&dir).unwrap();
    write(&dir.join("Cargo.toml"), "this is not valid TOML {{{");
    write(&dir.join("src").join("main.rs"), "fn main() {}");
    let results = validate_bot(&dir, &ValidateOptions::default()).unwrap();
    let fails = names_with_severity(&results, Severity::Fail);
    assert!(fails.contains(&"cargo_metadata"));
}
