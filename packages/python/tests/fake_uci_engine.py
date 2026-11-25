"""A tiny fake UCI engine for the UCI bridge tests. Not a chess engine: it knows no rules.

Usage: python fake_uci_engine.py <mode> <bestmove> [<log file>]

It answers the UCI handshake and, for each ``go``, prints ``bestmove <bestmove>``,
except as the mode says:

- ``good``: answers at once;
- ``illegal``: answers ``bestmove a1a1``;
- ``none``: answers ``bestmove (none)``;
- ``slow``: answers only when told ``stop``;
- ``late``: for the first ``go`` only, answers ``stop`` after 400 ms with
  ``bestmove h7h8``; every later ``go`` is answered at once;
- ``crash``: exits on ``go``;
- ``mute``: never answers ``uci``.

Every command received is appended to the log file, if one is given.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    mode = sys.argv[1]
    bestmove = sys.argv[2]
    log = open(sys.argv[3], "a", encoding="utf-8") if len(sys.argv) > 3 else None
    pending = False
    goes = 0

    def say(text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        command = raw.strip()
        if log is not None:
            log.write(command + "\n")
            log.flush()
        if command == "uci":
            if mode == "mute":
                continue
            say("id name FakeEngine")
            say("option name Hash type spin default 16 min 1 max 1024")
            say("uciok")
        elif command == "isready":
            say("readyok")
        elif command.startswith("go"):
            goes += 1
            if mode == "crash":
                return 3
            if mode == "slow" or (mode == "late" and goes == 1):
                pending = True
                continue
            say("info depth 1 score cp 13 pv " + bestmove)
            if mode == "illegal":
                say("bestmove a1a1")
            elif mode == "none":
                say("bestmove (none)")
            else:
                say(f"bestmove {bestmove} ponder e7e5")
        elif command == "stop":
            if pending:
                pending = False
                if mode == "late":
                    time.sleep(0.4)
                    say("bestmove h7h8")
                else:
                    say(f"bestmove {bestmove}")
        elif command == "quit":
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
