"""Tests for ``chipzen.toml`` discovery + parsing + resolution helpers.

Covers the External-API Issue 23 spec
(``management/ophir-track/external-api-issue-breakdown.md`` §23,
chipzen-ai/chipzen-sdk#42):

- discovery search order (cwd > ~/.chipzen/ > /etc/chipzen/)
- parsing recognized fields ([external_api].token + [external_api].url)
- precedence rules (explicit token/url kwarg always wins over config-file)
- error surfaces (missing section, malformed TOML, wrong types)
- POSIX-only /etc behavior (skipped on Windows)

End-to-end consumption of these helpers by ``run_external_bot`` (config-file
fallback + explicit override) is covered in ``test_external.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
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
