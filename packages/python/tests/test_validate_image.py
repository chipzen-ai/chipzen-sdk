"""``validate`` fails a poker bot the uploaded image could not start (issue #260).

The 0.6.3 ``init`` wrote ``main.py`` with no ``main()`` and no Dockerfile. It
passed every check, then never played after upload. These pin the two checks
that now catch it: ``dockerfile`` and ``entry_main``.
"""

import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import re

import pytest

from chipzen.scaffold import scaffold_bot
from chipzen.validate import validate_bot, validate_cli

BOT_CLASS = textwrap.dedent("""\
    from chipzen import Action, Bot, GameState


    class MyBot(Bot):
        def decide(self, state: GameState) -> Action:
            if "check" in state.valid_actions:
                return Action.check()
            return Action.fold()
""")

MAIN_FUNCTION = "\n\ndef main():\n    pass\n"
MAIN_GUARD = '\n\nif __name__ == "__main__":\n    main()\n'


def _check(results, name):
    found = [(sev, msg) for sev, check, msg in results if check == name]
    assert len(found) == 1, f"expected one {name} result, got {found}"
    return found[0]


def _dockerfile(start: str) -> str:
    return f"FROM python:3.12-slim\nWORKDIR /bot\nCOPY bot.py .\n{start}\n"


def _remove_main(bot_py):
    source = bot_py.read_text("utf-8")
    without = re.sub(r"\ndef main\(\) -> None:.*?(?=\nif __name__)", "\n", source, flags=re.S)
    assert "def main" not in without
    bot_py.write_text(without, "utf-8")


class TestFreshScaffold:
    def test_both_checks_pass(self, tmp_path):
        results = validate_bot(scaffold_bot("ok", parent_dir=tmp_path))
        assert _check(results, "dockerfile")[0] == "pass"
        assert _check(results, "entry_main") == ("pass", "bot.py defines main()")

    def test_removing_main_fails_with_a_fix(self, tmp_path):
        bot_dir = scaffold_bot("no_main", parent_dir=tmp_path)
        _remove_main(bot_dir / "bot.py")
        severity, message = _check(validate_bot(bot_dir), "entry_main")
        assert severity == "fail"
        assert "bot.py has no top-level main() function" in message
        assert "from bot import main; main()" in message
        assert "chipzen-sdk init" in message

    def test_removing_the_dockerfile_fails_with_a_fix(self, tmp_path):
        bot_dir = scaffold_bot("no_docker", parent_dir=tmp_path)
        (bot_dir / "Dockerfile").unlink()
        severity, message = _check(validate_bot(bot_dir), "dockerfile")
        assert severity == "fail"
        assert message.startswith("No Dockerfile in ")
        assert "docker save" in message and "chipzen-sdk init" in message

    def test_cli_exits_1_and_prints_the_reason(self, tmp_path, capsys):
        bot_dir = scaffold_bot("cli_no_main", parent_dir=tmp_path)
        _remove_main(bot_dir / "bot.py")
        with pytest.raises(SystemExit) as exc_info:
            validate_cli([str(bot_dir), "--no-color"])
        assert exc_info.value.code == 1
        assert "[FAIL] entry_main: bot.py has no top-level main()" in capsys.readouterr().out


def test_the_old_0_6_3_scaffold_layout_fails_both(tmp_path):
    """main.py with a Bot subclass, no main(), no Dockerfile: what #260 reported."""
    (tmp_path / "main.py").write_text(BOT_CLASS)
    results = validate_bot(tmp_path)
    assert _check(results, "dockerfile")[0] == "fail"
    severity, message = _check(results, "entry_main")
    assert severity == "fail"
    assert "main.py has no top-level main()" in message
    # The checks about decide() still run and still pass: the gap was that
    # they were the only ones.
    assert _check(results, "smoke_test")[0] == "pass"


class TestDockerfileCommand:
    def test_no_entrypoint_or_cmd_fails(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION)
        (tmp_path / "Dockerfile").write_text(_dockerfile("USER 10001"))
        severity, message = _check(validate_bot(tmp_path), "dockerfile")
        assert severity == "fail"
        assert "no ENTRYPOINT or CMD" in message

    def test_entrypoint_in_an_earlier_stage_does_not_count(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION)
        (tmp_path / "Dockerfile").write_text(
            'FROM python:3.12-slim AS builder\nENTRYPOINT ["python", "-c", "x"]\n'
            "FROM python:3.12-slim\nCOPY --from=builder /bot /bot\n"
        )
        assert _check(validate_bot(tmp_path), "dockerfile")[0] == "fail"

    def test_entrypoint_naming_another_module_fails(self, tmp_path):
        (tmp_path / "main.py").write_text(BOT_CLASS + MAIN_FUNCTION)
        (tmp_path / "Dockerfile").write_text(
            _dockerfile('ENTRYPOINT ["python", "-c", "from bot import main; main()"]')
        )
        severity, message = _check(validate_bot(tmp_path), "dockerfile")
        assert severity == "fail"
        assert "runs bot, but the entry point is main.py" in message

    def test_shell_form_and_cmd_are_read(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION)
        (tmp_path / "Dockerfile").write_text(
            _dockerfile('ENTRYPOINT ["python", "-c"]\nCMD ["from bot import main; main()"]')
        )
        results = validate_bot(tmp_path)
        assert _check(results, "dockerfile")[0] == "pass"
        assert _check(results, "entry_main")[0] == "pass"

    def test_unreadable_command_falls_back_to_requiring_main(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS)
        (tmp_path / "Dockerfile").write_text(_dockerfile('CMD ["./start.sh"]'))
        results = validate_bot(tmp_path)
        assert _check(results, "dockerfile")[0] == "pass"
        assert _check(results, "entry_main")[0] == "fail"


class TestScriptEntryPoint:
    """``python -u /bot/bot.py`` (the reference bot's form) needs a __main__ block."""

    START = 'ENTRYPOINT ["python", "-u", "/bot/bot.py"]'

    def test_with_main_guard_passes(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION + MAIN_GUARD)
        (tmp_path / "Dockerfile").write_text(_dockerfile(self.START))
        results = validate_bot(tmp_path)
        assert [s for s, n, _ in results if s == "fail"] == []
        assert "__main__" in _check(results, "entry_main")[1]

    def test_reversed_guard_counts(self, tmp_path):
        guard = '\n\nif "__main__" == __name__:\n    main()\n'
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION + guard)
        (tmp_path / "Dockerfile").write_text(_dockerfile(self.START))
        assert _check(validate_bot(tmp_path), "entry_main")[0] == "pass"

    def test_without_main_guard_fails(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION)
        (tmp_path / "Dockerfile").write_text(_dockerfile(self.START))
        severity, message = _check(validate_bot(tmp_path), "entry_main")
        assert severity == "fail"
        assert "runs bot.py as a script" in message

    def test_python_dash_m_is_script_mode(self, tmp_path):
        (tmp_path / "bot.py").write_text(BOT_CLASS + MAIN_FUNCTION)
        (tmp_path / "Dockerfile").write_text(_dockerfile('CMD ["python", "-m", "bot"]'))
        assert _check(validate_bot(tmp_path), "entry_main")[0] == "fail"


def test_zip_without_a_dockerfile_fails(tmp_path):
    import zipfile

    zip_path = tmp_path / "bot.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("main.py", BOT_CLASS + MAIN_FUNCTION)
    assert _check(validate_bot(zip_path), "dockerfile")[0] == "fail"
