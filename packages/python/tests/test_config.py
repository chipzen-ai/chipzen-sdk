"""Tests for ``chipzen.toml`` discovery + ``run_bot`` config-file fallback.

Covers the External-API Issue 23 spec
(``management/ophir-track/external-api-issue-breakdown.md`` §23,
chipzen-ai/chipzen-sdk#42):

- discovery search order (cwd > ~/.chipzen/ > /etc/chipzen/)
- parsing recognized fields ([external_api].token + [external_api].url)
- precedence rules (explicit token kwarg always wins over config-file)
- error surfaces (missing section, malformed TOML, wrong types)
- POSIX-only /etc behavior (skipped on Windows)
- ``run_bot()`` integration (config-file fallback + explicit override)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
from chipzen.bot import ChipzenBot
from chipzen.client import run_bot
from chipzen.config import (
    CONFIG_FILENAME,
    SECTION_NAME,
    ChipzenConfig,
    ChipzenConfigError,
    _search_paths,
    discover_config_path,
    load_chipzen_config,
    resolve_token,
    resolve_url,
)

# ---------------------------------------------------------------------------
# Module-level exports + dataclass shape
# ---------------------------------------------------------------------------


def test_public_api_exported_from_chipzen():
    """``ChipzenConfig`` / ``ChipzenConfigError`` / ``load_chipzen_config``
    are part of the canonical ``chipzen`` namespace."""
    assert chipzen.ChipzenConfig is ChipzenConfig
    assert chipzen.ChipzenConfigError is ChipzenConfigError
    assert chipzen.load_chipzen_config is load_chipzen_config
    for name in ("ChipzenConfig", "ChipzenConfigError", "load_chipzen_config"):
        assert name in chipzen.__all__


def test_chipzen_config_is_frozen():
    """Config dataclass is immutable so callers can share one instance
    safely across worker tasks."""
    cfg = ChipzenConfig(path=Path("/tmp/x"), token="cz_extbot_xyz")
    with pytest.raises((AttributeError, Exception)):
        cfg.token = "other"  # type: ignore[misc]


def test_constants_match_spec():
    """The filename + section name are the canonical strings from the spec."""
    assert CONFIG_FILENAME == "chipzen.toml"
    assert SECTION_NAME == "external_api"


def test_chipzen_config_error_is_value_error():
    """``ChipzenConfigError`` subclasses ``ValueError`` so the SDK's
    existing error surface (``RetryPolicy`` validation, etc.) stays
    consistent."""
    assert issubclass(ChipzenConfigError, ValueError)


# ---------------------------------------------------------------------------
# Search-path order
# ---------------------------------------------------------------------------


def test_search_paths_order_starts_with_cwd():
    """Spec: cwd is the first candidate."""
    paths = _search_paths()
    assert paths[0] == Path.cwd() / CONFIG_FILENAME


def test_search_paths_includes_home():
    """Spec: ``~/.chipzen/chipzen.toml`` is the second candidate."""
    paths = _search_paths()
    assert paths[1] == Path.home() / ".chipzen" / CONFIG_FILENAME


def test_search_paths_etc_only_on_posix():
    """``/etc/chipzen/chipzen.toml`` is POSIX-only. Windows has no
    equivalent system path on the spec list (the home-dir entry is
    enough)."""
    paths = _search_paths()
    etc = Path("/etc/chipzen") / CONFIG_FILENAME
    if os.name == "posix":
        assert etc in paths
        # And it's the LAST entry (cwd > home > /etc).
        assert paths[-1] == etc
        assert len(paths) == 3
    else:
        assert etc not in paths
        assert len(paths) == 2


# ---------------------------------------------------------------------------
# Discovery — first match wins on the search path
# ---------------------------------------------------------------------------


def _write_toml(path: Path, body: str) -> Path:
    """Helper: write a config file, create parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_discover_returns_none_when_nothing_on_search_path(tmp_path):
    """No file anywhere -> ``None`` (NOT an error)."""
    # An entirely empty list of candidates also produces None.
    assert discover_config_path([tmp_path / "nope.toml"]) is None
    assert discover_config_path([]) is None


