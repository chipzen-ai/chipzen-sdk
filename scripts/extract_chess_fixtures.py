#!/usr/bin/env python3
"""Regenerate the chess (preview) conformance fixtures from Chipzen's chess engine.

The fixtures (``packages/python/src/chipzen/fixtures/chess/``) are what
``chipzen validate`` feeds a chess bot: real ``move`` requests, rendered as
``turn_request`` messages, each paired with example ``turn_action`` answers
labelled with what the platform does with them (``ok``, or ``rejected`` with
the machine code ``action_rejected.details.code`` carries).

The engine is private and stays out of this repo. This script replays the
engine's golden matches and a few seeded random games through it, takes the
requests it makes of a bot, and writes out **wire messages only**. No engine
code, no rules code, and no log is copied into this repo, and nothing in the
fixtures lets a bot work out the legal moves of a position that is not in them.

Run it with the private engine checkout on the path (nothing is written there):

    python -B scripts/extract_chess_fixtures.py --engine <chess-engine checkout>

The engine is the copy the platform runs (Chipzen vendors it, unchanged, from
the chess-engine repository), so point ``--engine`` at a folder that holds the
``chess_engine`` package and its ``v1/golden`` and ``v1/rulesets`` folders.

Labels come from the engine's own ``check_answer``, applied to the answer as the
platform's chess codec decodes it. The rendering and decoding below follow the
chess Layer 2 dialect (``CHESS-GAME-STATE-PROTOCOL.md`` section 5): the engine
request under ``turn_request.state``, next to ``phase`` and the LAYER2-COMMON
compatibility keys; ``seat`` in the envelope only; ``timeout_ms`` = window +
bank left; ``valid_actions: ["move"]``; an answer whose ``action`` is not
``move`` is ``not_in_valid_actions``, one whose ``params`` is not exactly
``{"move": <string>}`` is ``bad_shape``, and the move string (cut to 16
characters) goes to the engine. ``match_start.game_config`` is the dialect's
section 5.1: ``variant`` ``"chess"``, the rule set's table without its
``notes``, and ``engine_version``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from pathlib import Path
from typing import Any

SDK_SRC = Path(__file__).resolve().parent.parent / "packages" / "python" / "src"
OUT_DIR = SDK_SRC / "chipzen" / "fixtures" / "chess"

RULE_SET_ID = "chess-1.0"
SERVER_TS = "2026-11-14T12:00:00.000Z"
COMPAT_KEYS: dict[str, Any] = {
    "phase": "move",
    "board": [],
    "your_hole_cards": [],
    "pot": 0,
    "to_call": 0,
    "min_raise": 0,
    "max_raise": 0,
    "your_stack": 0,
    "opponent_stacks": [0],
}

#: Golden matches sampled for requests: the start, the middle and the end of
#: each, both seats.
GOLDENS = (
    "chess-scholars-mate",
    "chess-fools-mate",
    "chess-loyd-stalemate",
    "chess-bank-drain",
    "chess-greedy-threefold",
    "chess-random-mate-black",
    "chess-random-insufficient",
    "chess-random-ply-cap",
)

#: Seeded random games searched for the special moves (castling, promotion,
#: en passant).
RANDOM_SEEDS = range(1, 400)


#: The dialect name ``match_start.game_config.variant`` carries (dialect 5.1). Not
#: the rule set's own ``variant`` key (``"standard"``), which stays in ``rule_set``
#: and in every request ``state``.
DIALECT = "chess"

#: Longest move text the codec keeps (dialect section 8).
MAX_MOVE_TEXT = 16


def render(request: dict, n: int, match_id: str) -> dict:
    """An engine request as a ``turn_request`` (dialect section 5.2)."""
    clock = request["clock"]
    state = dict(COMPAT_KEYS)
    state.update({k: v for k, v in request.items() if k != "seat"})
    return {
        "type": "turn_request",
        "match_id": match_id,
        "seq": n + 1,
        "server_ts": SERVER_TS,
        "request_id": f"req_{n:05d}",
        "seat": request["seat"],
        "timeout_ms": clock["window_ms"] + clock["my_bank_ms"],
        "valid_actions": ["move"],
        "state": state,
    }


def decode(message: dict) -> dict:
    """The answer the engine is handed, as the platform's codec decodes it (module doc)."""
    action = message.get("action")
    if type(action) is not str or action != "move":
        return {"bad": "not_in_valid_actions"}
    params = message.get("params")
    if not isinstance(params, dict) or set(params) != {"move"}:
        return {"bad": "bad_shape"}
    move = params["move"]
    if type(move) is not str:
        return {"bad": "bad_shape"}
    return {"move": move[:MAX_MOVE_TEXT]}


