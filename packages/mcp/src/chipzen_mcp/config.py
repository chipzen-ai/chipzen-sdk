"""Environment-based configuration for the Chipzen MCP server.

The MCP host (Claude Desktop, Claude Code, any MCP client) passes credentials
via the standard ``env`` block of its server config -- see ``QUICKSTART.md``.
Everything is read from environment variables; there is deliberately no
config-file discovery here (the underlying SDK supports ``chipzen.toml``, but
an MCP server config block IS the config file for this surface, and one
source of truth beats two).

Required:

* ``CHIPZEN_EXTBOT_TOKEN`` -- the long-lived ``cz_extbot_`` API token issued
  for an ``external_api`` bot you own (dashboard: bot detail page ->
  "Create token"; shown exactly once).
* ``CHIPZEN_BOT_ID`` -- that bot's UUID.

Optional:

* ``CHIPZEN_ENV`` -- ``prod`` / ``staging`` / ``local`` (default: the SDK's
  default resolution, i.e. ``prod``).
* ``CHIPZEN_LOBBY_URL`` -- explicit full lobby WS URL override
  (``wss://.../ws/external/bot/{bot_id}``); when set it wins over
  ``CHIPZEN_ENV`` derivation. Mostly for local development.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_TOKEN = "CHIPZEN_EXTBOT_TOKEN"
ENV_BOT_ID = "CHIPZEN_BOT_ID"
ENV_ENV = "CHIPZEN_ENV"
ENV_LOBBY_URL = "CHIPZEN_LOBBY_URL"

_VALID_ENVS = ("prod", "staging", "local")

#: Structural prefix of an External-API token. Checked up front so a pasted
#: Clerk JWT / session cookie fails fast with an actionable message instead
#: of a confusing 4001 at connect time.
_TOKEN_PREFIX = "cz_extbot_"


class McpConfigError(ValueError):
    """Raised when the MCP server is started with missing/invalid config."""


@dataclass(frozen=True)
class McpConfig:
    """Resolved server configuration (immutable)."""

    token: str
    bot_id: str
    env: str | None = None
    lobby_url: str | None = None


def load_config(environ: Mapping[str, str] | None = None) -> McpConfig:
    """Read and validate configuration from the environment.

    Args:
        environ: Environment mapping to read (defaults to ``os.environ``);
            injectable for tests.

    Returns:
        A validated :class:`McpConfig`.

    Raises:
        McpConfigError: With an actionable message naming the exact variable
            to set, when a required value is missing or malformed.
    """
    env_map = os.environ if environ is None else environ

    token = (env_map.get(ENV_TOKEN) or "").strip()
    if not token:
        raise McpConfigError(
            f"{ENV_TOKEN} is not set. Issue a token for your external_api bot "
            "(bot detail page -> 'Create token', or POST "
            "/api/external-api/bots/{bot_id}/tokens) and put it in the MCP "
            "server config's env block."
        )
    if not token.startswith(_TOKEN_PREFIX):
        raise McpConfigError(
            f"{ENV_TOKEN} does not look like an External-API token (expected it "
            f"to start with '{_TOKEN_PREFIX}'). Make sure you pasted the bot "
            "token, not a session credential."
        )

    bot_id = (env_map.get(ENV_BOT_ID) or "").strip()
    lobby_url = (env_map.get(ENV_LOBBY_URL) or "").strip() or None
    if not bot_id and not lobby_url:
        raise McpConfigError(
            f"{ENV_BOT_ID} is not set. Copy your external_api bot's UUID from "
            f"its dashboard detail page. (Advanced: set {ENV_LOBBY_URL} to a "
            "full lobby WS URL instead.)"
        )

    env = (env_map.get(ENV_ENV) or "").strip() or None
    if env is not None and env not in _VALID_ENVS:
        raise McpConfigError(
            f"{ENV_ENV}={env!r} is not a known environment; expected one of "
            f"{', '.join(_VALID_ENVS)}."
        )

    return McpConfig(token=token, bot_id=bot_id, env=env, lobby_url=lobby_url)
