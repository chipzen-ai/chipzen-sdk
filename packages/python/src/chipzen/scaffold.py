"""Scaffold a new bot project with starter files.

Creates a directory with everything needed to get started building
a Chipzen poker bot.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAIN_PY_TEMPLATE = '''\
"""My Chipzen poker bot."""

from chipzen import Bot, GameState, Action


class MyBot(Bot):
    """A starter bot -- customize the decide() method to build your strategy."""

    def decide(self, state: GameState) -> Action:
        # Your strategy goes here!
        # state.valid_actions tells you what you can do: fold, check, call, raise
        # state.hole_cards has your two cards
        # state.board has the community cards
        # state.pot is the total pot size
        # state.to_call is how much you need to put in to call

        # Simple default: check when free, call small bets, fold large ones
        if "check" in state.valid_actions:
            return Action.check()

        if state.to_call < state.pot * 0.3:
            return Action.call()

        return Action.fold()

    def on_hand_start(self, hand_number: int, hole_cards: list) -> None:
        """Called at the start of each hand. Use for per-hand setup."""
        pass

    def on_hand_result(self, result: dict) -> None:
        """Called after each hand. Use to track opponent tendencies."""
        pass
'''

REQUIREMENTS_TEMPLATE = """\
# Chipzen bot dependencies
# Only packages in the platform allow-list can be installed in the sandbox.
#
# Allowed packages:
#   websockets    (included automatically)
#   numpy         (for numerical computing)
#   scipy         (for scientific computing)
#   chipzen-sdk   (included automatically)
#
# Add your dependencies below:
"""

README_TEMPLATE = """\
# {name}

A poker bot for the [Chipzen](https://chipzen.ai) platform.

## Quick Start

1. Install the SDK:
   ```bash
   pip install chipzen-bot
   ```

2. Edit `main.py` to implement your strategy in the `decide()` method.

3. Validate before uploading:
   ```bash
   chipzen-sdk validate .
   ```

4. Zip and upload:
   ```bash
   zip {name}.zip main.py requirements.txt
   # Upload via the Chipzen web UI
   ```

## Bot API

Your bot receives a `GameState` with:
- `hole_cards` -- your two private cards
- `board` -- community cards dealt so far
- `pot` -- total chips in the pot
- `to_call` -- chips needed to call (0 means you can check)
- `valid_actions` -- what you can do right now
- `min_raise` / `max_raise` -- valid raise range

Return an `Action`:
- `Action.fold()`
- `Action.check()`
- `Action.call()`
- `Action.raise_to(amount)`

## Tips

- The platform enforces a 500ms decision timeout. Keep your `decide()` fast.
- Test against all three built-in opponents: `random`, `call`, `tight`
- Run `chipzen-sdk validate .` before every upload to catch problems early.
"""

GITIGNORE_TEMPLATE = """\
__pycache__/
*.pyc
*.pyo
.env
.venv/
venv/
*.egg-info/
dist/
build/
.pytest_cache/
"""


def scaffold_bot(
    name: str,
    *,
    parent_dir: Path | None = None,
) -> Path:
    """Create a new bot project directory with starter files.

    Args:
        name: Name of the bot project (used as directory name).
        parent_dir: Where to create the directory (default: cwd).

    Returns:
        Path to the created directory.

    Raises:
        FileExistsError: If the directory already exists.
    """
    if parent_dir is None:
        parent_dir = Path.cwd()

    bot_dir = parent_dir / name

    if bot_dir.exists():
        raise FileExistsError(f"Directory already exists: {bot_dir}")

    bot_dir.mkdir(parents=True)

    (bot_dir / "main.py").write_text(MAIN_PY_TEMPLATE)
    (bot_dir / "requirements.txt").write_text(REQUIREMENTS_TEMPLATE)
    (bot_dir / "README.md").write_text(README_TEMPLATE.format(name=name))
    (bot_dir / ".gitignore").write_text(GITIGNORE_TEMPLATE)

    return bot_dir


def init_cli(args: list[str] | None = None) -> None:
    """CLI entry point for the init command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="chipzen-sdk init",
        description="Scaffold a new Chipzen poker bot project",
    )
    parser.add_argument(
        "name",
        help="Name for the bot project directory",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Parent directory to create the project in (default: current directory)",
    )

    parsed = parser.parse_args(args)

    parent = Path(parsed.dir) if parsed.dir else None

    try:
        bot_dir = scaffold_bot(parsed.name, parent_dir=parent)
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Created bot project: {bot_dir}")
    print()
    print("Next steps:")
    print(f"  cd {parsed.name}")
    print("  # Edit main.py to implement your strategy")
    print("  chipzen-sdk validate .")