def test_discover_cwd_wins_over_home(tmp_path):
    """Spec: cwd > home. If both files exist, cwd wins."""
    cwd_file = _write_toml(tmp_path / "cwd" / CONFIG_FILENAME, "[external_api]\ntoken='cwd'\n")
    home_file = _write_toml(tmp_path / "home" / CONFIG_FILENAME, "[external_api]\ntoken='home'\n")

    found = discover_config_path([cwd_file, home_file])
    assert found == cwd_file


def test_discover_home_used_when_cwd_missing(tmp_path):
    """If only home has the file, home is returned."""
    cwd_missing = tmp_path / "cwd" / CONFIG_FILENAME
    home_file = _write_toml(tmp_path / "home" / CONFIG_FILENAME, "[external_api]\ntoken='home'\n")

    found = discover_config_path([cwd_missing, home_file])
    assert found == home_file


def test_discover_etc_used_when_both_others_missing(tmp_path):
    """If only the last candidate exists, it's returned."""
    cwd_missing = tmp_path / "cwd" / CONFIG_FILENAME
    home_missing = tmp_path / "home" / CONFIG_FILENAME
    etc_file = _write_toml(tmp_path / "etc" / CONFIG_FILENAME, "[external_api]\ntoken='etc'\n")

    found = discover_config_path([cwd_missing, home_missing, etc_file])
    assert found == etc_file


def test_discover_directory_at_path_is_skipped(tmp_path):
    """If a *directory* exists at ``chipzen.toml``, treat it as not-a-file
    and fall through (rare but possible if a dev fat-fingers ``mkdir``)."""
    weird = tmp_path / CONFIG_FILENAME
    weird.mkdir()
    fallback = _write_toml(tmp_path / "elsewhere" / CONFIG_FILENAME, "[external_api]\ntoken='ok'\n")
    assert discover_config_path([weird, fallback]) == fallback


# ---------------------------------------------------------------------------
# Parsing — happy path
# ---------------------------------------------------------------------------


def test_load_parses_token_and_url(tmp_path):
    """Both fields populate the dataclass."""
    cfg_path = _write_toml(
        tmp_path / CONFIG_FILENAME,
        '[external_api]\ntoken = "cz_extbot_xyz"\nurl = "wss://chipzen.ai/ws/external/bot/abc"\n',
    )
    cfg = load_chipzen_config([cfg_path])
    assert cfg is not None
    assert cfg.token == "cz_extbot_xyz"
    assert cfg.url == "wss://chipzen.ai/ws/external/bot/abc"
    assert cfg.path == cfg_path


def test_load_token_only(tmp_path):
    """URL is optional; missing url is fine."""
    cfg_path = _write_toml(
        tmp_path / CONFIG_FILENAME,
        '[external_api]\ntoken = "cz_extbot_xyz"\n',
    )
    cfg = load_chipzen_config([cfg_path])
    assert cfg is not None
    assert cfg.token == "cz_extbot_xyz"
    assert cfg.url is None


def test_load_url_only(tmp_path):
    """Token is also optional — a config with only a URL is valid (e.g.
    a shared system file that points at staging; each dev supplies the
    token explicitly)."""
    cfg_path = _write_toml(
        tmp_path / CONFIG_FILENAME,
        '[external_api]\nurl = "wss://staging.chipzen.ai/ws/external/bot/x"\n',
    )
    cfg = load_chipzen_config([cfg_path])
    assert cfg is not None
    assert cfg.token is None
    assert cfg.url == "wss://staging.chipzen.ai/ws/external/bot/x"


def test_load_empty_section_returns_config_with_none_fields(tmp_path):
    """An empty ``[external_api]`` table is valid TOML; just yields a
    ChipzenConfig with both fields None. Caller decides what to do."""
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, "[external_api]\n")
    cfg = load_chipzen_config([cfg_path])
    assert cfg is not None
    assert cfg.token is None
    assert cfg.url is None


