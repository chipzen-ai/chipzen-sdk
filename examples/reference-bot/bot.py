"""Reference "always-check-or-fold" bot.

The simplest bot that speaks the Chipzen two-layer protocol: subclass
``chipzen.Bot`` and return ``check`` when it is legal, otherwise ``fold``.
No cards are inspected, no stakes are considered, no state is tracked.

This exists as:
  * a starter example for new SDK developers,
  * a load-test sanity target whose decision cost is effectively zero,
    and
  * a post-deploy smoke target that validates the upload/play pipeline
    without being bottlenecked on a bot's search.

Environment:
    CHIPZEN_WS_URL   WebSocket URL injected by the platform at launch.
    CHIPZEN_TOKEN    Bot API token (empty string is fine for localhost dev).
    CHIPZEN_TICKET   Alternative single-use ticket; unused here but forwarded
                     so the image works in either flow.

Logging:
    INFO is machine-readable and deliberately terse. We log one line per
    ``decide()`` entry with the game phase and legal actions, and one
    ``action=`` line with the chosen action. No PII, no hole cards echoed
    back beyond their position counts, no tokens.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from chipzen import Action, Bot, GameState
from chipzen.client import run_bot

logger = logging.getLogger("reference-bot")


class ReferenceBot(Bot):
    """Check when legal, else fold. Nothing else."""

    def decide(self, state: GameState) -> Action:
        valid = list(state.valid_actions or ())
        logger.info(
            "decide hand=%s phase=%s legal=%s",
            state.hand_number,
            state.phase,
            ",".join(valid) or "-",
        )

        if "check" in valid:
            action = Action.check()
        else:
            action = Action.fold()

        logger.info("action=%s", action.action)
        return action


async def _amain() -> None:
    log_level = os.environ.get("REFERENCE_BOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    ws_url = os.environ.get("CHIPZEN_WS_URL") or os.environ.get("CHIPZEN_URL", "")
    if not ws_url:
        logger.error("CHIPZEN_WS_URL is required")
        sys.exit(2)

    token = os.environ.get("CHIPZEN_TOKEN")
    ticket = os.environ.get("CHIPZEN_TICKET")

    logger.info("reference-bot ready; connecting to %s", ws_url)
    await run_bot(
        ws_url,
        ReferenceBot(),
        token=token if token is not None else None,
        ticket=ticket if ticket else None,
        client_name="reference-bot",
        client_version="0.1.0",
    )


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")


if __name__ == "__main__":
    main()
