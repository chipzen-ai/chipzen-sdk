# cython: language_level=3str
"""Chipzen starter bot.

Subclass `Bot`, implement `decide()`, that's it. The SDK handles the
WebSocket connection, the two-layer protocol handshake, ping/pong,
`request_id` echoing, `action_rejected` retries, and reconnect.

Replace the strategy in `decide()` with your own. Everything else can
stay as-is.
"""

from __future__ import annotations

import asyncio
import os
import sys

from chipzen import Action, Bot, GameState
from chipzen.client import run_bot


def table_position(your_seat: int, dealer_seat: int, num_players: int) -> str:
    """Derive your seat's table position from the button.

    The protocol is multiway-shaped: ``opponent_stacks`` is a LIST, so the
    table size is ``len(opponent_stacks) + 1``. Combined with ``your_seat`` and
    ``dealer_seat`` (both already on the parsed ``GameState``), that is
    everything you need to know where you sit:

        seats_after_button = (your_seat - dealer_seat) % num_players

    Heads-up is the special case the scaffold default below was written for: the
    button posts the small blind and acts first preflop. See
    docs/protocol/POKER-GAME-STATE-PROTOCOL.md section 5.9.
    """
    if num_players <= 1:
        return "button"
    sab = (your_seat - dealer_seat) % num_players
    if num_players == 2:
        return "button_sb" if sab == 0 else "big_blind"
    if sab == 0:
        return "button"
    if sab == 1:
        return "small_blind"
    if sab == 2:
        return "big_blind"
    if sab == num_players - 1:
        return "cutoff"
    if sab == 3:
        return "early"
    return "middle"


#: Every action name this starter knows how to build a payload for. The
#: platform's action vocabulary is open-ended: a table may offer an action a
#: given SDK release predates (`draw` at a 2-7 Triple Draw table, `place` at a
#: Pineapple OFC table), and those actions carry their parameters under
#: `params`, which this starter does not populate. Treat `valid_actions` as
#: data to check, never as a fixed list to assume.
KNOWN_ACTIONS = frozenset({"fold", "check", "call", "raise", "all_in"})


class MyBot(Bot):
    """Replace `decide()` with your strategy."""

    def __init__(self) -> None:
        # Remembers which unfamiliar action names have already been reported,
        # so an unknown table logs once instead of once per turn. Left
        # unannotated on purpose: this file is cythonized for the runtime
        # image, and a PEP 526 attribute annotation is a type DECLARATION
        # there, not a hint.
        self._reported_unknown = set()

    def decide(self, state: GameState) -> Action:
        # The SDK has handed you a fully-parsed GameState. Return one
        # `Action` (Action.fold/check/call/raise_to/all_in). Must be in
        # state.valid_actions.
        #
        # Seat-count-aware: opponent_stacks is a LIST of every other seat
        # (length N-1), so this bot keeps running unchanged at a 3-6 player
        # table. your_seat + dealer_seat give your position. When you add real
        # strategy, iterate / aggregate opponent_stacks instead of assuming a
        # single opponent (reading opponent_stacks[0] sees only one neighbor).
        num_players = len(state.opponent_stacks) + 1
        _position = table_position(state.your_seat, state.dealer_seat, num_players)

        # Notice, once, any action name this starter cannot build a payload
        # for. This is a NO-LIMIT HOLD'EM bot; it is not a variant bot, and it
        # deliberately does not guess at a `draw` or `place` payload it has no
        # strategy for. Surfacing the mismatch beats silently misplaying it.
        unknown = sorted(set(state.valid_actions) - KNOWN_ACTIONS - self._reported_unknown)
        if unknown:
            self._reported_unknown.update(unknown)
            print(
                f"note: this table offers actions this bot does not implement: {unknown} "
                f"(phase={state.phase!r}) -- falling back to the safest action on offer",
                file=sys.stderr,
            )

        # Pick from what is actually offered rather than assuming check/fold
        # are always available. If NOTHING on offer is understood, fold is the
        # least-committal thing to send: the server rejects it and substitutes
        # its own default for the table, and the session survives to the next
        # turn instead of the bot crashing out of the match.
        if "check" in state.valid_actions:
            return Action.check()
        if "fold" in state.valid_actions:
            return Action.fold()
        if "call" in state.valid_actions:
            return Action.call()
        return Action.fold()


def main() -> None:
    """Entry point — invoked by the Dockerfile ENTRYPOINT.

    The Chipzen platform injects `CHIPZEN_WS_URL` and `CHIPZEN_TOKEN`
    (or `CHIPZEN_TICKET`) at container launch time. For local testing
    against your own stack, set them yourself or pass the URL as the
    first positional argument.
    """
    url = os.environ.get("CHIPZEN_WS_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not url:
        print(
            "error: CHIPZEN_WS_URL not set and no URL passed on the command line",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(
        run_bot(
            url,
            MyBot(),
            token=os.environ.get("CHIPZEN_TOKEN"),
            ticket=os.environ.get("CHIPZEN_TICKET"),
        )
    )


if __name__ == "__main__":
    main()
