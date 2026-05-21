"""Tests for ``chipzen run-external`` CLI wrapper.

Covers the External-API Issue 25 spec
(``management/ophir-track/external-api-issue-breakdown.md`` §25,
chipzen-ai/chipzen-sdk#44):

- argparse surface (--env, --token, --bot-id, --bot-class, --log-level)
- dynamic bot-module loading via importlib.util.spec_from_file_location
- Bot subclass discovery (single auto, multiple needs --bot-class)
- precedence rules (--token > config token; --env > $CHIPZEN_ENV >
  default; config url > env-derived URL)
- error surfaces (missing file, no Bot subclass, multiple subclasses,
  no bot_id when URL not set, missing required arg)
- integration: CLI runs against a stub WS server and the bot's
  ``decide`` is invoked.

Conventions match ``tests/test_config.py`` and ``tests/test_connect.py``:
the helpers ``_install_connect_stub`` / ``_bypass_config_discovery`` /
``_clear_chipzen_env`` are duplicated here so the test file stays
standalone (the SDK doesn't ship a test-helpers fixture module).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from chipzen.bot import ChipzenBot
from chipzen.connect import ENV_VAR_NAME
from chipzen.run_external import (
    _build_parser,
    _find_bot_subclasses,
    _load_bot_module,
    _resolve_connection,
    _select_bot_class,
    run_external_cli,
)

from chipzen.config import CONFIG_FILENAME, ChipzenConfig, ChipzenConfigError

# ---------------------------------------------------------------------------
# Helpers (copied from test_config + test_connect to keep this file
# standalone — the SDK doesn't ship a shared test-helpers fixture module.)
# ---------------------------------------------------------------------------


def _write_toml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _bypass_config_discovery(monkeypatch, tmp_path) -> None:
    """Redirect cwd + home to empty dirs so default config discovery
    finds nothing."""
    empty_cwd = tmp_path / "empty_cwd"
    empty_cwd.mkdir(exist_ok=True)
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(exist_ok=True)
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))


def _clear_chipzen_env(monkeypatch) -> None:
    monkeypatch.delenv(ENV_VAR_NAME, raising=False)


class _CapturingWS:
    """Mock websocket recording everything sent. Returns an error frame
    on the first ``recv()`` so the session loop exits cleanly after the
    handshake."""

    def __init__(self):
        self.sent: list[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def recv(self) -> str:
        return '{"type":"error","code":"x","message":"test exit"}'

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _CapturingConnect:
    """Stub for ``websockets.connect`` capturing the URL it's called with."""

    def __init__(self):
        self.urls: list[str] = []
        self.user_agents: list[str | None] = []
        self.ws = _CapturingWS()

    def __call__(self, url, *, user_agent_header=None):
        self.urls.append(url)
        self.user_agents.append(user_agent_header)
        return self

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *args):
        return False


def _install_connect_stub(monkeypatch) -> _CapturingConnect:
    stub = _CapturingConnect()
    import websockets.asyncio.client as wsac

    monkeypatch.setattr(wsac, "connect", stub)
    return stub


# Body for a minimal bot file used by many tests. Defined once so the
# tests stay shorter.
_SINGLE_BOT_FILE = """\
from chipzen import Bot, GameState, Action


class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()
"""

_TWO_BOTS_FILE = """\
from chipzen import Bot, GameState, Action


class FirstBot(Bot):
    def decide(self, state: GameState) -> Action:
        return Action.fold()


class SecondBot(Bot):
    def decide(self, state: GameState) -> Action:
        return Action.fold()
"""

_NO_BOT_FILE = """\
# A Python file that does not define any Bot subclass.

def helper() -> int:
    return 42
"""

_BROKEN_BOT_FILE = """\
# Intentionally broken Python.
class MyBot(  # syntax error
"""