def test_load_no_search_path_match_returns_none(tmp_path):
    """No file anywhere on the search path -> ``None``, no error."""
    cfg = load_chipzen_config([tmp_path / "missing.toml"])
    assert cfg is None


def test_load_ignores_unknown_fields(tmp_path):
    """Extra fields under ``[external_api]`` are silently ignored. This
    keeps forward-compat with future SDK versions that add new fields:
    older SDKs reading a newer config don't crash."""
    cfg_path = _write_toml(
        tmp_path / CONFIG_FILENAME,
        '[external_api]\ntoken = "cz_extbot_xyz"\nfuture_field = 42\n',
    )
    cfg = load_chipzen_config([cfg_path])
    assert cfg is not None
    assert cfg.token == "cz_extbot_xyz"


def test_load_ignores_unrelated_top_level_sections(tmp_path):
    """Other top-level sections coexist peacefully — devs may want one
    config file for multiple tools."""
    cfg_path = _write_toml(
        tmp_path / CONFIG_FILENAME,
        '[other_tool]\nsetting = "x"\n[external_api]\ntoken = "cz_extbot_ok"\n',
    )
    cfg = load_chipzen_config([cfg_path])
    assert cfg is not None
    assert cfg.token == "cz_extbot_ok"


# ---------------------------------------------------------------------------
# Parsing — error surfaces
# ---------------------------------------------------------------------------


def test_load_malformed_toml_raises(tmp_path):
    """Malformed TOML -> clear error pointing at the file."""
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, "this is = = not valid toml\n")
    with pytest.raises(ChipzenConfigError, match="Failed to parse"):
        load_chipzen_config([cfg_path])


def test_load_missing_section_raises(tmp_path):
    """A file with no ``[external_api]`` section is a hard error.

    Silent fallback to ``None`` would mask the dev's typo (e.g. they
    wrote ``[chipzen]`` instead of ``[external_api]``) — the whole
    point of having a config file is to be explicit about what's set.
    """
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, '[other]\nfoo = "bar"\n')
    with pytest.raises(ChipzenConfigError, match=r"\[external_api\]"):
        load_chipzen_config([cfg_path])


def test_load_token_wrong_type_raises(tmp_path):
    """If ``token`` is an int / list / table instead of a string, raise."""
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, "[external_api]\ntoken = 42\n")
    with pytest.raises(ChipzenConfigError, match="token must be a string"):
        load_chipzen_config([cfg_path])


def test_load_url_wrong_type_raises(tmp_path):
    """If ``url`` is the wrong type, raise."""
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, "[external_api]\nurl = true\n")
    with pytest.raises(ChipzenConfigError, match="url must be a string"):
        load_chipzen_config([cfg_path])


def test_load_section_as_array_raises(tmp_path):
    """``[[external_api]]`` (array-of-tables) is the wrong shape; raise."""
    cfg_path = _write_toml(
        tmp_path / CONFIG_FILENAME,
        '[[external_api]]\ntoken = "cz_extbot_x"\n',
    )
    with pytest.raises(ChipzenConfigError, match=r"\[external_api\] must be a table"):
        load_chipzen_config([cfg_path])


# ---------------------------------------------------------------------------
# Default search-path discovery (no explicit list)
# ---------------------------------------------------------------------------


def test_default_discovery_picks_up_cwd_file(tmp_path, monkeypatch):
    """With cwd-set to a directory containing chipzen.toml, the SDK
    discovers it without any explicit search-path override.

    HOME is also redirected to an empty dir so the test doesn't depend
    on whoever's running it.
    """
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, '[external_api]\ntoken = "cz_extbot_x"\n')
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))

    cfg = load_chipzen_config()
    assert cfg is not None
    assert cfg.path == cfg_path
    assert cfg.token == "cz_extbot_x"


