#!/usr/bin/env python3
"""Regenerate the Fog of War (preview) conformance fixtures from Chipzen's chess engine.

The Fog of War sibling of ``extract_chess_fixtures.py``: same engine, same
codec, same output shape (``packages/python/src/chipzen/fixtures/fogofwar/``).
It replays the engine's ``fogofwar-*`` golden matches and a few seeded random
games, takes the masked requests the engine makes of each seat, and writes out
**wire messages only**: no engine code, no rules code, no log, and nothing that
lets a bot work out the legal moves of a position that is not in the file.

Run it with the private engine on the path (nothing is written there):

    python -B scripts/extract_fogofwar_fixtures.py --engine <folder holding chess_engine>

The rendering follows ``FOGOFWAR-GAME-STATE-PROTOCOL.md`` section 4 (the
engine's fog request under ``turn_request.state``, next to ``phase`` and the
LAYER2-COMMON compatibility keys; ``seat`` in the envelope only; ``timeout_ms``
= window + bank left; ``valid_actions: ["move"]``), and decoding is chess's
(the Fog of War codec is chess's codec, section 5). ``match_start.game_config``
is section 3: ``variant`` ``"fogofwar"``, the rule set's table without its
``notes``, and ``engine_version``. Labels are the engine's own ``check_answer``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_chess_fixtures import SDK_SRC, pair  # noqa: E402 - sibling script

OUT_DIR = SDK_SRC / "chipzen" / "fixtures" / "fogofwar"

RULE_SET_ID = "fogofwar-1.0"

#: ``match_start.game_config.variant`` at a Fog of War table: the dialect's name.
#: The rule set's own ``variant`` key (``"fog"``) stays in ``rule_set`` and in
#: every request ``state``.
DIALECT = "fogofwar"

#: Golden matches sampled for requests: the start, the middle and the end of
#: each, both seats.
GOLDENS = (
    "fogofwar-quick-capture",
    "fogofwar-greedy-capture",
    "fogofwar-castle-through-attack",
    "fogofwar-walk-into-capture",
    "fogofwar-random-capture-white",
    "fogofwar-random-capture-black",
    "fogofwar-random-threefold",
    "fogofwar-random-ply-cap",
)

#: Seeded random games searched for the special moves.
RANDOM_SEEDS = range(1, 400)

KINDS = {"king_capture", "promotion", "castling", "en_passant"}


def special(request: dict) -> str | None:
    """Which special move this request offers, judged from the strings and the view only."""
    legal = request["legal_moves"]
    pieces = request["pieces"]
    white = request["side"] == "white"
    their_king = "k" if white else "K"
    pawn = "P" if white else "p"
    if any(pieces.get(m[2:4]) == their_king for m in legal):
        return "king_capture"
    if any(len(m) == 5 for m in legal):
        return "promotion"
    king = "K" if white else "k"
    castles = ("e1g1", "e1c1", "e8g8", "e8c8")
    if request["castling"] != "-" and any(
        m in castles and pieces.get(m[:2]) == king for m in legal
    ):
        return "castling"
    for m in legal:
        # A pawn's diagonal step onto an empty visible square is en passant.
        if pieces.get(m[:2]) == pawn and m[0] != m[2] and m[2:4] not in pieces:
            return "en_passant"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", required=True, type=Path, help="folder holding chess_engine")
    args = parser.parse_args()
    sys.path.insert(0, str(args.engine.resolve()))
    import chess_engine as ce  # noqa: PLC0415 - private, only on the path at run time

    golden_dir = args.engine / "chess_engine" / "v1" / "golden"
    pairs: list[dict[str, Any]] = []
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
        if found >= KINDS:
            break
        rng = random.Random(seed)
        match = ce.new_match(RULE_SET_ID, seed=seed)
        while not match.done:
            seat, request = next(iter(match.requests().items()))
            if not request["legal_moves"]:
                break
            kind = special(request)
            if kind is not None and kind not in found:
                found.add(kind)
                source = f"random-{seed}-{kind}"
                pairs.append(pair(match, seat, request, n, f"fixture-{source}", source))
                n += 1
            # Avoid ending the game at once, so the special moves get a chance.
            quiet = [
                m for m in request["legal_moves"] if request["pieces"].get(m[2:4]) not in ("k", "K")
            ]
            move = rng.choice(quiet or request["legal_moves"])
            match.step({seat: {"status": "ok", "answer": {"move": move}, "latency_ms": 5}})

    rule_set = json.loads(
        (args.engine / "chess_engine" / "v1" / "rulesets" / f"{RULE_SET_ID}.json").read_text(
            "utf-8"
        )
    )
    rule_set.pop("notes", None)
    out = {
        "about": (
            "Fog of War (preview) conformance fixtures for chipzen validate. Wire messages only, "
            "generated by scripts/extract_fogofwar_fixtures.py; do not edit by hand."
        ),
        "rule_set_id": RULE_SET_ID,
        "match_start": {
            "type": "match_start",
            "match_id": "fixture-fogofwar",
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