_IMPORTS_OTHER_BOT_FILE = """\
# A file that imports a Bot from elsewhere and also defines one locally.
# Discovery must pick only the locally-defined class.
from chipzen import Bot, GameState, Action
from chipzen.examples.random_bot import RandomBot  # noqa: F401


class LocalBot(Bot):
    def decide(self, state: GameState) -> Action:
        return Action.fold()
"""


# ---------------------------------------------------------------------------
# Argparse surface
# ---------------------------------------------------------------------------


class TestArgParser:
    """Tests for ``_build_parser`` — pure argparse surface."""

    def test_bot_file_is_required(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_bot_file_parses_as_path(self):
        parser = _build_parser()
        parsed = parser.parse_args(["my_bot.py"])
        assert isinstance(parsed.bot_file, Path)
        assert parsed.bot_file == Path("my_bot.py")

    def test_env_defaults_to_none(self):
        """env=None is the 'caller didn't say anything' sentinel so the
        downstream resolver can distinguish from explicit env='prod'."""
        parser = _build_parser()
        parsed = parser.parse_args(["b.py"])
        assert parsed.env is None

    def test_env_accepts_three_canonical_values(self):
        parser = _build_parser()
        for env in ("prod", "staging", "local"):
            parsed = parser.parse_args(["b.py", "--env", env])
            assert parsed.env == env

    def test_env_rejects_unknown_value(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["b.py", "--env", "dev"])

    def test_token_defaults_none(self):
        parser = _build_parser()
        parsed = parser.parse_args(["b.py"])
        assert parsed.token is None

    def test_token_arg(self):
        parser = _build_parser()
        parsed = parser.parse_args(["b.py", "--token", "cz_extbot_xyz"])
        assert parsed.token == "cz_extbot_xyz"

    def test_bot_id_arg(self):
        parser = _build_parser()
        parsed = parser.parse_args(["b.py", "--bot-id", "abc123"])
        assert parsed.bot_id == "abc123"

    def test_bot_class_arg(self):
        parser = _build_parser()
        parsed = parser.parse_args(["b.py", "--bot-class", "TightAggressive"])
        assert parsed.bot_class == "TightAggressive"

    def test_log_level_default_info(self):
        parser = _build_parser()
        parsed = parser.parse_args(["b.py"])
        assert parsed.log_level == "INFO"

    def test_log_level_choices_enforced(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["b.py", "--log-level", "TRACE"])

    def test_help_lists_examples(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        captured = capsys.readouterr()
        # Examples section is the user-facing payoff of the help text.
        assert "chipzen run-external" in captured.out
        assert "CHIPZEN_ENV" in captured.out


# ---------------------------------------------------------------------------
# Bot module loading
# ---------------------------------------------------------------------------


class TestLoadBotModule:
    """Tests for ``_load_bot_module`` — dynamic file -> module import."""

    def test_loads_single_bot_file(self, tmp_path):
        bot_file = tmp_path / "single_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)

        module = _load_bot_module(bot_file)
        assert hasattr(module, "MyBot")
        assert module.MyBot.__name__ == "MyBot"

    def test_missing_file_raises_clear_error(self, tmp_path):
        nope = tmp_path / "does_not_exist.py"
        with pytest.raises(FileNotFoundError, match="does_not_exist.py"):
            _load_bot_module(nope)

    def test_syntax_error_raises_importerror(self, tmp_path):
        bot_file = tmp_path / "broken_bot.py"
        bot_file.write_text(_BROKEN_BOT_FILE)
        with pytest.raises(ImportError, match="Failed to load"):
            _load_bot_module(bot_file)

    def test_broken_module_does_not_leak_into_sys_modules(self, tmp_path):
        """Half-loaded modules must be popped from sys.modules to avoid
        confusing the next import attempt."""
        bot_file = tmp_path / "leak_bot.py"
        bot_file.write_text(_BROKEN_BOT_FILE)
        before = set(sys.modules)
        with pytest.raises(ImportError):
            _load_bot_module(bot_file)
        after = set(sys.modules)
        leaked = [m for m in (after - before) if "leak_bot" in m]
        assert leaked == [], f"Leaked modules: {leaked}"

    def test_loaded_module_is_named_with_synthetic_prefix(self, tmp_path):
        """The synthetic module name avoids collisions with real packages."""
        bot_file = tmp_path / "named_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)
        module = _load_bot_module(bot_file)
        assert "named_bot" in module.__name__
        # And it's registered in sys.modules so re-loading the same file
        # doesn't surprise importlib's cache.
        assert module.__name__ in sys.modules


# ---------------------------------------------------------------------------
# Bot subclass discovery
# ---------------------------------------------------------------------------


class TestFindBotSubclasses:
    """Tests for ``_find_bot_subclasses``."""

    def test_finds_single_subclass(self, tmp_path):
        bot_file = tmp_path / "single.py"
        bot_file.write_text(_SINGLE_BOT_FILE)
        module = _load_bot_module(bot_file)
        found = _find_bot_subclasses(module)
        assert len(found) == 1
        assert found[0].__name__ == "MyBot"

    def test_finds_multiple_subclasses(self, tmp_path):
        bot_file = tmp_path / "multi.py"
        bot_file.write_text(_TWO_BOTS_FILE)
        module = _load_bot_module(bot_file)
        found = _find_bot_subclasses(module)
        names = sorted(c.__name__ for c in found)
        assert names == ["FirstBot", "SecondBot"]

    def test_returns_empty_when_no_subclass(self, tmp_path):
        bot_file = tmp_path / "noclass.py"
        bot_file.write_text(_NO_BOT_FILE)
        module = _load_bot_module(bot_file)
        assert _find_bot_subclasses(module) == []

    def test_excludes_chipzen_bot_base_class_itself(self, tmp_path):
        """The ``from chipzen import Bot`` import surfaces ``Bot`` as a
        module member, but it must NOT be returned as a candidate."""
        bot_file = tmp_path / "single.py"
        bot_file.write_text(_SINGLE_BOT_FILE)
        module = _load_bot_module(bot_file)
        found = _find_bot_subclasses(module)
        # Base class itself is never a candidate.
        assert ChipzenBot not in found

    def test_excludes_bot_subclasses_imported_from_elsewhere(self, tmp_path):
        """If the user's file imports a Bot subclass from another module,
        it must NOT be counted — only locally-defined ones matter.
        Otherwise scaffolds that ``from chipzen.examples.random_bot
        import RandomBot`` would always trigger the ambiguous-pick
        error.
        """
        bot_file = tmp_path / "imports_other.py"
        bot_file.write_text(_IMPORTS_OTHER_BOT_FILE)
        module = _load_bot_module(bot_file)
        found = _find_bot_subclasses(module)
        names = [c.__name__ for c in found]
        assert names == ["LocalBot"]


# ---------------------------------------------------------------------------
# Bot class selection
# ---------------------------------------------------------------------------


class TestSelectBotClass:
    """Tests for ``_select_bot_class`` (the picker)."""

    @staticmethod
    def _make(name: str) -> type[ChipzenBot]:
        return type(name, (ChipzenBot,), {"decide": lambda self, state: None})

    def test_single_candidate_auto_selected(self):
        only = self._make("Only")
        chosen = _select_bot_class([only], bot_class_name=None, bot_file=Path("x.py"))
        assert chosen is only

    def test_no_candidates_raises_clear_error(self):
        with pytest.raises(RuntimeError, match="No chipzen.Bot subclass found"):
            _select_bot_class([], bot_class_name=None, bot_file=Path("x.py"))

    def test_multiple_candidates_requires_bot_class_arg(self):
        a = self._make("Alpha")
        b = self._make("Beta")
        with pytest.raises(RuntimeError, match="Multiple chipzen.Bot subclasses"):
            _select_bot_class([a, b], bot_class_name=None, bot_file=Path("x.py"))

    def test_multiple_candidates_explicit_pick_works(self):
        a = self._make("Alpha")
        b = self._make("Beta")
        chosen = _select_bot_class([a, b], bot_class_name="Beta", bot_file=Path("x.py"))
        assert chosen is b

    def test_explicit_pick_unknown_name_raises(self):
        a = self._make("Alpha")
        with pytest.raises(RuntimeError, match="No Bot subclass named 'Ghost'"):
            _select_bot_class([a], bot_class_name="Ghost", bot_file=Path("x.py"))

    def test_error_messages_list_available_names(self):
        a = self._make("Alpha")
        b = self._make("Beta")
        with pytest.raises(RuntimeError, match="Alpha.*Beta|Beta.*Alpha"):
            _select_bot_class([a, b], bot_class_name=None, bot_file=Path("x.py"))


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------


class TestResolveConnection:
    """Tests for ``_resolve_connection`` (url/token/policy picker)."""

    def test_config_url_used_when_present(self, monkeypatch):
        _clear_chipzen_env(monkeypatch)
        cfg = ChipzenConfig(
            path=Path("/x"),
            token="cz_extbot_cfg",
            url="wss://override.example/ws/external/bot/x",
        )
        url, token, _policy, returned_cfg = _resolve_connection(
            config=cfg,
            explicit_env=None,
            explicit_token=None,
            explicit_bot_id=None,
        )
        assert url == "wss://override.example/ws/external/bot/x"
        assert token == "cz_extbot_cfg"
        assert returned_cfg is cfg

    def test_explicit_token_overrides_config_token(self, monkeypatch):
        _clear_chipzen_env(monkeypatch)
        cfg = ChipzenConfig(
            path=Path("/x"),
            token="cz_extbot_cfg",
            url="wss://override.example/ws/external/bot/x",
        )
        url, token, _policy, _cfg = _resolve_connection(
            config=cfg,
            explicit_env=None,
            explicit_token="cz_extbot_arg",
            explicit_bot_id=None,
        )
        assert token == "cz_extbot_arg"

    def test_env_derived_url_when_no_config_url(self, monkeypatch):
        """Without config URL, builds wss://chipzen.ai/ws/external/bot/{bot_id}."""
        _clear_chipzen_env(monkeypatch)
        cfg = ChipzenConfig(
            path=Path("/x"),
            token="cz_extbot_cfg",
            bot_id="bot-uuid-1234",
        )
        url, token, _policy, _cfg = _resolve_connection(
            config=cfg,
            explicit_env="staging",
            explicit_token=None,
            explicit_bot_id=None,
        )
        assert url == "wss://staging.chipzen.ai/ws/external/bot/bot-uuid-1234"
        assert token == "cz_extbot_cfg"

    def test_env_derived_default_is_prod(self, monkeypatch):
        _clear_chipzen_env(monkeypatch)
        cfg = ChipzenConfig(path=Path("/x"), bot_id="bot-id-x")
        url, _token, _policy, _cfg = _resolve_connection(
            config=cfg,
            explicit_env=None,
            explicit_token=None,
            explicit_bot_id=None,
        )
        assert url == "wss://chipzen.ai/ws/external/bot/bot-id-x"

    def test_env_var_used_when_no_explicit_env(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR_NAME, "local")
        cfg = ChipzenConfig(path=Path("/x"), bot_id="bot-id-x")
        url, _token, _policy, _cfg = _resolve_connection(
            config=cfg,
            explicit_env=None,
            explicit_token=None,
            explicit_bot_id=None,
        )
        assert url == "ws://localhost:8001/ws/external/bot/bot-id-x"

    def test_explicit_env_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR_NAME, "local")
        cfg = ChipzenConfig(path=Path("/x"), bot_id="bot-id-x")
        url, _token, _policy, _cfg = _resolve_connection(
            config=cfg,
            explicit_env="prod",
            explicit_token=None,
            explicit_bot_id=None,
        )
        assert url == "wss://chipzen.ai/ws/external/bot/bot-id-x"

    def test_explicit_bot_id_overrides_config_bot_id(self, monkeypatch):
        _clear_chipzen_env(monkeypatch)
        cfg = ChipzenConfig(path=Path("/x"), bot_id="from-config")
        url, _token, _policy, _cfg = _resolve_connection(
            config=cfg,
            explicit_env="prod",
            explicit_token=None,
            explicit_bot_id="from-arg",
        )
        assert url == "wss://chipzen.ai/ws/external/bot/from-arg"

    def test_no_bot_id_no_url_raises(self, monkeypatch):
        """Without a URL and without a bot_id, there's no way to build a
        lobby URL — surface a clear error."""
        _clear_chipzen_env(monkeypatch)
        cfg = ChipzenConfig(path=Path("/x"), token="cz_extbot_x")
        with pytest.raises(RuntimeError, match="No lobby URL is configured"):
            _resolve_connection(
                config=cfg,
                explicit_env="prod",
                explicit_token=None,
                explicit_bot_id=None,
            )

    def test_no_config_no_bot_id_raises(self, monkeypatch):
        _clear_chipzen_env(monkeypatch)
        with pytest.raises(RuntimeError, match="No lobby URL is configured"):
            _resolve_connection(
                config=None,
                explicit_env="prod",
                explicit_token=None,
                explicit_bot_id=None,
            )

    def test_no_config_with_explicit_bot_id_works(self, monkeypatch):
        _clear_chipzen_env(monkeypatch)
        url, token, _policy, returned_cfg = _resolve_connection(
            config=None,
            explicit_env="prod",
            explicit_token="cz_extbot_arg",
            explicit_bot_id="bot-x",
        )
        assert url == "wss://chipzen.ai/ws/external/bot/bot-x"
        assert token == "cz_extbot_arg"
        # connect_to_chipzen runs discovery when config is None; since
        # we bypassed config discovery, the returned config is None too.
        assert returned_cfg is None


# ---------------------------------------------------------------------------
# Config schema extension — bot_id field
# ---------------------------------------------------------------------------


class TestConfigBotIdField:
    """Tests for the new ``bot_id`` field on ``ChipzenConfig``."""

    def test_load_parses_bot_id(self, tmp_path):
        from chipzen.config import load_chipzen_config

        cfg_path = _write_toml(
            tmp_path / CONFIG_FILENAME,
            '[external_api]\ntoken = "cz_extbot_x"\nbot_id = "abc123"\n',
        )
        cfg = load_chipzen_config([cfg_path])
        assert cfg is not None
        assert cfg.bot_id == "abc123"

    def test_bot_id_optional(self, tmp_path):
        """bot_id is optional: absence is fine, not an error."""
        from chipzen.config import load_chipzen_config

        cfg_path = _write_toml(
            tmp_path / CONFIG_FILENAME, '[external_api]\ntoken = "cz_extbot_x"\n'
        )
        cfg = load_chipzen_config([cfg_path])
        assert cfg is not None
        assert cfg.bot_id is None

    def test_bot_id_wrong_type_raises(self, tmp_path):
        from chipzen.config import load_chipzen_config

        cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, "[external_api]\nbot_id = 42\n")
        with pytest.raises(ChipzenConfigError, match="bot_id must be a string"):
            load_chipzen_config([cfg_path])


