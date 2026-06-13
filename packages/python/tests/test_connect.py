"""Tests for :func:`chipzen.connect.connect_to_chipzen`.

Covers the External-API Issue 24 spec
(``management/ophir-track/external-api-issue-breakdown.md`` §24,
chipzen-ai/chipzen-sdk#43):

- env → URL mapping (prod / staging / local)
- ``CHIPZEN_ENV`` env-var override
- explicit ``env=`` arg overrides ``CHIPZEN_ENV``
- ``chipzen.toml`` ``[external_api].url`` overrides env-derived URL
- ``bot_id`` is required (clear error on missing / empty)
- unknown env value → clear error (typo guard)
- precedence interaction between explicit arg, env var, and config file

The precedence rule under test is: ``chipzen.toml`` URL > explicit
``env=`` > ``CHIPZEN_ENV`` > default of ``prod``. See the module
docstring on ``chipzen.connect`` for the rationale.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import chipzen
from chipzen.config import ChipzenConfig, ChipzenConfigError
from chipzen.connect import (
    ENV_NAMES,
    ENV_VAR_NAME,
    ConnectionConfig,
    _resolve_env_name,
    _url_for_env,
    connect_to_chipzen,
)
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

# Sample bot id used across the test file. UUID-shaped but the helper
# doesn't validate the format — any non-empty string is accepted —
# so this is mostly to make the resulting URLs look realistic.
BOT_ID = "abc12345-6789-4def-9012-3456789abcde"


def _bypass_config_discovery(monkeypatch, tmp_path) -> None:
    """Redirect cwd + home to empty dirs so default config discovery
    finds nothing. Tests that don't want config-file influence call
    this first.
    """
    empty_cwd = tmp_path / "empty_cwd"
    empty_cwd.mkdir(exist_ok=True)
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(exist_ok=True)
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty_home))


def _clear_chipzen_env(monkeypatch) -> None:
    """Ensure ``$CHIPZEN_ENV`` is unset for the test. ``delenv`` with
    ``raising=False`` is idempotent and works whether the var is set
    in the host shell or not."""
    monkeypatch.delenv(ENV_VAR_NAME, raising=False)


# ---------------------------------------------------------------------------
# Public-API exports
# ---------------------------------------------------------------------------


def test_public_api_exported_from_chipzen():
    """``connect_to_chipzen`` and ``ConnectionConfig`` are part of the
    canonical ``chipzen`` namespace alongside ``Bot`` / ``RetryPolicy``."""
    assert chipzen.connect_to_chipzen is connect_to_chipzen
    assert chipzen.ConnectionConfig is ConnectionConfig
    for name in ("connect_to_chipzen", "ConnectionConfig"):
        assert name in chipzen.__all__


def test_env_names_are_canonical_three():
    """The recognized env list must match the spec exactly (no extras)."""
    assert ENV_NAMES == ("prod", "staging", "local")


def test_env_var_name_is_chipzen_env():
    """The env-var name is the spec-mandated ``CHIPZEN_ENV``."""
    assert ENV_VAR_NAME == "CHIPZEN_ENV"


def test_connection_config_is_frozen():
    """``ConnectionConfig`` is immutable so it's safe to share across
    coroutines."""
    cfg = ConnectionConfig(
        url="wss://x/ws/external/bot/y",
        token=None,
        retry_policy=DEFAULT_RETRY_POLICY,
        env="prod",
        config=None,
    )
    with pytest.raises((AttributeError, Exception)):
        cfg.url = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Env → URL mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name,expected_prefix,scheme",
    [
        ("prod", "wss://chipzen.ai", "wss"),
        ("staging", "wss://staging.chipzen.ai", "wss"),
        ("local", "ws://localhost:8001", "ws"),
    ],
)
def test_url_for_env_matches_spec(env_name, expected_prefix, scheme):
    """Each env maps to the canonical URL template from the spec."""
    url = _url_for_env(env_name, bot_id=BOT_ID)
    assert url == f"{expected_prefix}/ws/external/bot/{BOT_ID}"
    # ``local`` is the only env that uses unencrypted ``ws://`` — prod
    # and staging both terminate TLS at Cloudflare. Lock this in so a
    # future config edit can't silently downgrade prod.
    assert url.startswith(f"{scheme}://")


def test_prod_url_explicit(monkeypatch, tmp_path):
    """``env="prod"`` resolves to the prod lobby URL."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    cfg = connect_to_chipzen(bot_id=BOT_ID, env="prod")
    assert cfg.url == f"wss://chipzen.ai/ws/external/bot/{BOT_ID}"
    assert cfg.env == "prod"


