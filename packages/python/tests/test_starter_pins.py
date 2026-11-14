"""Tests for ``scripts/check_starter_pins.py`` (chipzen-sdk#218)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_starter_pins.py"
_spec = importlib.util.spec_from_file_location("check_starter_pins", SCRIPT)
pins = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pins
_spec.loader.exec_module(pins)


def _v(text):
    return pins.parse_version(text)


def _tree(root, py="0.6.1", js="0.5.1", rs="0.3.2", py_pin="~=0.6", js_pin="^0.5.1", rs_pin="0.3"):
    """Write the smallest repo layout the guard reads: one package and starter per language."""
    files = {
        "packages/python/pyproject.toml": f'[project]\nversion = "{py}"\n',
        "packages/python/starters/python/requirements.txt": f"chipzen-bot{py_pin}\n",
        "packages/javascript/package.json": json.dumps({"version": js}),
        "packages/javascript/starters/javascript/package.json": json.dumps(
            {"dependencies": {"@chipzen-ai/bot": js_pin}}
        ),
        "packages/rust/Cargo.toml": f'[workspace.package]\nversion = "{rs}"\n',
        "packages/rust/starters/rust/Cargo.toml": f'[dependencies]\nchipzen-bot = "{rs_pin}"\n',
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "pin,version,expected",
    [
        ("^0.3.0", "0.3.9", True),
        ("^0.3.0", "0.5.1", False),  # the stuck pin issue #218 reports
        ("^0.5.1", "0.5.1", True),
        ("^0.5.1", "0.5.0", False),
        ("^0.5.0", "0.6.0", False),
        ("^1.2.0", "1.9.0", True),
        ("^1.2.0", "2.0.0", False),
        ("^0.0.3", "0.0.3", True),
        ("^0.0.3", "0.0.4", False),
        ("^0.5", "0.5.9", True),
        ("~0.5.1", "0.5.9", True),
        ("~0.5.1", "0.6.0", False),
        ("~1", "1.9.0", True),
        ("0.5.1", "0.5.1", True),
        ("0.5.1", "0.5.2", False),
        (">=0.5.1 <0.7", "0.6.9", True),
        (">=0.5.1 <0.7", "0.7.0", False),
        ("^0.5.1 || ^0.6.0", "0.5.1", True),  # the bridge a minor-bump PR uses
        ("^0.5.1 || ^0.6.0", "0.6.0", True),
        ("^0.5.1 || ^0.6.0", "0.7.0", False),
    ],
)
def test_npm_ranges(pin, version, expected):
    assert pins.npm_admits(pin, _v(version)) is expected


@pytest.mark.parametrize(
    "pin,version,expected",
    [
        ("~=0.3", "0.6.1", True),  # not stuck, as issue #218 says, just a misleading floor
        ("~=0.6", "0.6.1", True),
        ("~=0.6", "0.9.0", True),
        ("~=0.6", "0.5.1", False),
        ("~=0.6", "1.0.0", False),
        ("~=0.6.1", "0.6.1", True),
        ("~=0.6.1", "0.7.0", False),
        ("==0.6.1", "0.6.1", True),
        ("==0.6.1", "0.6.2", False),
        (">=0.6,<2", "1.0.0", True),
        (">=0.6,<2", "2.0.0", False),
        (">=0.6,!=0.6.1", "0.6.1", False),
    ],
)
def test_pep440_specifiers(pin, version, expected):
    assert pins.pep440_admits(pin, _v(version)) is expected


@pytest.mark.parametrize(
    "pin,version,expected",
    [
        ("0.3", "0.3.2", True),  # bare means caret in Cargo, exact in npm
        ("0.3", "0.4.0", False),
        ("0.3", "0.2.9", False),
        ("^0.3", "0.3.2", True),
        ("~0.3.1", "0.3.9", True),
        (">=0.3, <0.5", "0.4.0", True),
        (">=0.3, <0.5", "0.5.0", False),
        ("1", "1.9.0", True),
    ],
)
def test_cargo_requirements(pin, version, expected):
    assert pins.cargo_admits(pin, _v(version)) is expected


@pytest.mark.parametrize(
    "admits,pin",
    [
        (pins.npm_admits, "latest"),
        (pins.npm_admits, "*"),
        (pins.npm_admits, "^0.5.x"),
        (pins.npm_admits, "workspace:*"),
        (pins.npm_admits, "file:../.."),
        (pins.npm_admits, "^0.5.1 ||"),
        (pins.npm_admits, "=0.5"),
        (pins.npm_admits, "0.5"),
        (pins.npm_admits, "^0.3.0 latest"),  # an unreadable clause after a miss still errors
        (pins.pep440_admits, "0.6"),
        (pins.pep440_admits, "~=1"),
        (pins.pep440_admits, "==0.6.*"),
        (pins.pep440_admits, "[extra]~=0.6"),
        (pins.cargo_admits, "*"),
        (pins.cargo_admits, "0.3.*"),
    ],
)
def test_a_pin_outside_the_grammar_is_an_error_not_a_pass(admits, pin):
    with pytest.raises(pins.UnreadablePinError):
        admits(pin, _v("0.5.1"))


def test_the_repo_as_committed_passes():
    passed, failed = pins.check()
    assert failed == []
    assert len(passed) >= 7, passed  # poker in all three, plus the Big Bad Bots starters


def test_the_javascript_reference_bot_is_checked():
    passed, _ = pins.check()
    assert any(line.startswith("examples/reference-bot-js/package.json:") for line in passed)


def test_a_stale_reference_bot_pin_fails(tmp_path):
    _tree(tmp_path)
    example = tmp_path / "examples/reference-bot-js/package.json"
    example.parent.mkdir(parents=True)
    example.write_text(json.dumps({"dependencies": {"@chipzen-ai/bot": "^0.3.0"}}), "utf-8")
    _, failed = pins.check(tmp_path)
    assert len(failed) == 1
    assert failed[0].startswith("examples/reference-bot-js/package.json: ^0.3.0 does not admit")


def test_a_clean_tree_passes(tmp_path):
    passed, failed = pins.check(_tree(tmp_path))
    assert failed == []
    assert len(passed) == 3


@pytest.mark.parametrize(
    "bump,starter",
    [
        ({"py": "1.0.0"}, "packages/python/starters/python/requirements.txt"),
        ({"js": "0.6.0"}, "packages/javascript/starters/javascript/package.json"),
        ({"rs": "0.4.0"}, "packages/rust/starters/rust/Cargo.toml"),
    ],
)
def test_a_version_bump_that_leaves_a_starter_behind_fails(tmp_path, bump, starter):
    _, failed = pins.check(_tree(tmp_path, **bump))
    assert len(failed) == 1
    assert failed[0].startswith(f"{starter}:")
    assert "does not admit" in failed[0]


def test_the_bridge_range_passes_a_minor_bump_before_it_is_published(tmp_path):
    _, failed = pins.check(_tree(tmp_path, js="0.6.0", js_pin="^0.5.1 || ^0.6.0"))
    assert failed == []


def test_a_new_starter_is_found_without_being_listed(tmp_path):
    _tree(tmp_path)
    new = tmp_path / "packages/javascript/starters/bigbadbots/newbot/package.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"dependencies": {"@chipzen-ai/bot": "^0.3.0"}}), encoding="utf-8")
    _, failed = pins.check(tmp_path)
    assert len(failed) == 1
    assert "newbot" in failed[0]


def test_a_checkout_under_a_build_dir_name_still_finds_its_starters(tmp_path):
    passed, failed = pins.check(_tree(tmp_path / "target" / "venv"))
    assert failed == []
    assert len(passed) == 3


def test_installed_dependencies_are_not_mistaken_for_starters(tmp_path):
    _tree(tmp_path)
    vendored = tmp_path / "packages/javascript/starters/javascript/node_modules/ws/package.json"
    vendored.parent.mkdir(parents=True)
    vendored.write_text(json.dumps({"version": "8.0.0"}), encoding="utf-8")
    _, failed = pins.check(tmp_path)
    assert failed == []


@pytest.mark.parametrize(
    "starter,text",
    [
        ("packages/python/starters/python/requirements.txt", "numpy>=1.20\n"),
        ("packages/python/starters/python/requirements.txt", "chipzen-bot\n"),
        ("packages/javascript/starters/javascript/package.json", '{"dependencies": {}}'),
        ("packages/javascript/starters/javascript/package.json", "{not json"),
        ("packages/rust/starters/rust/Cargo.toml", "[dependencies]\n"),
        ("packages/rust/starters/rust/Cargo.toml", '[dependencies]\nchipzen-bot = {path = ".."}\n'),
        ("packages/rust/starters/rust/Cargo.toml", "[dependencies\n"),
    ],
)
def test_a_starter_without_a_readable_pin_fails(tmp_path, starter, text):
    _tree(tmp_path)
    (tmp_path / starter).write_text(text, encoding="utf-8")
    _, failed = pins.check(tmp_path)
    assert len(failed) == 1
    assert failed[0].startswith(f"{starter}:")


def test_an_empty_starters_dir_fails(tmp_path):
    _tree(tmp_path)
    (tmp_path / "packages/rust/starters/rust/Cargo.toml").unlink()
    _, failed = pins.check(tmp_path)
    assert len(failed) == 1
    assert "no Cargo.toml found" in failed[0]


def test_a_pre_release_package_version_fails_loudly(tmp_path):
    _, failed = pins.check(_tree(tmp_path, js="0.6.0-rc.1"))
    assert len(failed) == 1
    assert failed[0].startswith("packages/javascript/package.json:")


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    real_check = pins.check
    good = _tree(tmp_path / "good")
    monkeypatch.setattr(pins, "check", lambda: real_check(good))
    assert pins.main() == 0
    bad = _tree(tmp_path / "bad", js="0.6.0")
    monkeypatch.setattr(pins, "check", lambda: real_check(bad))
    assert pins.main() == 1
    assert "does not admit 0.6.0" in capsys.readouterr().err