# ---------------------------------------------------------------------------
# CLI integration — end-to-end
# ---------------------------------------------------------------------------


class TestRunExternalCli:
    """Integration tests for ``run_external_cli``: parse args, load bot,
    resolve connection, and call ``run_bot`` against the stub WS server."""

    def test_missing_file_exits_2(self, tmp_path, monkeypatch):
        _bypass_config_discovery(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc:
            run_external_cli([str(tmp_path / "does_not_exist.py")])
        assert exc.value.code == 2

    def test_no_bot_subclass_exits_2(self, tmp_path, monkeypatch):
        _bypass_config_discovery(monkeypatch, tmp_path)
        bot_file = tmp_path / "empty_bot.py"
        bot_file.write_text(_NO_BOT_FILE)
        with pytest.raises(SystemExit) as exc:
            run_external_cli([str(bot_file), "--bot-id", "x", "--token", "cz_extbot_x"])
        assert exc.value.code == 2

    def test_multiple_subclasses_without_bot_class_exits_2(self, tmp_path, monkeypatch):
        _bypass_config_discovery(monkeypatch, tmp_path)
        bot_file = tmp_path / "multi.py"
        bot_file.write_text(_TWO_BOTS_FILE)
        with pytest.raises(SystemExit) as exc:
            run_external_cli([str(bot_file), "--bot-id", "x", "--token", "cz_extbot_x"])
        assert exc.value.code == 2

    def test_no_bot_id_no_url_exits_2(self, tmp_path, monkeypatch):
        """Without any way to build a URL, the CLI must exit non-zero
        with a clear error message — not crash mid-import."""
        _bypass_config_discovery(monkeypatch, tmp_path)
        _clear_chipzen_env(monkeypatch)
        bot_file = tmp_path / "ok_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)
        with pytest.raises(SystemExit) as exc:
            run_external_cli([str(bot_file), "--token", "cz_extbot_x"])
        assert exc.value.code == 2

    def test_runs_against_stub_server(self, tmp_path, monkeypatch):
        """End-to-end happy path: CLI loads bot, resolves URL, calls
        ``run_bot`` which connects to the stubbed websocket and sends
        an authenticate frame."""
        _bypass_config_discovery(monkeypatch, tmp_path)
        _clear_chipzen_env(monkeypatch)
        stub = _install_connect_stub(monkeypatch)

        bot_file = tmp_path / "stub_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)

        run_external_cli(
            [
                str(bot_file),
                "--env",
                "staging",
                "--bot-id",
                "test-bot-uuid",
                "--token",
                "cz_extbot_test",
            ]
        )

        # URL was built from env=staging + bot-id from the CLI.
        assert stub.urls == ["wss://staging.chipzen.ai/ws/external/bot/test-bot-uuid"]
        # Authenticate frame carries the explicit token.
        assert len(stub.ws.sent) == 1
        auth = json.loads(stub.ws.sent[0])
        assert auth["type"] == "authenticate"
        assert auth["token"] == "cz_extbot_test"

    def test_config_token_used_when_no_arg(self, tmp_path, monkeypatch):
        """Token defaults to the chipzen.toml value when no --token flag."""
        _clear_chipzen_env(monkeypatch)
        stub = _install_connect_stub(monkeypatch)

        # Place chipzen.toml in cwd.
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))
        _write_toml(
            tmp_path / CONFIG_FILENAME,
            '[external_api]\ntoken = "cz_extbot_from_config"\nbot_id = "cfg-bot-id"\n',
        )

        bot_file = tmp_path / "config_token_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)

        run_external_cli([str(bot_file), "--env", "prod"])

        auth = json.loads(stub.ws.sent[0])
        assert auth["token"] == "cz_extbot_from_config"
        # bot_id came from config, env from CLI.
        assert stub.urls == ["wss://chipzen.ai/ws/external/bot/cfg-bot-id"]

    def test_config_url_overrides_env_derived(self, tmp_path, monkeypatch):
        """[external_api].url short-circuits env-derived URL building."""
        _clear_chipzen_env(monkeypatch)
        stub = _install_connect_stub(monkeypatch)

        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))
        _write_toml(
            tmp_path / CONFIG_FILENAME,
            "[external_api]\n"
            'token = "cz_extbot_cfg"\n'
            'url = "wss://verbatim.example/ws/external/bot/v"\n',
        )

        bot_file = tmp_path / "verbatim_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)

        run_external_cli([str(bot_file), "--env", "prod"])

        # env=prod was ignored because the config URL wins.
        assert stub.urls == ["wss://verbatim.example/ws/external/bot/v"]

    def test_bot_class_disambiguates_two_bots(self, tmp_path, monkeypatch):
        """--bot-class picks the right Bot subclass when there are two."""
        _bypass_config_discovery(monkeypatch, tmp_path)
        _clear_chipzen_env(monkeypatch)
        stub = _install_connect_stub(monkeypatch)

        bot_file = tmp_path / "two_bots.py"
        bot_file.write_text(_TWO_BOTS_FILE)

        # Should NOT raise: --bot-class resolves the ambiguity.
        run_external_cli(
            [
                str(bot_file),
                "--bot-id",
                "two-id",
                "--token",
                "cz_extbot_x",
                "--bot-class",
                "SecondBot",
            ]
        )

        # Connection still went through.
        assert len(stub.urls) == 1

    def test_syntax_error_in_bot_file_exits_2(self, tmp_path, monkeypatch):
        """A bot file with a syntax error surfaces a clean CLI error
        rather than a bare traceback at exec_module time."""
        _bypass_config_discovery(monkeypatch, tmp_path)
        _clear_chipzen_env(monkeypatch)
        bot_file = tmp_path / "broken.py"
        bot_file.write_text(_BROKEN_BOT_FILE)
        with pytest.raises(SystemExit) as exc:
            run_external_cli(
                [str(bot_file), "--bot-id", "x", "--token", "cz_extbot_x"]
            )
        assert exc.value.code == 2

    def test_bot_init_error_exits_2(self, tmp_path, monkeypatch):
        """An exception in the Bot's ``__init__`` exits cleanly with
        code 2 instead of propagating a bare stack trace."""
        _bypass_config_discovery(monkeypatch, tmp_path)
        _clear_chipzen_env(monkeypatch)
        bot_file = tmp_path / "init_crash.py"
        bot_file.write_text(
            "from chipzen import Bot, GameState, Action\n"
            "\n"
            "class CrashBot(Bot):\n"
            "    def __init__(self):\n"
            "        raise RuntimeError('boom during init')\n"
            "    def decide(self, state: GameState) -> Action:\n"
            "        return Action.fold()\n"
        )
        with pytest.raises(SystemExit) as exc:
            run_external_cli(
                [str(bot_file), "--bot-id", "x", "--token", "cz_extbot_x"]
            )
        assert exc.value.code == 2

    def test_malformed_config_exits_2(self, tmp_path, monkeypatch):
        """Malformed chipzen.toml -> clean error, exit 2."""
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))
        _write_toml(tmp_path / CONFIG_FILENAME, "= = invalid toml\n")

        bot_file = tmp_path / "ok_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)

        with pytest.raises(SystemExit) as exc:
            run_external_cli([str(bot_file), "--token", "x", "--bot-id", "y"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# __main__ wiring
# ---------------------------------------------------------------------------


class TestMainCommandRouting:
    """Tests for ``chipzen.__main__.main`` routing ``run-external``."""

    def test_run_external_command_listed_in_help(self, capsys):
        from chipzen.__main__ import main

        original_argv = sys.argv
        try:
            sys.argv = ["chipzen", "--help"]
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
            captured = capsys.readouterr()
            assert "run-external" in captured.out
        finally:
            sys.argv = original_argv

    def test_run_external_command_routes_to_run_external_cli(self, tmp_path, monkeypatch):
        """``chipzen run-external <file>`` actually dispatches to
        ``run_external_cli``."""
        _bypass_config_discovery(monkeypatch, tmp_path)
        _clear_chipzen_env(monkeypatch)
        _install_connect_stub(monkeypatch)

        bot_file = tmp_path / "routed_bot.py"
        bot_file.write_text(_SINGLE_BOT_FILE)

        from chipzen.__main__ import main

        original_argv = sys.argv
        try:
            sys.argv = [
                "chipzen",
                "run-external",
                str(bot_file),
                "--env",
                "prod",
                "--bot-id",
                "routed-id",
                "--token",
                "cz_extbot_routed",
            ]
            main()  # should not raise
        finally:
            sys.argv = original_argv