def test_default_discovery_returns_none_when_nothing_exists(tmp_path, monkeypatch):
    """With cwd + home both pointing at empty dirs, default discovery
    returns ``None`` — no spurious 'file not found' errors."""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))

    # Note: on POSIX, /etc/chipzen/chipzen.toml might exist on the
    # host. We can't easily mock it without monkeypatching the whole
    # ``_search_paths`` helper. Skip the POSIX strictness when /etc has
    # a real config — the test is still meaningful elsewhere.
    if os.name == "posix" and (Path("/etc/chipzen") / CONFIG_FILENAME).is_file():
        pytest.skip("real /etc/chipzen/chipzen.toml on host shadows the test")
    cfg = load_chipzen_config()
    assert cfg is None


def test_default_discovery_falls_through_to_home(tmp_path, monkeypatch):
    """cwd has no file, but home does -> home file is used."""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    home_dir = tmp_path / "home"
    chipzen_dir = home_dir / ".chipzen"
    home_file = _write_toml(
        chipzen_dir / CONFIG_FILENAME, '[external_api]\ntoken = "cz_extbot_home"\n'
    )
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))

    cfg = load_chipzen_config()
    assert cfg is not None
    assert cfg.path == home_file
    assert cfg.token == "cz_extbot_home"


# ---------------------------------------------------------------------------
# resolve_token + resolve_url precedence
# ---------------------------------------------------------------------------


def test_resolve_token_explicit_wins_over_config():
    """Spec: explicit ``token=`` kwarg always wins over config-file value."""
    cfg = ChipzenConfig(path=Path("/x"), token="cz_extbot_config")
    assert resolve_token(explicit_token="cz_extbot_kwarg", config=cfg) == "cz_extbot_kwarg"


def test_resolve_token_empty_string_explicit_wins_over_config():
    """Edge case: ``token=""`` is an explicit value, not 'unspecified'.

    Pythonic ``None``-vs-``""`` distinction matters here — a dev who
    explicitly passes the empty string (e.g. for localhost sidecar)
    expects to NOT pick up the config-file token.
    """
    cfg = ChipzenConfig(path=Path("/x"), token="cz_extbot_config")
    assert resolve_token(explicit_token="", config=cfg) == ""


def test_resolve_token_config_used_when_no_kwarg():
    """No explicit token -> config-file value wins."""
    cfg = ChipzenConfig(path=Path("/x"), token="cz_extbot_config")
    assert resolve_token(explicit_token=None, config=cfg) == "cz_extbot_config"


def test_resolve_token_none_when_nothing_set():
    """No explicit, no ticket, no config -> ``None``. Caller decides
    whether that's a hard error."""
    assert resolve_token(explicit_token=None) is None
    assert resolve_token(explicit_token=None, config=None) is None


def test_resolve_token_explicit_ticket_suppresses_config_token():
    """If a ticket is being used, the config-file token must NOT leak
    through — ticket-auth and token-auth are mutually exclusive on the
    wire."""
    cfg = ChipzenConfig(path=Path("/x"), token="cz_extbot_config")
    assert resolve_token(explicit_token=None, explicit_ticket="ticket-abc", config=cfg) is None


def test_resolve_url_explicit_wins_over_config():
    cfg = ChipzenConfig(path=Path("/x"), url="wss://config.example/ws")
    assert (
        resolve_url(explicit_url="wss://kwarg.example/ws", config=cfg) == "wss://kwarg.example/ws"
    )


def test_resolve_url_config_used_when_no_kwarg():
    cfg = ChipzenConfig(path=Path("/x"), url="wss://config.example/ws")
    assert resolve_url(explicit_url=None, config=cfg) == "wss://config.example/ws"


def test_resolve_url_none_when_nothing_set():
    assert resolve_url(explicit_url=None, config=None) is None
    assert resolve_url(explicit_url=None) is None


# ---------------------------------------------------------------------------
# run_bot integration: config-file fallback + error surfaces
# ---------------------------------------------------------------------------