def answers(request: dict) -> list[dict]:
    """Example answers for one request: one legal, several not."""
    legal = request["legal_moves"]
    ours = [{"action": "move", "params": {"move": legal[len(legal) // 2]}}]
    # A UCI string that is not legal here: a legal move's reverse, which is
    # never legal for the side to move unless listed (then skipped).
    for move in legal:
        reverse = move[2:4] + move[0:2]
        if reverse not in legal:
            ours.append({"action": "move", "params": {"move": reverse}})
            break
    ours += [
        {"action": "move", "params": {"move": legal[0].upper()}},
        {"action": "move", "params": {"move": legal[0][:2] + "-" + legal[0][2:]}},
        {"action": "move", "params": {"move": 42}},
        {"action": "move", "params": {}},
        {"action": "check", "params": {}},
    ]
    return ours


def label(match: Any, seat: int, message: dict) -> dict:
    rejection = match.check_answer(seat, decode(message))
    if rejection is None:
        return {"expect": "ok"}
    return {"expect": "rejected", "code": rejection.code}


def pair(match: Any, seat: int, request: dict, n: int, match_id: str, source: str) -> dict:
    wire = render(request, n, match_id)
    return {
        "source": source,
        "request": wire,
        "answers": [
            {
                "answer": json.dumps(
                    {
                        "type": "turn_action",
                        "match_id": match_id,
                        "request_id": wire["request_id"],
                        **message,
                    }
                ),
                **label(match, seat, message),
            }
            for message in answers(request)
        ],
    }


def special(request: dict) -> str | None:
    """Which special move this request offers, judged from the strings only."""
    legal = request["legal_moves"]
    fen_fields = request["fen"].split()
    if any(len(m) == 5 for m in legal):
        return "promotion"
    if fen_fields[3] != "-" and any(m[2:4] == fen_fields[3] for m in legal):
        return "en_passant"
    if any(m in ("e1g1", "e1c1", "e8g8", "e8c8") for m in legal) and fen_fields[2] != "-":
        return "castling"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", required=True, type=Path, help="chess-engine checkout")
    args = parser.parse_args()
    sys.path.insert(0, str(args.engine.resolve()))
    import chess_engine as ce  # noqa: PLC0415 - private, only on the path at run time

    golden_dir = args.engine / "chess_engine" / "v1" / "golden"
    pairs: list[dict] = []
    n = 0

    for name in GOLDENS:
        log = json.loads(gzip.decompress((golden_dir / f"{name}.json.gz").read_bytes()))
        match = ce.new_match(log["rule_set_id"], seed=log["seed"])
        steps = log["steps"]
        keep = {0, 1, len(steps) // 2, len(steps) - 2, len(steps) - 1}
        for index, step in enumerate(steps):
            seat, request = next(iter(match.requests().items()))
            if index in keep and request["legal_moves"]:
                pairs.append(pair(match, seat, request, n, f"fixture-{name}", name))
                n += 1
            match.step({seat: step["delivery"]})

    found: set[str] = set()
    for seed in RANDOM_SEEDS:
        if found >= {"promotion", "en_passant", "castling"}:
            break
        rng = random.Random(seed)
        match = ce.new_match(RULE_SET_ID, seed=seed)
        while not match.done:
            seat, request = next(iter(match.requests().items()))
            kind = special(request)
            if kind is not None and kind not in found:
                found.add(kind)
                source = f"random-{seed}-{kind}"
                pairs.append(pair(match, seat, request, n, f"fixture-{source}", source))
                n += 1
            move = rng.choice(request["legal_moves"])
            match.step({seat: {"status": "ok", "answer": {"move": move}, "latency_ms": 5}})

    rule_set = json.loads(
        (args.engine / "chess_engine" / "v1" / "rulesets" / f"{RULE_SET_ID}.json").read_text(
            "utf-8"
        )
    )
    rule_set.pop("notes", None)
    out = {
        "about": (
            "Chess (preview) conformance fixtures for chipzen validate. Wire messages only, "
            "generated by scripts/extract_chess_fixtures.py; do not edit by hand."
        ),
        "rule_set_id": RULE_SET_ID,
        "match_start": {
            "type": "match_start",
            "match_id": "fixture-chess",
            "game_config": {
                "variant": DIALECT,
                "rule_set_id": RULE_SET_ID,
                "num_players": 2,
                "rule_set": rule_set,
                "engine_version": ce.ENGINE_VERSION,
            },
        },
        "pairs": pairs,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # One pair per line, so a regeneration diffs by request.
    head = {k: v for k, v in out.items() if k != "pairs"}
    lines = [json.dumps(head, sort_keys=True)[:-1] + ', "pairs": [']
    lines += [
        "  " + json.dumps(p, sort_keys=True) + ("," if i < len(pairs) - 1 else "")
        for i, p in enumerate(pairs)
    ]
    text = "\n".join(lines) + "\n]}\n"
    json.loads(text)
    (OUT_DIR / "move.json").write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(pairs)} requests ({sorted(found)}) to {OUT_DIR / 'move.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
