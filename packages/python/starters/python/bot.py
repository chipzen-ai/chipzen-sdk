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


class MyBot(Bot):
    """Replace `decide()` with your strategy."""

    def decide(self, state: GameState) -> Action:
        # The SDK has handed you a fully-parsed GameState. Return one
        # `Action` (Action.fold/check/call/raise_to/all_in). Must be in
        # state.valid_actions.
        if "check" in state.valid_actions:
            return Action.check()
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
