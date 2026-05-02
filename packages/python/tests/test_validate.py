"""Tests for the chipzen SDK bot validation."""

import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.validate import (
    ALLOWED_PACKAGES,
    BLOCKED_MODULES,
    validate_bot,
)


@pytest.fixture
def tmp_bot(tmp_path):
    """Create a minimal valid bot in a temp directory."""
    code = textwrap.dedent("""\
        from chipzen import Bot, GameState, Action

        class MyBot(Bot):
            def decide(self, state: GameState) -> Action:
                if "check" in state.valid_actions:
                    return Action.check()
                return Action.fold()
    """)
    (tmp_path / "main.py").write_text(code)
    return tmp_path


@pytest.fixture
def bot_with_requirements(tmp_bot):
    """A valid bot with a requirements.txt."""
    (tmp_bot / "requirements.txt").write_text("numpy>=1.20\n")
    return tmp_bot


class TestValidBot:
    def test_valid_bot_passes_all_checks(self, tmp_bot):
        results = validate_bot(tmp_bot)
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities
        check_names = [name for _, name, _ in results]
        assert "file_structure" in check_names
        assert "syntax" in check_names
        assert "imports" in check_names
        assert "bot_class" in check_names
        assert "decide_method" in check_names
        assert "smoke_test" in check_names
        assert "timeout" in check_names

    def test_valid_bot_with_bot_py(self, tmp_path):
        code = textwrap.dedent("""\
            from chipzen import Bot, GameState, Action

            class TestBot(Bot):
                def decide(self, state: GameState) -> Action:
                    return Action.call()
        """)
        (tmp_path / "bot.py").write_text(code)
        results = validate_bot(tmp_path)
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities


class TestFileStructure:
    def test_missing_entry_point(self, tmp_path):
        (tmp_path / "something.py").write_text("x = 1")
        results = validate_bot(tmp_path)
        fails = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert any(name == "file_structure" for name, _ in fails)

    def test_custom_entry_point(self, tmp_path):
        code = textwrap.dedent("""\
            from chipzen import Bot, GameState, Action

            class CustomBot(Bot):
                def decide(self, state: GameState) -> Action:
                    return Action.fold()
        """)
        (tmp_path / "custom.py").write_text(code)
        results = validate_bot(tmp_path, entry_point="custom.py")
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities


class TestSyntaxCheck:
    def test_syntax_error_fails(self, tmp_path):
        (tmp_path / "main.py").write_text("def broken(:\n  pass")
        results = validate_bot(tmp_path)
        fails = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert any(name == "syntax" for name, _ in fails)


class TestImportCheck:
    def test_blocked_import_fails(self, tmp_path):
        code = textwrap.dedent("""\
            import subprocess
            from chipzen import Bot, GameState, Action

            class MyBot(Bot):
                def decide(self, state: GameState) -> Action:
                    return Action.fold()
        """)
        (tmp_path / "main.py").write_text(code)
        results = validate_bot(tmp_path)
        import_results = [(sev, msg) for sev, name, msg in results if name == "imports"]
        assert any(sev == "fail" for sev, _ in import_results)

    def test_warn_import_warns(self, tmp_path):
        code = textwrap.dedent("""\
            import os
            from chipzen import Bot, GameState, Action

            class MyBot(Bot):
                def decide(self, state: GameState) -> Action:
                    return Action.fold()
        """)
        (tmp_path / "main.py").write_text(code)
        results = validate_bot(tmp_path)
        import_results = [(sev, msg) for sev, name, msg in results if name == "imports"]
        assert any(sev == "warn" for sev, _ in import_results)

    def test_clean_imports_pass(self, tmp_bot):
        results = validate_bot(tmp_bot)
        import_results = [(sev, msg) for sev, name, msg in results if name == "imports"]
        assert all(sev == "pass" for sev, _ in import_results)


class TestBotClassCheck:
    def test_no_bot_class_fails(self, tmp_path):
        code = textwrap.dedent("""\
            class NotABot:
                def decide(self, state):
                    pass
        """)
        (tmp_path / "main.py").write_text(code)
        results = validate_bot(tmp_path)
        fails = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert any(name == "bot_class" for name, _ in fails)


class TestDecideMethodCheck:
    def test_missing_decide_fails(self, tmp_path):
        code = textwrap.dedent("""\
            from chipzen import Bot, GameState, Action

            class MyBot(Bot):
                def on_hand_start(self, hand_number, hole_cards):
                    pass
        """)
        (tmp_path / "main.py").write_text(code)
        results = validate_bot(tmp_path)
        fails = [(name, msg) for sev, name, msg in results if sev == "fail"]
        assert any(name == "decide_method" for name, _ in fails)


class TestRequirementsCheck:
    def test_allowed_packages_pass(self, bot_with_requirements):
        results = validate_bot(bot_with_requirements)
        req_results = [(sev, msg) for sev, name, msg in results if name == "requirements"]
        assert all(sev == "pass" for sev, _ in req_results)

    def test_disallowed_package_warns(self, tmp_bot):
        (tmp_bot / "requirements.txt").write_text("pandas>=1.0\n")
        results = validate_bot(tmp_bot)
        req_results = [(sev, msg) for sev, name, msg in results if name == "requirements"]
        assert any(sev == "warn" for sev, _ in req_results)

    def test_comments_and_blanks_ignored(self, tmp_bot):
        (tmp_bot / "requirements.txt").write_text("# a comment\n\nnumpy\n")
        results = validate_bot(tmp_bot)
        req_results = [(sev, msg) for sev, name, msg in results if name == "requirements"]
        assert all(sev == "pass" for sev, _ in req_results)


class TestSmokeTest:
    def test_bot_that_returns_wrong_type_fails(self, tmp_path):
        code = textwrap.dedent("""\
            from chipzen import Bot, GameState, Action

            class BadBot(Bot):
                def decide(self, state: GameState):
                    return "fold"  # wrong type!
        """)
        (tmp_path / "main.py").write_text(code)
        results = validate_bot(tmp_path)
        smoke_results = [(sev, msg) for sev, name, msg in results if name == "smoke_test"]
        assert any(sev == "fail" for sev, _ in smoke_results)


class TestSizeCheck:
    def test_small_directory_passes(self, tmp_bot):
        results = validate_bot(tmp_bot)
        size_results = [(sev, msg) for sev, name, msg in results if name == "size"]
        assert all(sev == "pass" for sev, _ in size_results)

    def test_oversized_directory_fails(self, tmp_bot):
        results = validate_bot(tmp_bot, max_upload_bytes=10)  # 10 bytes
        size_results = [(sev, msg) for sev, name, msg in results if name == "size"]
        assert any(sev == "fail" for sev, _ in size_results)


class TestZipValidation:
    def test_valid_zip(self, tmp_bot, tmp_path):
        import zipfile

        zip_path = tmp_path / "bot.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(tmp_bot / "main.py", "main.py")
        results = validate_bot(zip_path)
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities


class TestTimeoutCheck:
    def test_fast_bot_passes(self, tmp_bot):
        results = validate_bot(tmp_bot, timeout_warn_ms=5000)
        timeout_results = [(sev, msg) for sev, name, msg in results if name == "timeout"]
        assert all(sev == "pass" for sev, _ in timeout_results)
