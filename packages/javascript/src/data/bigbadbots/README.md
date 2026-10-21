# Big Bad Bots data bundled with `@chipzen-ai/bot`

Byte-for-byte copies of the Python SDK's files, so both SDKs play by the same
published rule sets and are checked against the same recorded frames:

| Here | Source of truth |
|---|---|
| `rulesets/*.json` | `packages/python/src/chipzen/rulesets/bigbadbots/` (read by `bigbadbots_rules.ts`) |
| `fixtures/*.json` | `packages/python/src/chipzen/fixtures/bigbadbots/` (read by `bigbadbots_validate.ts`) |

Do not edit these by hand. `scripts/extract_bigbadbots_fixtures.py` writes both
copies of the fixtures; a new rule-set file is added to both directories. The
tests (`tests/bigbadbots-rules.test.ts`, `tests/bigbadbots-validate.test.ts`)
fail if the copies drift.
