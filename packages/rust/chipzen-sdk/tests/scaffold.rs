use chipzen_sdk::{scaffold_bot, ScaffoldOptions};
use std::fs;
use tempfile::tempdir;

#[test]
fn scaffold_creates_project_directory() {
    let parent = tempdir().unwrap();
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.path().to_path_buf()),
    };
    let dir = scaffold_bot("alpha_bot", &opts).unwrap();
    assert_eq!(dir, parent.path().join("alpha_bot"));
    assert!(dir.is_dir());
}

#[test]
fn scaffold_emits_all_expected_files() {
    let parent = tempdir().unwrap();
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.path().to_path_buf()),
    };
    let dir = scaffold_bot("beta_bot", &opts).unwrap();
    for name in [
        "Cargo.toml",
        "src/main.rs",
        "Dockerfile",
        ".dockerignore",
        ".gitignore",
        "README.md",
    ] {
        assert!(dir.join(name).is_file(), "missing scaffolded file: {name}");
    }
}

#[test]
fn scaffolded_cargo_toml_depends_on_chipzen_bot() {
    let parent = tempdir().unwrap();
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.path().to_path_buf()),
    };
    let dir = scaffold_bot("gamma_bot", &opts).unwrap();
    let cargo_toml = fs::read_to_string(dir.join("Cargo.toml")).unwrap();
    assert!(cargo_toml.contains("name = \"gamma_bot\""));
    assert!(cargo_toml.contains("chipzen-bot"));
    assert!(cargo_toml.contains("edition = \"2021\""));
    assert!(cargo_toml.contains("tokio"));
}

#[test]
fn scaffolded_main_rs_implements_bot() {
    let parent = tempdir().unwrap();
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.path().to_path_buf()),
    };
    let dir = scaffold_bot("delta_bot", &opts).unwrap();
    let source = fs::read_to_string(dir.join("src").join("main.rs")).unwrap();
    assert!(source.contains("use chipzen_bot::"));
    assert!(source.contains("impl Bot for"));
    assert!(source.contains("fn decide"));
    assert!(source.contains("#[tokio::main]"));
    assert!(source.contains("run_bot"));
}

#[test]
fn scaffold_rejects_invalid_project_names() {
    let parent = tempdir().unwrap();
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.path().to_path_buf()),
    };
    assert!(scaffold_bot("has spaces", &opts).is_err());
    assert!(scaffold_bot("with/slash", &opts).is_err());
    assert!(scaffold_bot("", &opts).is_err());
}

#[test]
fn scaffold_refuses_to_clobber_existing_directory() {
    let parent = tempdir().unwrap();
    let opts = ScaffoldOptions {
        parent_dir: Some(parent.path().to_path_buf()),
    };
    scaffold_bot("epsilon_bot", &opts).unwrap();
    let err = scaffold_bot("epsilon_bot", &opts).unwrap_err();
    assert!(format!("{err}").contains("already exists"));
}