class _NoopBot(ChipzenBot):
    """Minimal bot for run_bot integration tests."""

    def decide(self, state):  # type: ignore[override]
        from chipzen.models import Action

        return Action.fold()


class _CapturingWS:
    """Mock websocket recording everything sent. Iterates immediately
    so ``_run_session`` exits after handshake."""

    def __init__(self):
        self.sent: list[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def recv(self) -> str:
        # Return an "error" frame so _run_session bails after authenticate.
        return '{"type":"error","code":"x","message":"test exit"}'

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _CapturingConnect:
    """Stub for ``websockets.connect`` capturing the URL it was called with."""

    def __init__(self):
        self.urls: list[str] = []
        self.ws = _CapturingWS()

    def __call__(self, url, *, user_agent_header=None):  # noqa: D401
        self.urls.append(url)
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


def _bypass_config_discovery(monkeypatch, tmp_path) -> None:
    """Redirect cwd + home to empty dirs so default config discovery
    finds nothing. Tests that DON'T want config-file influence should
    call this first.
    """
    empty_cwd = tmp_path / "empty_cwd"
    empty_cwd.mkdir(exist_ok=True)
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(exist_ok=True)
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))
    # If a real /etc/chipzen file exists on the host (rare), the test
    # asserting "no config found" may need to live with it; we don't
    # try to mock the absolute /etc path.


@pytest.mark.asyncio
async def test_run_bot_uses_config_token_when_no_kwarg(monkeypatch, tmp_path):
    """When no ``token=`` kwarg is passed, the chipzen.toml token is
    sent on the authenticate frame."""
    stub = _install_connect_stub(monkeypatch)

    cfg = ChipzenConfig(path=tmp_path / CONFIG_FILENAME, token="cz_extbot_from_config")

    await run_bot(
        "ws://localhost/ws/match/abc/bot",
        _NoopBot(),
        config=cfg,
        max_retries=0,
    )

    # _run_session sends exactly one frame before bailing on the error
    # response: the authenticate frame.
    assert len(stub.ws.sent) == 1
    import json

    auth = json.loads(stub.ws.sent[0])
    assert auth["type"] == "authenticate"
    assert auth["token"] == "cz_extbot_from_config"


@pytest.mark.asyncio
async def test_run_bot_explicit_token_overrides_config(monkeypatch, tmp_path):
    """Explicit ``token=`` kwarg always wins over the config-file value."""
    stub = _install_connect_stub(monkeypatch)

    cfg = ChipzenConfig(path=tmp_path / CONFIG_FILENAME, token="cz_extbot_config_value")

    await run_bot(
        "ws://localhost/ws/match/abc/bot",
        _NoopBot(),
        token="cz_extbot_kwarg_value",
        config=cfg,
        max_retries=0,
    )

    import json

    auth = json.loads(stub.ws.sent[0])
    assert auth["token"] == "cz_extbot_kwarg_value"


@pytest.mark.asyncio
async def test_run_bot_config_url_used_when_no_url_kwarg(monkeypatch, tmp_path):
    """When ``url=None`` (or omitted), the config-file ``url`` is used
    for the WebSocket connect call."""
    stub = _install_connect_stub(monkeypatch)

    cfg = ChipzenConfig(
        path=tmp_path / CONFIG_FILENAME,
        token="cz_extbot_x",
        url="ws://config.example/ws/match/cfg/bot",
    )

    await run_bot(
        bot=_NoopBot(),
        config=cfg,
        max_retries=0,
    )

    assert stub.urls == ["ws://config.example/ws/match/cfg/bot"]


@pytest.mark.asyncio
async def test_run_bot_explicit_url_overrides_config_url(monkeypatch, tmp_path):
    """Explicit ``url`` arg always wins over config-file URL."""
    stub = _install_connect_stub(monkeypatch)

    cfg = ChipzenConfig(
        path=tmp_path / CONFIG_FILENAME,
        url="ws://config.example/ws/match/cfg/bot",
    )

    await run_bot(
        "ws://kwarg.example/ws/match/kw/bot",
        _NoopBot(),
        config=cfg,
        token="t",
        max_retries=0,
    )

    assert stub.urls == ["ws://kwarg.example/ws/match/kw/bot"]


