# Published Big Bad Bots rule sets

One file per released rule set, read by `chipzen.bigbadbots_rules`
(`published_rule_set()`), so a bot can look up a known version's numbers
offline. See [`docs/RULESETS.md`](../../../../../../docs/RULESETS.md) for the
id scheme, the lifecycle and the changelog.

| Key | What it is |
|---|---|
| `rule_set_id` | The id, e.g. `bigbadbots-1.2`. |
| `state` | The lifecycle state when this SDK release was cut (RULESETS.md section 3). |
| `engine` | The engine release the rule set was frozen with. |
| `frozen_on` | The date it was frozen (UTC). |
| `sha256` | The sha256 of the rule set's canonical content, as published in RULESETS.md section 7. |
| `rule_set` | The **published** sections of its parameter table. |

**Only published numbers are here.** `rule_set` holds the sections whose every
value is already public, in RULESETS.md section 7 and the protocol document's
section 4.1 (`gearup`, `arena`, `rounds`, `forfeit`, `deadline`). The other
sections (bodies, parts, moves, specials, and so on) are added when their
tables are published in RULESETS.md (chipzen-ai/chipzen-sdk#138).

**The server's numbers always win.** Every match sends its rule set's full
parameter table in `match_start.game_config.rule_set`, and that is what a bot
plays by (`state.rules`). These files are a reference: the SDK never replaces
the server's values with them. An unknown `rule_set_id`, or a value here that
differs from the server's, logs a warning and the bot plays on.

A released rule set never changes, so these files only ever gain entries.
Do not edit an existing one.
