"""Chipzen Poker Bot SDK -- build, test, and deploy poker bots for the Chipzen platform."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# Defined BEFORE the submodule imports below: ``chipzen.client`` and
# ``chipzen.external`` read ``chipzen.__version__`` at import time (for the
# default handshake ``client_version``), so it must exist on the partially
# initialized package object when those imports run.
#
# The explicit annotation matters: without it mypy cannot infer a single type
# for a name assigned in both branches of a try/except, and every module that
# uses ``__version__`` as a default argument fails with `has-type`.
__version__: str
try:
    # Single source of truth: the installed distribution's version, which
    # hatchling reads straight from pyproject's ``version``. Deriving it here
    # means the runtime constant and the published wheel cannot drift -- the
    # exact failure this replaces (#89: a hand-maintained "0.3.0" was still
    # advertised in the WS User-Agent of the published 0.3.1 wheel).
    __version__ = _dist_version("chipzen-bot")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled source tree
    __version__ = "0.0.0+unknown"

from chipzen.bot import ChipzenBot
from chipzen.client import BotDecisionError, run_bot
from chipzen.config import ChipzenConfig, ChipzenConfigError, load_chipzen_config
from chipzen.connect import ConnectionConfig, connect_to_chipzen
from chipzen.external import run_external_bot
from chipzen.models import Action, Card, GameState, Player, RoundStart, TurnResult
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

# `Bot` is the canonical public name. `ChipzenBot` is the historical
# internal class name and remains exported for backward compatibility;
# they are the *same* class object (`Bot is ChipzenBot` evaluates True).
# Always prefer `from chipzen import Bot` in user code.
Bot = ChipzenBot

__all__ = [
    # Bot authoring
    "Bot",
    "ChipzenBot",
    "Action",
    "Card",
    "GameState",
    "Player",
    "RoundStart",
    "TurnResult",
    # Containerized / direct-match path
    "run_bot",
    "BotDecisionError",
    # External-API remote-play path
    "run_external_bot",
    "connect_to_chipzen",
    "ConnectionConfig",
    "ChipzenConfig",
    "ChipzenConfigError",
    "load_chipzen_config",
    "RetryPolicy",
    "DEFAULT_RETRY_POLICY",
]
