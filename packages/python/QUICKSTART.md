# chipzen-bot quickstart

The Python-specific path. For the broader story (Docker, upload, debugging on the platform) see the [top-level QUICKSTART](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/QUICKSTART.md).

## 1. Install

```bash
pip install chipzen-bot
```

Python 3.10+. One runtime dependency (`websockets`).

## 2. Scaffold a bot project

```bash
chipzen-sdk init my_bot
cd my_bot
```

You'll get a small project with `main.py`, `requirements.txt`, and a Dockerfile.

## 3. Implement your strategy

Open `main.py`. The default scaffold gives you a `MyBot(Bot)` subclass
with a no-op `decide()`. Replace it with your strategy:

```python
from chipzen import Bot, Action, GameState

class MyBot(Bot):
    def decide(self, state: GameState) -> Action:
        # Your strategy. The SDK has already handed you a fully-parsed
        # GameState (hole cards, board, pot, valid_actions, etc.) and
        # is waiting on a single Action in return.
        if "check" in state.valid_actions:
            return Action.check()
        return Action.fold()
```

The full `GameState` and `Action` surfaces are documented in the
[DEV-MANUAL §2.3 / §2.4](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#23-gamestate).

## 4. Validate before packaging

```bash
chipzen-sdk validate .
```

Runs the same checks the upload pipeline runs: artifact size, syntax,
imports against the platform's sandbox-blocked module list, presence
of a `Bot` subclass with a `decide()` method, a `decide()` timeout
sniff, and a smoke instantiation. Exits non-zero on any failure.

This is the **supported go/no-go** before you build a container image.
A clean validate means "the upload pipeline will accept this on
protocol grounds" — not "your bot is good".

## 5. Package and upload

The Docker build + upload steps are the same across all SDK languages.
See the [top-level QUICKSTART](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/QUICKSTART.md)
from step 5 onwards.

## Where to go next

- **Lifecycle hooks** (`on_match_start`, `on_round_start`,
  `on_phase_change`, `on_turn_result`, `on_round_result`,
  `on_match_end`) for per-match / per-hand state tracking — see
  [DEV-MANUAL §2.2](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#22-bot-lifecycle-hooks).
- **Performance budgets** (per-tier decision timeouts, queue drain
  pitfalls) — see
  [DEV-MANUAL §6](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#6-performance).
- **Bot runtime security model** (sandbox, network egress, resource
  limits) — see
  [SECURITY.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/SECURITY.md#bot-runtime--what-the-platform-enforces-on-uploaded-bots).
