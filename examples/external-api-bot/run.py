#!/usr/bin/env python3
"""Runnable entry point for the External-API reference bot.

Connects to the lobby, waits to be matched, plays one match end-to-end, and
exits. See ``docs/EXTERNAL-API-BOT-PROTOCOL.md`` for the full protocol and the
example README for prerequisites (you need a ``cz_extbot_`` token issued for an
``external_api`` bot you own).

Usage (from this directory)::

    python run.py \\
        --base-url wss://staging.chipzen.ai \\
        --bot-id <bot-uuid> \\
        --token cz_extbot_...

Or via environment variables (CLI flags take precedence)::

    CHIPZEN_BASE_URL   — platform origin, e.g. wss://staging.chipzen.ai
    CHIPZEN_BOT_ID     — the External-API bot's UUID
    CHIPZEN_EXTBOT_TOKEN — the cz_extbot_ token

Add ``--loop`` to keep the lobby connection cycling for successive matches
(reconnects the lobby after each match ends).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from client import run_once


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chipzen External-API reference bot")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CHIPZEN_BASE_URL"),
        help="Platform origin (e.g. wss://staging.chipzen.ai or ws://localhost:8001)",
    )
    parser.add_argument(
        "--bot-id",
        default=os.environ.get("CHIPZEN_BOT_ID"),
        help="The External-API bot's UUID",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CHIPZEN_EXTBOT_TOKEN"),
        help="The cz_extbot_ API token",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep cycling: after a match ends, reconnect the lobby and wait for the next match",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    required = (
        ("--base-url", args.base_url),
        ("--bot-id", args.bot_id),
        ("--token", args.token),
    )
    missing = [name for name, val in required if not val]
    if missing:
        print(
            f"error: missing required argument(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "       pass them as flags or via CHIPZEN_BASE_URL / CHIPZEN_BOT_ID / "
            "CHIPZEN_EXTBOT_TOKEN",
            file=sys.stderr,
        )
        return 2

    while True:
        result = await run_once(args.base_url, args.bot_id, args.token)
        if result is not None:
            print(
                f"match ended: reason={result.get('reason')} results={result.get('results')}"
            )
        else:
            print("match ended without a clean match_end frame")
        if not args.loop:
            return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
