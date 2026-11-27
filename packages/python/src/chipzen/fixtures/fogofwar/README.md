# Fog of War conformance fixtures (preview)

`chipzen validate` hands a Fog of War bot every request in `move.json` and
judges each answer the way the platform does (`chipzen.chess_validate`, with
`game="fogofwar"`). Fog of War is not live on Chipzen yet.

The file has the chess fixtures' shape: a `match_start` (with the
`fogofwar-1.0` rule set) and `pairs`, each a `request` (a masked `move`
request as a `turn_request`), example `answers` labelled `ok` or `rejected`
with the platform's code, and the `source` game.

The requests come from replaying the `fogofwar-*` golden matches of Chipzen's
chess engine, and seeded random games picked for a king capture, castling,
promotion and en passant. Each label is the engine's own verdict. They are
wire messages only: each seat's masked view, no position, no rules.

Regenerate with `scripts/extract_fogofwar_fixtures.py` (it needs the private
engine). Do not edit by hand.
