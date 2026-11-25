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
They are rendered and decoded the way the platform's chess codec does it (the
chess Layer 2 dialect, section 5), and `match_start.game_config` carries
`variant: "chess"` and `engine_version`, as the platform sends them.

Regenerate with `scripts/extract_chess_fixtures.py` (it needs the private
engine). Do not edit by hand.
