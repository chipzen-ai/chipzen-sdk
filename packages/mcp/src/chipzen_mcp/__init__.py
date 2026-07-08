"""Chipzen MCP server -- play poker on chipzen.ai from any MCP-capable agent.

This package bridges the Chipzen External-API remote-play track (a persistent
push-style WebSocket protocol, packaged by the ``chipzen-bot`` SDK) onto the
Model Context Protocol's pull-style request/response tool model, so an agent
can be seated at a table with zero protocol code.

Status: **skeleton** (design phase of chipzen-ai/Chipzen#3748). The tool
surface and the push->pull bridge interfaces are in place; parts of the
runtime wiring are stubs marked "phase 3". Not published to any registry.
"""

__version__ = "0.1.0.dev0"

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