def test_staging_url_explicit(monkeypatch, tmp_path):
    """``env="staging"`` resolves to the staging lobby URL."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    cfg = connect_to_chipzen(bot_id=BOT_ID, env="staging")
    assert cfg.url == f"wss://staging.chipzen.ai/ws/external/bot/{BOT_ID}"
    assert cfg.env == "staging"


def test_local_url_explicit(monkeypatch, tmp_path):
    """``env="local"`` resolves to the localhost dev URL on port 8001."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    cfg = connect_to_chipzen(bot_id=BOT_ID, env="local")
    assert cfg.url == f"ws://localhost:8001/ws/external/bot/{BOT_ID}"
    assert cfg.env == "local"


def test_default_env_is_prod_when_nothing_set(monkeypatch, tmp_path):
    """No explicit env + no ``CHIPZEN_ENV`` + no config file -> ``prod``.

    Conservative default. Mistakes against staging are cheaper than
    mistakes against prod, but "no env specified" most often means "I
    am a dev who just ``pip install``-ed the SDK and want to talk to
    the real platform" — so defaulting to prod matches the principle
    of least surprise.
    """
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    cfg = connect_to_chipzen(bot_id=BOT_ID)
    assert cfg.url == f"wss://chipzen.ai/ws/external/bot/{BOT_ID}"
    assert cfg.env == "prod"


# ---------------------------------------------------------------------------
# ``CHIPZEN_ENV`` environment variable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_value", ["prod", "staging", "local"])
def test_chipzen_env_var_picks_up_env(monkeypatch, tmp_path, env_value):
    """When ``$CHIPZEN_ENV`` is set and no explicit ``env=`` is passed,
    the env var drives URL resolution."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_VAR_NAME, env_value)

    cfg = connect_to_chipzen(bot_id=BOT_ID)
    assert cfg.env == env_value
    assert f"/ws/external/bot/{BOT_ID}" in cfg.url


def test_explicit_env_arg_wins_over_chipzen_env_var(monkeypatch, tmp_path):
    """Spec: explicit non-default ``env=`` arg overrides ``CHIPZEN_ENV``.

    This is the key precedence rule the user request called out: a dev
    who hard-codes ``env="staging"`` in their script should NOT have
    that flipped to prod by ``CHIPZEN_ENV=prod`` in the shell. The
    explicit value is the dev's clear intent.
    """
    _bypass_config_discovery(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_VAR_NAME, "prod")

    cfg = connect_to_chipzen(bot_id=BOT_ID, env="staging")
    assert cfg.env == "staging"
    assert cfg.url == f"wss://staging.chipzen.ai/ws/external/bot/{BOT_ID}"


def test_explicit_env_equals_prod_still_wins_over_env_var(monkeypatch, tmp_path):
    """Even when the explicit value matches the function default
    (``env="prod"``), it counts as explicit and wins over a contrary
    ``CHIPZEN_ENV``.

    Functionally equivalent to the previous test in URL terms (both
    resolve to prod), but tests the mechanism: passing the default
    value explicitly is NOT the same as not passing anything.
    """
    _bypass_config_discovery(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_VAR_NAME, "staging")

    cfg = connect_to_chipzen(bot_id=BOT_ID, env="prod")
    assert cfg.env == "prod"
    assert cfg.url == f"wss://chipzen.ai/ws/external/bot/{BOT_ID}"


def test_empty_chipzen_env_treated_as_unset(monkeypatch, tmp_path):
    """``CHIPZEN_ENV=`` (set but empty) falls back to the default rather
    than tripping the unknown-env error. Accidental shell exports with
    no value shouldn't break the helper."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_VAR_NAME, "")

    cfg = connect_to_chipzen(bot_id=BOT_ID)
    assert cfg.env == "prod"


