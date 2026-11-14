# Chess conformance fixtures (preview)

`chipzen validate` hands a chess bot every request in `move.json` and judges
each answer the way the platform does (`chipzen.chess_validate`). Chess is not
live on Chipzen yet.

`move.json` holds a `match_start` (with the `chess-1.0` rule set) and `pairs`.
Each pair is:

| Key | What it is |
|---|---|
| `request` | A `move` request as a `turn_request`. |
| `answers` | Example `turn_action` messages for it, as JSON text, each with `expect`: `ok`, or `rejected` with the `code` the platform sends in `action_rejected.details.code`. |
| `source` | The recorded game the request comes from. |

The requests come from replaying games of Chipzen's chess engine (its golden
matches, and seeded random games picked for castling, promotion and en
passant), and each label is the engine's own verdict. They are wire messages
only: no rules, and nothing that gives the legal moves of any other position.
The chess wire codec is not built yet, so the `turn_request` rendering follows
the game-interface specification; it will be regenerated if the published
chess dialect differs.

Regenerate with `scripts/extract_chess_fixtures.py` (it needs the private
engine checkout). Do not edit by hand.
