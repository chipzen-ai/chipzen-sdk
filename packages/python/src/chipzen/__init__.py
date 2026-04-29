"""Chipzen Poker Bot SDK -- build, test, and deploy poker bots for the Chipzen platform."""

from chipzen.bot import ChipzenBot
from chipzen.models import Action, Card, GameState, Player, RoundStart, TurnResult

# Convenience alias matching the public API: `from chipzen import Bot`
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
