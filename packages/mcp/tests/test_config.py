"""Tests for env-based config loading."""

import pytest

from chipzen_mcp.config import (
    ENV_BOT_ID,
    ENV_ENV,
    ENV_LOBBY_URL,
    ENV_TOKEN,
    McpConfigError,
    load_config,
)

TOKEN = "cz_extbot_" + "x" * 32
BOT_ID = "8f3a1c2e-0000-0000-0000-000000000000"


def test_happy_path() -> None:
    cfg = load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID, ENV_ENV: "staging"})
    assert cfg.token == TOKEN
    assert cfg.bot_id == BOT_ID
    assert cfg.env == "staging"
    assert cfg.lobby_url is None


def test_env_defaults_to_none_for_sdk_resolution() -> None:
    cfg = load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID})
    assert cfg.env is None


def test_missing_token_names_the_variable() -> None:
    with pytest.raises(McpConfigError, match=ENV_TOKEN):
        load_config({ENV_BOT_ID: BOT_ID})


def test_non_extbot_token_rejected_fast() -> None:
    with pytest.raises(McpConfigError, match="cz_extbot_"):
        load_config({ENV_TOKEN: "eyJhbGciOi-not-a-bot-token", ENV_BOT_ID: BOT_ID})


def test_missing_bot_id_names_the_variable() -> None:
    with pytest.raises(McpConfigError, match=ENV_BOT_ID):
        load_config({ENV_TOKEN: TOKEN})


def test_lobby_url_substitutes_for_bot_id() -> None:
    url = "ws://localhost:8001/ws/external/bot/" + BOT_ID
    cfg = load_config({ENV_TOKEN: TOKEN, ENV_LOBBY_URL: url})
    assert cfg.lobby_url == url
    assert cfg.bot_id == ""


def test_bad_env_rejected() -> None:
    with pytest.raises(McpConfigError, match="production"):
        load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID, ENV_ENV: "production"})
