"""Chipzen Poker Bot SDK -- build, test, and deploy poker bots for the Chipzen platform."""

from chipzen.bot import ChipzenBot
from chipzen.models import Action, Card, GameState, Player, RoundStart, TurnResult

# `Bot` is the canonical public name. `ChipzenBot` is the historical
# internal class name and remains exported for backward compatibility;
# they are the *same* class object (`Bot is ChipzenBot` evaluates True).
# Always prefer `from chipzen import Bot` in user code.
Bot = ChipzenBot

__all__ = [
    "Bot",
    "ChipzenBot",
    "Action",
    "Card",
    "GameState",
    "Player",
    "RoundStart",
    "TurnResult",
]

__version__ = "0.2.0"
