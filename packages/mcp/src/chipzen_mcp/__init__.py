"""Chipzen MCP server -- play poker on chipzen.ai from any MCP-capable agent.

This package bridges the Chipzen External-API remote-play track (a persistent
push-style WebSocket protocol, packaged by the ``chipzen-bot`` SDK) onto the
Model Context Protocol's pull-style request/response tool model, so an agent
can be seated at a table with zero protocol code.

Status: **pre-alpha** (chipzen-ai/Chipzen#3748, runtime wiring complete).
The push->pull bridge, session lifecycle (cooperative stop, lobby-presence
surfacing), and the ``challenge_house_bot`` client are implemented; end-to-end
staging verification against the server side of chipzen-ai/Chipzen#3750 is
the remaining gate. Not published to any registry.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    # Single source of truth: the installed distribution's version (from
    # pyproject `version`). Deriving it here means the runtime constant and
    # the published wheel can never drift -- which is exactly the failure
    # this replaces (chipzen-ai/Chipzen#3885: a hand-maintained "0.1.0.dev0"
    # leaked into the outbound User-Agent of the published 0.1.0 wheel).
    __version__ = _dist_version("chipzen-mcp")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled source tree
    __version__ = "0.0.0+unknown"

from chipzen_mcp.bridge import BridgeBot, ExternalSession, TurnRegistry, TurnSnapshot
from chipzen_mcp.config import (
    ENV_BOT_ID,
    ENV_ENV,
    ENV_TOKEN,
    McpConfig,
    McpConfigError,
    load_config,
)

__all__ = [
    "__version__",
    # Bridge (push->pull) primitives
    "TurnRegistry",
    "TurnSnapshot",
    "BridgeBot",
    "ExternalSession",
    # Config
    "McpConfig",
    "McpConfigError",
    "load_config",
    "ENV_TOKEN",
    "ENV_BOT_ID",
    "ENV_ENV",
]
