"""Tests for the chipzen SDK bot scaffolding."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from chipzen.scaffold import scaffold_bot


class TestScaffoldBot:
    def test_creates_directory(self, tmp_path):
        bot_dir = scaffold_bot("my_bot", parent_dir=tmp_path)
        assert bot_dir.exists()
        assert bot_dir.is_dir()
        assert bot_dir.name == "my_bot"

    def test_creates_main_py(self, tmp_path):
        bot_dir = scaffold_bot("test_bot", parent_dir=tmp_path)
        main = bot_dir / "main.py"
        assert main.exists()
        content = main.read_text()
        assert "class MyBot" in content
        assert "def decide" in content
        assert "from chipzen import" in content

    def test_creates_requirements_txt(self, tmp_path):
        bot_dir = scaffold_bot("test_bot", parent_dir=tmp_path)
        req = bot_dir / "requirements.txt"
        assert req.exists()
        content = req.read_text()
        assert "numpy" in content  # listed in comments

    def test_creates_readme(self, tmp_path):
        bot_dir = scaffold_bot("test_bot", parent_dir=tmp_path)
        readme = bot_dir / "README.md"
        assert readme.exists()
        content = readme.read_text()
        assert "test_bot" in content
        assert "chipzen-sdk" in content

    def test_creates_gitignore(self, tmp_path):
        bot_dir = scaffold_bot("test_bot", parent_dir=tmp_path)
        gitignore = bot_dir / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "__pycache__" in content

    def test_existing_directory_raises(self, tmp_path):
        (tmp_path / "existing").mkdir()
        with pytest.raises(FileExistsError):
            scaffold_bot("existing", parent_dir=tmp_path)

    def test_scaffolded_bot_passes_validation(self, tmp_path):
        """The scaffolded bot should pass all validation checks."""
        from chipzen.validate import validate_bot

        bot_dir = scaffold_bot("valid_bot", parent_dir=tmp_path)
        results = validate_bot(bot_dir)
        severities = [sev for sev, _, _ in results]
        assert "fail" not in severities

    def test_default_parent_is_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bot_dir = scaffold_bot("cwd_bot")
        assert bot_dir.parent == tmp_path
