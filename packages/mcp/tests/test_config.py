"""Tests for env-based config loading."""

import pytest

from chipzen_mcp.config import (
    BRIDGE_PLAYABLE_GAMES,
    ENV_BOT_ID,
    ENV_ENV,
    ENV_LOBBY_URL,
    ENV_SUPPORTED_GAMES,
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


# --- supported_games declaration (chipzen-ai/Chipzen#4754) -----------------


def test_supported_games_defaults_to_what_the_bridge_can_play() -> None:
    cfg = load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID})
    assert cfg.supported_games == BRIDGE_PLAYABLE_GAMES == ("poker",)


@pytest.mark.parametrize("raw", ["", "   ", ",", " , ,"])
def test_blank_supported_games_uses_the_default(raw: str) -> None:
    cfg = load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID, ENV_SUPPORTED_GAMES: raw})
    assert cfg.supported_games == BRIDGE_PLAYABLE_GAMES


def test_supported_games_override_is_trimmed_and_deduped() -> None:
    cfg = load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID, ENV_SUPPORTED_GAMES: " poker, poker "})
    assert cfg.supported_games == ("poker",)


@pytest.mark.parametrize("raw", ["draw27", "poker,ofc", "Poker", "nlhe"])
def test_supported_games_cannot_claim_a_game_the_bridge_cannot_play(raw: str) -> None:
    """Declaring a variant would seat the agent at a table whose actions the
    ``act`` tool cannot express -- the silent fold-out the gate exists to stop."""
    with pytest.raises(McpConfigError, match=ENV_SUPPORTED_GAMES):
        load_config({ENV_TOKEN: TOKEN, ENV_BOT_ID: BOT_ID, ENV_SUPPORTED_GAMES: raw})
