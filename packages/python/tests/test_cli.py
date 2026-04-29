"""Tests for the chipzen-sdk CLI commands (init, validate)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.__main__ import main
from chipzen.scaffold import scaffold_bot
from chipzen.validate import validate_bot


class TestInitCli:
    """Tests for the `chipzen-sdk init` command."""

    def test_scaffold_creates_runnable_bot(self, tmp_path):
        """Scaffolded bot can be imported and called."""
        bot_dir = scaffold_bot("cli_test_bot", parent_dir=tmp_path)
        main_py = bot_dir / "main.py"
        assert main_py.exists()

        # Dynamically import and instantiate
        import importlib.util

        spec = importlib.util.spec_from_file_location("cli_test_bot_main", str(main_py))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        bot_cls = getattr(mod, "MyBot")
        bot = bot_cls()

        from chipzen.models import Action, Card, GameState

        state = GameState(
            hand_number=1,
            phase="preflop",
            hole_cards=[Card.from_str("Ah"), Card.from_str("Kd")],
            pot=150,
            your_stack=9900,
            to_call=50,
            min_raise=200,
            max_raise=9900,
            valid_actions=["fold", "call", "raise"],
        )
        action = bot.decide(state)
        assert isinstance(action, Action)
        assert action.action in ("fold", "call", "raise")

    def test_scaffold_validate_roundtrip(self, tmp_path):
        """Scaffolded bot passes all validation checks."""
        bot_dir = scaffold_bot("roundtrip_bot", parent_dir=tmp_path)
        results = validate_bot(bot_dir)
        failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert failures == [], f"Validation failures: {failures}"

    def test_scaffold_respects_parent_dir(self, tmp_path):
        subdir = tmp_path / "projects"
        subdir.mkdir()
        bot_dir = scaffold_bot("nested_bot", parent_dir=subdir)
        assert bot_dir.parent == subdir


class TestValidateCli:
    """Tests for the `chipzen-sdk validate` command."""

    def test_validate_empty_dir_fails(self, tmp_path):
        results = validate_bot(tmp_path)
        severities = [sev for sev, _, _ in results]
        assert "fail" in severities

    def test_validate_custom_entry_point(self, tmp_path):
        code = (
            "from chipzen import Bot, GameState, Action\n\n"
            "class CustomBot(Bot):\n"
            "    def decide(self, state: GameState) -> Action:\n"
            "        return Action.fold()\n"
        )
        (tmp_path / "custom_entry.py").write_text(code)
        results = validate_bot(tmp_path, entry_point="custom_entry.py")
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities

    def test_validate_zip_with_nested_dir(self, tmp_path):
        """Zip with a single subdirectory should still pass."""
        import zipfile

        code = (
            "from chipzen import Bot, GameState, Action\n\n"
            "class ZipBot(Bot):\n"
            "    def decide(self, state: GameState) -> Action:\n"
            "        return Action.check()\n"
        )
        zip_path = tmp_path / "nested.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("my_bot/main.py", code)
        results = validate_bot(zip_path)
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities


class TestMainEntryPoint:
    """Tests for chipzen.__main__.main()."""

    def test_help_flag(self, capsys):
        """--help should print usage and exit 0."""
        original_argv = sys.argv
        try:
            sys.argv = ["chipzen-sdk", "--help"]
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
            captured = capsys.readouterr()
            assert "chipzen-sdk" in captured.out.lower() or "chipzen" in captured.out.lower()
        finally:
            sys.argv = original_argv

    def test_unknown_command(self, capsys):
        """Unknown command should print help and exit 1."""
        original_argv = sys.argv
        try:
            sys.argv = ["chipzen-sdk", "nonexistent"]
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
        finally:
            sys.argv = original_argv
