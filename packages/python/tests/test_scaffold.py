"""Tests for ``chipzen-sdk init``: it writes the canonical Python starter (issue #260)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pathlib import Path

import pytest

from chipzen import scaffold
from chipzen.scaffold import STARTER_FILES, init_cli, scaffold_bot, starter_root
from chipzen.validate import validate_bot

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STARTER_DIR = PACKAGE_DIR / "starters" / "python"
_CACHE_NAMES = {"__pycache__"}


def _starter_files() -> set[str]:
    return {
        p.name
        for p in STARTER_DIR.iterdir()
        if p.name not in _CACHE_NAMES and not p.name.endswith((".pyc", ".so"))
    }


class TestNoDrift:
    """``init`` writes the starter byte for byte, so the two cannot drift again."""

    def test_starter_folder_holds_exactly_the_scaffolded_files(self):
        assert _starter_files() == set(STARTER_FILES), (
            "packages/python/starters/python and chipzen.scaffold.STARTER_FILES disagree: "
            "add the new file to STARTER_FILES and to the wheel's force-include in "
            "pyproject.toml, or remove it from the starter"
        )

    def test_wheel_force_include_ships_every_starter_file(self):
        pyproject = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text("utf-8"))
        mapping = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        expected = {
            f"starters/python/{name}": f"chipzen/{scaffold.PACKAGED_STARTER}/{name}"
            for name in STARTER_FILES
        }
        assert mapping == expected

    def test_init_output_is_byte_identical_to_the_starter(self, tmp_path):
        bot_dir = scaffold_bot("drift_bot", parent_dir=tmp_path)
        written = {p.name for p in bot_dir.iterdir()}
        assert written == set(STARTER_FILES)
        for name in STARTER_FILES:
            assert (bot_dir / name).read_bytes() == (STARTER_DIR / name).read_bytes(), name

    def test_source_checkout_reads_the_starter_folder(self):
        # This suite runs against src/ (an editable install has no package
        # data), so init must be reading the real starter folder.
        assert Path(str(starter_root())).resolve() == STARTER_DIR.resolve()


class TestScaffoldedProject:
    def test_has_entry_point_main_and_dockerfile(self, tmp_path):
        bot_dir = scaffold_bot("layout_bot", parent_dir=tmp_path)
        bot_py = (bot_dir / "bot.py").read_text("utf-8")
        assert "class MyBot(Bot)" in bot_py
        assert "\ndef main() -> None:" in bot_py
        dockerfile = (bot_dir / "Dockerfile").read_text("utf-8")
        assert 'ENTRYPOINT ["python", "-c", "from bot import main; main()"]' in dockerfile
        assert not (bot_dir / "main.py").exists(), "the pre-#260 layout is gone"

    def test_init_then_validate_passes(self, tmp_path):
        bot_dir = scaffold_bot("valid_bot", parent_dir=tmp_path)
        results = validate_bot(bot_dir)
        failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert failures == []
        names = {name for _, name, _ in results}
        assert {"dockerfile", "entry_main", "smoke_test"} <= names

    def test_init_then_validate_with_connectivity_passes(self, tmp_path):
        bot_dir = scaffold_bot("wire_bot", parent_dir=tmp_path)
        results = validate_bot(bot_dir, check_connectivity=True)
        failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert failures == []
        assert "connectivity_full_match" in {name for _, name, _ in results}

    def test_readme_has_none_of_the_stale_claims(self, tmp_path):
        readme = (scaffold_bot("readme_bot", parent_dir=tmp_path) / "README.md").read_text()
        lowered = readme.lower()
        assert ".zip" not in lowered and "zip and upload" not in lowered
        assert "built-in opponents" not in lowered
        assert "500ms decision timeout" not in lowered
        assert "docker save" in readme
        assert "5 seconds" in readme

    def test_gitignore_keeps_the_token_out(self, tmp_path):
        gitignore = (scaffold_bot("git_bot", parent_dir=tmp_path) / ".gitignore").read_text()
        assert "chipzen.toml" in gitignore
        assert "__pycache__" in gitignore


class TestScaffoldBehaviour:
    def test_existing_directory_raises(self, tmp_path):
        (tmp_path / "existing").mkdir()
        with pytest.raises(FileExistsError):
            scaffold_bot("existing", parent_dir=tmp_path)

    def test_default_parent_is_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bot_dir = scaffold_bot("cwd_bot")
        assert bot_dir.parent == tmp_path

    def test_missing_starter_fails_cleanly_and_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scaffold, "PACKAGED_STARTER", "_no_such_starter")
        monkeypatch.setattr(scaffold, "SOURCE_STARTER", tmp_path / "no-starter")
        with pytest.raises(FileNotFoundError, match="Reinstall"):
            scaffold_bot("broken_bot", parent_dir=tmp_path)
        assert not (tmp_path / "broken_bot").exists()

    def test_packaged_starter_is_preferred(self, tmp_path, monkeypatch):
        # A wheel install has chipzen/_starters/python; init must read that one.
        fake_pkg = tmp_path / "pkg"
        packaged = fake_pkg / "_starters" / "python"
        packaged.mkdir(parents=True)
        for name in STARTER_FILES:
            (packaged / name).write_text(f"packaged {name}\n")

        monkeypatch.setattr(scaffold.resources, "files", lambda _package: fake_pkg)
        bot_dir = scaffold_bot("wheel_bot", parent_dir=tmp_path / "out")
        assert (bot_dir / "bot.py").read_text() == "packaged bot.py\n"


class TestInitCli:
    def test_creates_project_and_prints_next_steps(self, tmp_path, capsys):
        init_cli(["cli_bot", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert (tmp_path / "cli_bot" / "Dockerfile").is_file()
        assert "chipzen-sdk validate ." in out
        assert "docker build" in out

    def test_existing_directory_exits_1(self, tmp_path, capsys):
        (tmp_path / "taken").mkdir()
        with pytest.raises(SystemExit) as exc_info:
            init_cli(["taken", "--dir", str(tmp_path)])
        assert exc_info.value.code == 1
        assert "already exists" in capsys.readouterr().err