@pytest.mark.asyncio
async def test_run_bot_no_url_and_no_config_raises(monkeypatch, tmp_path):
    """Spec: missing chipzen.toml + no kwarg URL -> clear error, not a
    silent failure."""
    _bypass_config_discovery(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="requires a WebSocket URL"):
        await run_bot(bot=_NoopBot())


@pytest.mark.asyncio
async def test_run_bot_no_bot_raises(monkeypatch, tmp_path):
    """``bot`` is required even when config is fully populated."""
    _bypass_config_discovery(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="requires a Bot instance"):
        await run_bot("ws://x/ws/match/a/b")


@pytest.mark.asyncio
async def test_run_bot_invokes_discovery_when_token_missing(monkeypatch, tmp_path):
    """If both ``token`` and ``url`` are explicitly passed, discovery is
    skipped — bots shouldn't pay the fs-stat cost. Otherwise, discovery
    fires automatically.
    """
    _install_connect_stub(monkeypatch)

    calls: list[None] = []

    def _spy_load(*args, **kwargs):
        calls.append(None)
        return None

    monkeypatch.setattr("chipzen.client.load_chipzen_config", _spy_load)

    # Both explicit -> NO discovery.
    await run_bot(
        "ws://localhost/ws/match/abc/bot",
        _NoopBot(),
        token="explicit",
        max_retries=0,
    )
    assert calls == []

    # Token missing -> discovery fires.
    await run_bot(
        "ws://localhost/ws/match/abc/bot",
        _NoopBot(),
        max_retries=0,
    )
    assert calls == [None]


@pytest.mark.asyncio
async def test_run_bot_malformed_config_propagates(monkeypatch, tmp_path):
    """If discovery finds a malformed chipzen.toml, ``run_bot`` re-raises
    the parse error — silent fallback to defaults would hide the typo."""
    cfg_path = _write_toml(tmp_path / CONFIG_FILENAME, "this == is = not toml\n")
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))

    with pytest.raises(ChipzenConfigError, match="Failed to parse"):
        await run_bot(bot=_NoopBot(), token="cz_extbot_x")
    # Discovery itself raises before we get to URL resolution, because
    # the URL is missing AND the config is in a state we can't parse.


@pytest.mark.asyncio
async def test_run_bot_missing_section_propagates(monkeypatch, tmp_path):
    """A chipzen.toml that's syntactically fine but has no [external_api]
    section raises a clear error pointing at the missing section."""
    _write_toml(tmp_path / CONFIG_FILENAME, '[other]\nx = "y"\n')
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))

    with pytest.raises(ChipzenConfigError, match=r"\[external_api\]"):
        await run_bot("ws://x/ws/match/a/b", _NoopBot())


@pytest.mark.asyncio
async def test_run_bot_explicit_config_overrides_discovery(monkeypatch, tmp_path):
    """Passing ``config=ChipzenConfig(...)`` skips discovery entirely —
    useful for bots that want to load the file themselves once and
    re-use the parsed object across multiple ``run_bot`` calls."""
    # Drop a poisoned config on disk that would raise if discovered.
    _write_toml(tmp_path / CONFIG_FILENAME, "not valid\n")
    monkeypatch.chdir(tmp_path)

    spy_called: list[None] = []

    def _spy(*args, **kwargs):
        spy_called.append(None)
        return None

    monkeypatch.setattr("chipzen.client.load_chipzen_config", _spy)

    _install_connect_stub(monkeypatch)

    explicit_cfg = ChipzenConfig(path=Path("/dev/null"), token="cz_extbot_explicit")
    await run_bot(
        "ws://x/ws/match/a/b",
        _NoopBot(),
        config=explicit_cfg,
        max_retries=0,
    )

    # Discovery was NOT called because we supplied an explicit config.
    assert spy_called == []
