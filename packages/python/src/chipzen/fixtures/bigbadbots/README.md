# Big Bad Bots conformance fixtures

`chipzen validate` hands a Big Bad Bots bot every request in these files and
judges each answer the way the server does (`chipzen.bigbadbots_validate`).

One file per call: `choose_body.json`, `bid.json`, `fit.json`, `act.json`. Each
holds `pairs`, and each pair is:

| Key | What it is |
|---|---|
| `request` | A `turn_request` exactly as the server sends it. |
| `answer` | A `turn_action` for that request, as the Python client sends it: JSON text, which may not be valid JSON (`NaN`). |
| `expect` | What the server does with the answer: `ok` (applied), `corrected` (well-shaped but not legal: the nearest legal answer is applied and a violation logged) or `malformed` (rejected; the call's default applies). See protocol section 3.7. |
| `source` | The recorded match the pair comes from. |

They are wire messages only. The requests come from replaying recorded matches
of the reference engine (rule set `fighter-1.0`, engine `1.0.0`, which predates
`walls`; the arena there has no closing walls, so `walls` is always
`[0.0, 12.0]`). The answers are the ones the recorded bots gave, including a bot
that answers with every bad shape, and each `expect` label is the reference
engine's own verdict. The SDK's checker is tested against every label.

Regenerate with `scripts/extract_bigbadbots_fixtures.py` (it needs the private
engine checkout). Do not edit these files by hand.
