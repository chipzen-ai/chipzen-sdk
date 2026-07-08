# Packaging your bot with a coding agent

You already have a poker bot and not much time. Instead of walking the
[`QUICKSTART.md`](QUICKSTART.md) by hand, hand one of the prompts below to a
coding agent (Claude Code, Codex, Cursor, Aider, …) and let it do the
packaging work: take your existing strategy, drop it into the right Chipzen
starter, validate it locally, and produce the artifact you upload (a Docker
tarball) or run (an external-API bot).

There are **two flows**. Pick the one that matches how you want your bot to
run:

| Flow | You want… | Prompt |
|---|---|---|
| **A — Hosted / upload** | Chipzen to run your bot for you. You upload a Docker image; the platform executes it in its sandbox. | [Flow A](#flow-a--hosted--upload-path) |
| **B — Remote-play API** | To run the bot yourself (laptop / server) and let Chipzen route matches to it over a long-lived connection. No Docker, no upload. | [Flow B](#flow-b--remote-play-api-path) |

Both flows exist for Python, and Flow A exists for Python / TypeScript
(JavaScript) / Rust. See the [per-language notes](#per-language-notes) and
[what to know before Flow B in another language](#flow-b-in-another-language).

> **These prompts tell the agent to read the canonical docs and follow them,
> not to trust hardcoded steps that could drift.** The docs in this repo
> (`QUICKSTART.md`, `DEV-MANUAL.md`, the starter `README.md`s,
> `IP-PROTECTION.md`, and the external-api docs) are the source of truth. The
> guardrails baked into each prompt — size budget, timeout, cold-start, etc. —
> are there so the agent's output is accepted at upload time on the first try.

---

## How to use these prompts

1. **Check out this repo** so the agent can read the starters and docs:
   `git clone https://github.com/chipzen-ai/chipzen-sdk.git`.
2. **Point the agent at both your bot and this repo.** Open your coding agent
   in a workspace that can see your existing bot repo *and* a local clone of
   `chipzen-sdk` (or let it clone the SDK itself).
3. **Copy a prompt below**, fill in the `<PLACEHOLDERS>`, and paste it in.
4. **Let the agent run the real checks.** Every prompt ends by having the
   agent run `chipzen-sdk validate` — that is the go/no-go gate, and it runs
   the same checks the upload pipeline runs. Don't skip it.

Prerequisites the human needs regardless of agent (from
[`QUICKSTART.md` §1](QUICKSTART.md#1-prerequisites)):

- **Docker** installed and running (Flow A only).
- **Python 3.10+** on PATH (the `chipzen-sdk` CLI is `pip install chipzen-bot`).
- **~5 GB free disk** for image layers (Flow A).
- A **Chipzen account** — for Flow B you also need an `external_api` bot and a
  `cz_extbot_` token (the prompt walks the agent through issuing one).

---

## The real constraints (baked into every Flow A prompt)

These come straight from [`DEV-MANUAL.md` §6–§7](DEV-MANUAL.md#6-performance),
[`QUICKSTART.md`](QUICKSTART.md), and the starter `README.md`s. They are the
difference between "validated locally and accepted" and "rejected at upload"
or "silently folds every hand in production". The prompts carry them as
acceptance criteria; they are collected here so you can sanity-check the
agent's work.

- **Image size:** hard cap **250 MB compressed** (`docker save | gzip`); the
  built image is separately capped at **200 MB**
  ([`DEV-MANUAL.md` §7.2](DEV-MANUAL.md#72-resource-limits)). Both caps are
  **platform-wide — there are no per-tier upload limits.** The Python/Rust
  starters land ~25–30 MB, the JS starter ~50 MB, with no extra deps.
- **Seccomp-safe dependencies:** the sandbox allowlists a minimal syscall set.
  **Prefer pure-Python (or pure-language) deps.** A native/C extension that
  needs an unlisted syscall can crash the container **silently on startup**,
  before your code runs (`docker logs` shows nothing) — see
  [`DEV-MANUAL.md` §7.4](DEV-MANUAL.md#74-seccomp) and
  [§9.5](DEV-MANUAL.md#95-container-dies-immediately-with-no-logs).
- **`decide()` timeout is end-to-end.** Default **10000 ms** for human-vs-bot
  (`/play`); **bot-vs-bot ranked and tournament play is tighter — 2000 ms**
  ([`DEV-MANUAL.md` §6.2](DEV-MANUAL.md#62-decision-timeout-by-match-type)).
  The budget covers the network hop, SDK queue drain, and your `decide()` body.
  Blow it and the server safe-defaults to fold — the bot looks like it's
  playing badly when it's actually timing out.
- **Cold start < 15 s.** The server waits ~15 s for your container to send its
  `hello`; a big model loaded at import time trips
  `bot_container_failed_to_attach`. **Lazy-load heavy deps inside
  `on_match_start`, not at module import.** ([`QUICKSTART.md` common
  mistakes](QUICKSTART.md#common-first-time-mistakes),
  [`DEV-MANUAL.md` §7.1](DEV-MANUAL.md#71-required-contract).)
- **Container contract** ([`DEV-MANUAL.md` §7.1](DEV-MANUAL.md#71-required-contract)):
  `ENTRYPOINT` runs the bot with unbuffered stdout (`python -u`); reads
  `CHIPZEN_WS_URL` + `CHIPZEN_TOKEN` (or `CHIPZEN_TICKET`) from env; treats
  `/tmp` as the **only** writable path (read-only root FS); runs non-root if it
  can. **The starters' Dockerfiles already satisfy this — keep them.**
- **IP protection (Python):** the Python starter's multi-stage Dockerfile
  compiles `bot.py` to a Cython `.so` so the runtime image ships no readable
  `.py` strategy source. TS and Rust starters do the equivalent (compiled
  binary). See [`packages/python/IP-PROTECTION.md`](../packages/python/IP-PROTECTION.md)
  for exactly what that protects (and what it doesn't — it's "raises the cost",
  not "impossible").

---

## Flow A — Hosted / upload path

**Goal:** take your existing bot and produce a Chipzen-compatible, locally
validated Docker image (`.tar.gz`) ready to upload, using the IP-protected
language starter under `packages/<lang>/starters/`.

Copy the prompt, fill the four placeholders, paste into your coding agent.

````text
You are helping me package an existing poker bot for the Chipzen SDK's
hosted/upload path. Do the work end to end; only stop to ask me a question if
a decision genuinely can't be made from the code or the docs.

INPUTS
- My existing bot lives at: <PATH TO YOUR EXISTING BOT>
- Its decision logic (the function/method that picks an action given a game
  state) is: <FILE / FUNCTION NAME, or "find it">
- Target language for the Chipzen bot: <python | typescript | rust>
- I will upload under this Chipzen tier: <free | pro | elite>

SOURCE OF TRUTH — read these first, and follow them over anything you
remember. Do not invent commands, paths, or flags.
- docs/QUICKSTART.md                        (the build→validate→export loop)
- docs/DEV-MANUAL.md  sections 2, 6, 7, 9   (SDK surface, perf, containers, troubleshooting)
- packages/<lang>/starters/<lang>/README.md (the exact starter you'll copy)
- packages/python/IP-PROTECTION.md          (what the compiled build protects)
If a doc and your memory disagree, the doc wins. If a command in a doc fails,
show me the error rather than substituting a different command.

STEPS
1. Copy the starter directory packages/<lang>/starters/<lang>/ to a NEW
   directory OUTSIDE this SDK repo (so my bot is a standalone project). This is
   the IP-protected starter — keep its Dockerfile and .dockerignore as-is; they
   already satisfy the container contract.
2. Port my strategy into the starter's decide() (python: bot.py `decide`;
   typescript: bot.js `decide`; rust: src/lib.rs `impl Bot for MyBot`). Keep
   the starter's entry point / main() and the SDK plumbing intact — I only want
   to replace the decision logic. Map my action outputs to the SDK Action API
   (fold / check / call / raise_to(<total bet>) / all_in). Raise amounts are
   TOTAL bet sizes, not increments, and must be in [min_raise, max_raise].
3. Add any dependencies my strategy needs to the starter's manifest
   (python: requirements.txt; typescript: package.json; rust: Cargo.toml).
   PREFER PURE-<LANGUAGE> DEPS. If a dependency pulls a native/C extension,
   flag it to me explicitly: the platform's seccomp profile can make such
   extensions crash the container silently on startup. Do not add a native dep
   without telling me.
4. Respect these hard constraints — treat them as acceptance criteria:
   - decide() must return within the decision timeout: 10000 ms human-vs-bot,
     but only 2000 ms for ranked bot-vs-bot / tournaments. Keep decide() fast
     and synchronous; cache expensive tables at on_match_start, not per hand.
   - Cold start < 15 s. If my bot loads a model or a big table, LAZY-LOAD it
     inside on_match_start (or first use), NOT at module import — otherwise the
     container misses the ~15 s attach budget.
   - Final compressed image: hard cap 250 MB compressed (built image capped at
     200 MB) — these are platform-wide, with NO per-tier upload limit (see
     DEV-MANUAL.md §7.2). If we're over, tell me and try the size levers the
     docs list (alpine base for pure-Python, strip pycache/tests, multi-stage).
5. Run the conformance test if the starter ships one (rust: `cargo test` for
   tests/conformance.rs).
6. Validate BEFORE building, exactly as the docs say:
       chipzen-sdk validate <my new bot dir>
   Then the stricter protocol check:
       chipzen-sdk validate <my new bot dir> --check-connectivity
   Both must pass (exit 0). Fix whatever they flag and re-run until green. Do
   not proceed to the build while validate is red.
7. Build and export the upload artifact:
       docker build -t my-bot:v1 <my new bot dir>
       docker save my-bot:v1 | gzip > my-bot.tar.gz
   Report the compressed size (`ls -lh my-bot.tar.gz`) and confirm it's within
   budget. (On Windows, run the docker save | gzip pipe in Git Bash or WSL —
   PowerShell corrupts the archive.)
8. Verify the IP protection worked using the check in the starter README
   (list /bot inside the image; the readable strategy source must NOT be
   present — the compiled .so / binary should be).

DELIVERABLES
- The new standalone bot directory (starter + my ported strategy).
- A green `chipzen-sdk validate --check-connectivity` run.
- my-bot.tar.gz, with its compressed size reported.
- A short note listing: any deps you added and whether any are native
  (seccomp risk), the measured image size vs. the budget, and anything I
  should double-check before uploading through the Chipzen developer UI.
````

### Per-language notes

The prompt above is language-parametric; here's what changes per language so
you can spot-check the agent.

- **Python** — starter `packages/python/starters/python/`. Strategy goes in
  `bot.py`'s `decide(self, state: GameState) -> Action`; keep `main()` and the
  `from bot import main; main()` entry the Cython Dockerfile expects. Deps in
  `requirements.txt`. The multi-stage Dockerfile compiles `bot.py` → `.so`
  (Cython) so no readable source ships. This is the reference implementation
  and the most-tested path.
- **TypeScript / JavaScript** — starter
  `packages/javascript/starters/javascript/`. Strategy in `bot.js`'s
  `decide(state)`; SDK is `@chipzen-ai/bot`. Deps in `package.json` (Node 20+).
  The Dockerfile uses `bun build --compile` to ship a single binary, no
  readable `.js`.
- **Rust** — starter `packages/rust/starters/rust/`. Strategy in `src/lib.rs`
  (`impl Bot for MyBot`); `src/main.rs` is a thin env-reading shim. Deps in
  `Cargo.toml`. Run `cargo test` (the shipped `tests/conformance.rs` drives
  `MyBot` through a canned full match) before validating. The Dockerfile does a
  multi-stage `cargo build --release` (stripped, LTO) shipping one binary.

---

## Flow B — Remote-play API path

**Goal:** adapt your existing bot to connect to Chipzen over the external API
with a `cz_extbot_` token, so you run it yourself and the platform routes
matches to it. No Docker, no image, no upload.

The canonical walkthroughs are
[`docs/external-api/FIRST-30-MINUTES.md`](external-api/FIRST-30-MINUTES.md)
(step-by-step) and
[`docs/EXTERNAL-API-BOT-PROTOCOL.md`](EXTERNAL-API-BOT-PROTOCOL.md) (the wire
protocol). **The Python SDK's `run_external_bot()` / `chipzen run-external`
ships the whole flow — and the exact same `chipzen.Bot` subclass works on both
the upload path and the remote path** ([`DEV-MANUAL.md`
§2.7](DEV-MANUAL.md#27-external-api-remote-play-run_external_bot)).

Copy the prompt, fill the placeholders, paste into your coding agent.

````text
You are helping me connect an existing poker bot to Chipzen's external-API
(remote-play) path, where I run the bot on my own machine and Chipzen routes
matches to it over a long-lived lobby connection. Do the work end to end.

INPUTS
- My existing bot lives at: <PATH TO YOUR EXISTING BOT>
- Its decision logic is: <FILE / FUNCTION NAME, or "find it">
- Language: <python | other — see note below>
- Environment: <staging | prod>   (start with staging)

SOURCE OF TRUTH — read these first and follow them; do not invent commands.
- docs/external-api/FIRST-30-MINUTES.md   (the 30-minute walkthrough)
- docs/EXTERNAL-API-BOT-PROTOCOL.md       (lobby, match data plane, errors, reconnect)
- docs/DEV-MANUAL.md §2.7                  (run_external_bot / chipzen run-external)
If a doc and your memory disagree, the doc wins.

STEPS
1. PYTHON FAST PATH (do this if language is python):
   - `pip install chipzen-bot` (0.3.0+).
   - Write a single chipzen.Bot subclass whose decide(state) contains my
     strategy, mapping to the SDK Action API (fold / check / call /
     raise_to(<total bet>) / all_in). This is the SAME Bot contract as the
     upload path — if I ever want to package it as a container later, the same
     class works.
   - Wire it up with run_external_bot(...) OR the `chipzen run-external
     my_bot.py --env <env>` CLI, exactly as DEV-MANUAL §2.7 and
     FIRST-30-MINUTES show. Put the token + bot_id in a chipzen.toml
     ([external_api] token = "cz_extbot_...", bot_id = "...").
2. Tell me to create the credentials in the Chipzen dashboard (I do this part;
   you can't): on the bot dashboard pick "Create bot" -> "External API", copy
   the bot_id from the detail page, then issue a token in the "API tokens"
   section. The plaintext token (format cz_extbot_<suffix>) is shown ONCE —
   I copy it immediately. Do NOT hardcode the token in source; read it from
   chipzen.toml or an env var (CHIPZEN_EXTBOT_TOKEN).
3. Respect the protocol rules the docs call out:
   - The returned action must be one of the turn's valid_actions; the SDK
     retries an illegal action with a legal fallback.
   - The bot must be ONLINE in the lobby before a match is dispatched, or the
     dispatch fails. Use the loop/keep-alive option so it stays up across a
     tournament's successive matches.
   - Plain ws:// is only allowed on localhost; staging/prod are wss://.
4. Give me the exact command to run it against <env>, and tell me how to fire a
   first match: challenge a house bot from the /challenges page as an UNRANKED
   EXHIBITION (external-API vs a sandboxed house bot is a cross-division pair,
   so ranked mode is rejected).

DELIVERABLES
- The bot file(s) + a chipzen.toml template (token/bot_id left as placeholders
  for me to fill — never commit real tokens).
- The exact run command, and the expected first log lines from FIRST-30-MINUTES
  so I know it connected to the lobby.
- A short note on what to check if `lobby: connected` doesn't appear (bot_id
  mismatch, revoked token, Cloudflare 1010 — per FIRST-30-MINUTES).
````

### Flow B in another language

The **packaged one-liner (`run_external_bot` / `chipzen run-external`) is a
Python SDK feature.** If your bot is in TypeScript or Rust and you want the
remote-play path:

- **First check** whether the JS (`@chipzen-ai/bot`) or Rust (`chipzen-bot`)
  SDK exposes an equivalent external-API runner before assuming it does — the
  packaged helper is documented for Python.
- **Otherwise, speak the external-API protocol directly** per
  [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](EXTERNAL-API-BOT-PROTOCOL.md) (lobby
  connect → `matched` → match gateway → the two-layer game loop). The in-repo
  reference client at [`examples/external-api-bot/`](../examples/external-api-bot/)
  speaks raw JSON over WebSockets and is the thing to read/port when learning
  the wire format — its `strategy.py` is a single `decide(state, valid_actions)`
  function, and `client.py` does all the plumbing.

Point your coding agent at `EXTERNAL-API-BOT-PROTOCOL.md` and the reference
client and ask it to port the transport into your language, keeping your
existing decision logic.

---

## After the agent is done

- **Flow A:** upload `my-bot.tar.gz` through the Chipzen developer UI and watch
  it move `pending_review → reviewing → active` (activation is automatic on a
  passing review)
  ([`DEV-MANUAL.md` §8.3](DEV-MANUAL.md#83-lifecycle-in-one-glance)). If it's
  rejected, the reason is in the bot card — cross-reference
  [`DEV-MANUAL.md` §9.1](DEV-MANUAL.md#91-my-bot-got-rejected-during-review).
- **Flow B:** run the bot, challenge a house bot, and watch the match in the
  dashboard while your terminal logs the match.
- **Either way**, `chipzen-sdk validate` and the local checks only prove your
  bot conforms to the protocol and won't be rejected on packaging grounds. They
  are **not** a strength check — the platform runs full bot-vs-bot evaluation
  after upload. Making the bot *good* is on you.

## Where to file issues

- **SDK / starter / validate / protocol bugs, or a prompt that produced
  something the pipeline rejected** — open an issue on
  [chipzen-ai/chipzen-sdk](https://github.com/chipzen-ai/chipzen-sdk/issues).
- **Platform / account / matchmaking / billing** — `support@chipzen.ai` or
  Discord, not this repo.
