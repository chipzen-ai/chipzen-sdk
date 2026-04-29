"""Tight-aggressive bot -- plays few hands, bets big when it does.

A simplified TAG strategy:
- Folds to large preflop raises
- Checks when free, occasionally bets half-pot
- Calls with decent pot odds
- Folds when facing large bets with bad odds
"""

from __future__ import annotations

import random

from chipzen.bot import ChipzenBot
from chipzen.models import Action, GameState


class TightAggressiveBot(ChipzenBot):
    """Plays tight preflop, aggressive postflop."""

    def decide(self, state: GameState) -> Action:
        to_call = state.to_call
        pot = state.pot

        # Fold to large preflop raises
        if state.phase == "preflop" and to_call > 0:
            if pot > 0 and to_call / pot > 0.3:
                return Action.fold()

        # If we can check, sometimes bet
        if "check" in state.valid_actions:
            if random.random() < 0.3 and "raise" in state.valid_actions:
                size = max(state.min_raise, pot // 2)
                size = min(size, state.max_raise)
                return Action.raise_to(size)
            return Action.check()

        # Call with decent pot odds
        if to_call > 0 and (pot + to_call) > 0:
            pot_odds = to_call / (pot + to_call)
            if pot_odds < 0.3:
                return Action.call()
            if pot_odds < 0.5 and random.random() < 0.4:
                return Action.call()

        # Bad odds -- fold
        if "fold" in state.valid_actions:
            return Action.fold()

        # Fallback
        if "call" in state.valid_actions:
            return Action.call()
        return Action.check()
