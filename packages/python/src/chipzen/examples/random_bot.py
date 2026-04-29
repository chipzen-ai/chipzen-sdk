"""Random bot -- picks a uniformly random valid action every time.

Raises to a random amount in the valid range when raising.
Useful as a chaos baseline for testing.
"""

from __future__ import annotations

import random

from chipzen.bot import ChipzenBot
from chipzen.models import Action, GameState


class RandomBot(ChipzenBot):
    """Picks a random valid action at every decision point."""

    def decide(self, state: GameState) -> Action:
        action = random.choice(state.valid_actions)

        if action == "raise":
            amount = random.randint(state.min_raise, max(state.min_raise, state.max_raise))
            return Action.raise_to(amount)

        if action == "call":
            return Action.call()
        if action == "check":
            return Action.check()

        return Action.fold()
