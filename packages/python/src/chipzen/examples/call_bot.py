"""Call bot -- always calls. Never folds, never raises.

The prototypical passive opponent. If you can't beat this, your strategy
has fundamental problems.
"""

from __future__ import annotations

from chipzen.bot import ChipzenBot
from chipzen.models import Action, GameState


class CallBot(ChipzenBot):
    """Always calls. Checks when free. Never folds, never raises."""

    def decide(self, state: GameState) -> Action:
        if "check" in state.valid_actions:
            return Action.check()
        if "call" in state.valid_actions:
            return Action.call()
        # Shouldn't get here, but handle gracefully
        return Action(action=state.valid_actions[0])