def test_unknown_chipzen_env_var_raises(monkeypatch, tmp_path):
    """A garbage value in ``$CHIPZEN_ENV`` surfaces immediately —
    listing the valid values — rather than silently falling back to
    prod and confusing the dev about why their staging bot connected
    to the prod lobby."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_VAR_NAME, "production")  # close but no

    with pytest.raises(ValueError, match="not a recognized environment"):
        connect_to_chipzen(bot_id=BOT_ID)


def test_unknown_explicit_env_raises(monkeypatch, tmp_path):
    """Typo in the explicit ``env=`` arg surfaces immediately. Lists
    the legal values in the error message so the fix is obvious."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    with pytest.raises(ValueError, match="Unknown env") as exc_info:
        connect_to_chipzen(bot_id=BOT_ID, env="prd")  # type: ignore[arg-type]
    # The error message must list all three legal values so the typo
    # is fixable without reading the source.
    assert "prod" in str(exc_info.value)
    assert "staging" in str(exc_info.value)
    assert "local" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ``chipzen.toml`` ``[external_api].url`` override
# ---------------------------------------------------------------------------


def test_config_file_url_overrides_env_derived_url(monkeypatch, tmp_path):
    """When a ``chipzen.toml`` ``[external_api].url`` is set, it ALWAYS
    wins over the env-derived URL.

    Matches the precedence pattern Issue 23 chose: the config-file value
    is the most explicit "this is the URL I want" signal. A dev who's
    pointed their config file at a custom endpoint shouldn't get that
    silently swapped out just because they also passed ``env=``.
    """
    _clear_chipzen_env(monkeypatch)

    cfg_path = tmp_path / "chipzen.toml"
    cfg_path.write_text(
        '[external_api]\nurl = "wss://custom.example/ws/external/bot/xyz"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
    (tmp_path / "empty").mkdir(exist_ok=True)

    # Even with explicit ``env="staging"``, the config file URL wins.
    result = connect_to_chipzen(bot_id=BOT_ID, env="staging")
    assert result.url == "wss://custom.example/ws/external/bot/xyz"
    # ``env`` on the return value is ``None`` because the URL was
    # supplied verbatim — there's no env mapping to report.
    assert result.env is None
    # The config object is plumbed through on the return value so
    # callers can pass it to ``run_bot`` and skip the second
    # filesystem stat.
    assert result.config is not None
    assert result.config.url == "wss://custom.example/ws/external/bot/xyz"


def test_config_file_token_propagates(monkeypatch, tmp_path):
    """The config-file token is surfaced on ``ConnectionConfig.token``
    so callers can hand it to :func:`run_bot` in one expression."""
    _clear_chipzen_env(monkeypatch)

    cfg_path = tmp_path / "chipzen.toml"
    cfg_path.write_text(
        '[external_api]\ntoken = "cz_extbot_from_file"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
    (tmp_path / "empty").mkdir(exist_ok=True)

    result = connect_to_chipzen(bot_id=BOT_ID, env="staging")
    assert result.token == "cz_extbot_from_file"
    # No URL in the config file -> env-derived URL is used.
    assert result.url == f"wss://staging.chipzen.ai/ws/external/bot/{BOT_ID}"
    assert result.env == "staging"


def test_no_config_file_token_is_none(monkeypatch, tmp_path):
    """When no config file is discoverable, ``token`` on the result is
    ``None``. The caller must pass ``token=`` to :func:`run_bot`
    separately."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    result = connect_to_chipzen(bot_id=BOT_ID, env="prod")
    assert result.token is None
    assert result.config is None


def test_malformed_config_file_propagates(monkeypatch, tmp_path):
    """A malformed ``chipzen.toml`` on the search path surfaces as
    :class:`ChipzenConfigError` — same behavior as :func:`run_bot`.
    Silent fallback would mask the typo."""
    _clear_chipzen_env(monkeypatch)

    cfg_path = tmp_path / "chipzen.toml"
    cfg_path.write_text("not = = valid toml\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
    (tmp_path / "empty").mkdir(exist_ok=True)

    with pytest.raises(ChipzenConfigError, match="Failed to parse"):
        connect_to_chipzen(bot_id=BOT_ID, env="prod")


def test_explicit_config_skips_discovery(monkeypatch, tmp_path):
    """When the caller passes a pre-loaded ``config=``, the helper does
    NOT invoke discovery — useful when the dev already loaded it once
    and wants to share the parsed object."""
    _clear_chipzen_env(monkeypatch)

    # Plant a poisoned config on disk that would raise if discovered.
    (tmp_path / "chipzen.toml").write_text("not valid\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    spy_called: list[None] = []

    def _spy(*args, **kwargs):
        spy_called.append(None)
        return None

    monkeypatch.setattr("chipzen.connect.load_chipzen_config", _spy)

    explicit_cfg = ChipzenConfig(path=Path("/dev/null"), token="cz_extbot_explicit")
    result = connect_to_chipzen(bot_id=BOT_ID, env="prod", config=explicit_cfg)

    # Discovery was NOT called because we supplied an explicit config.
    assert spy_called == []
    # The explicit token comes through on the return value.
    assert result.token == "cz_extbot_explicit"


# ---------------------------------------------------------------------------
# ``bot_id`` validation
# ---------------------------------------------------------------------------


def test_missing_bot_id_raises(monkeypatch, tmp_path):
    """An empty ``bot_id`` is a hard error. Otherwise the resulting URL
    would have a trailing slash with no bot id (``/ws/external/bot/``)
    which the lobby endpoint rejects with a less helpful 404."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    with pytest.raises(ValueError, match="bot_id"):
        connect_to_chipzen(bot_id="", env="prod")


def test_non_string_bot_id_raises(monkeypatch, tmp_path):
    """Numeric or ``None`` ``bot_id`` is rejected with the same error.
    Defends against the easy mistake of passing an int by accident."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    with pytest.raises(ValueError, match="bot_id"):
        connect_to_chipzen(bot_id=None, env="prod")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="bot_id"):
        connect_to_chipzen(bot_id=12345, env="prod")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Retry policy plumbing
# ---------------------------------------------------------------------------


def test_default_retry_policy_used_when_unspecified(monkeypatch, tmp_path):
    """If the caller doesn't pass ``retry_policy=``, the result carries
    the shared :data:`DEFAULT_RETRY_POLICY` — same defaults as
    :func:`run_bot`."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    cfg = connect_to_chipzen(bot_id=BOT_ID, env="prod")
    assert cfg.retry_policy is DEFAULT_RETRY_POLICY


def test_custom_retry_policy_propagates(monkeypatch, tmp_path):
    """A caller-supplied :class:`RetryPolicy` is preserved verbatim."""
    _bypass_config_discovery(monkeypatch, tmp_path)
    _clear_chipzen_env(monkeypatch)

    custom = RetryPolicy(
        max_reconnect_attempts=10,
        initial_backoff_ms=250,
        max_backoff_ms=60_000,
        backoff_multiplier=1.5,
    )
    cfg = connect_to_chipzen(bot_id=BOT_ID, env="prod", retry_policy=custom)
    assert cfg.retry_policy is custom


# ---------------------------------------------------------------------------
# Internal env-resolution helper
# ---------------------------------------------------------------------------


def test_resolve_env_name_none_env_var_returns_default():
    """Helper path: no explicit, no env var -> default of prod."""
    assert _resolve_env_name(None, env_var_value=None) == "prod"


def test_resolve_env_name_env_var_used_when_no_explicit():
    """Helper path: env var consulted when explicit arg is ``None``."""
    assert _resolve_env_name(None, env_var_value="staging") == "staging"


def test_resolve_env_name_explicit_wins_over_env_var():
    """Helper path: explicit non-``None`` arg overrides env var."""
    assert _resolve_env_name("local", env_var_value="prod") == "local"


def test_resolve_env_name_empty_env_var_falls_through():
    """Helper path: empty-string env var falls through to default."""
    assert _resolve_env_name(None, env_var_value="") == "prod"


def test_resolve_env_name_bad_env_var_raises():
    """Helper path: unknown env-var value raises with a clear message."""
    with pytest.raises(ValueError, match="not a recognized environment"):
        _resolve_env_name(None, env_var_value="bogus")


def test_resolve_env_name_bad_explicit_raises():
    """Helper path: unknown explicit arg raises with a clear message."""
    with pytest.raises(ValueError, match="Unknown env"):
        _resolve_env_name("bogus", env_var_value=None)  # type: ignore[arg-type]
