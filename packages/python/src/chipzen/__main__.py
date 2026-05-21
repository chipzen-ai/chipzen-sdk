"""CLI entry point for the chipzen-sdk package.

Two console scripts are wired to this module:

- ``chipzen-sdk``: the original bot-build / validate / scaffold CLI.
- ``chipzen``:     a shorter alias used by the ``run-external`` subcommand
                   landed for External-API Issue 25 (chipzen-ai/chipzen-sdk#44).
                   ``chipzen <command>`` is functionally equivalent to
                   ``chipzen-sdk <command>``; the alias exists so the
                   external-API onboarding docs can use the friendlier
                   ``chipzen run-external my_bot.py`` form.

Usage::

    chipzen-sdk init     my_bot
    chipzen-sdk validate ./my_bot/
    chipzen      run-external ./my_bot.py
"""

from __future__ import annotations

import sys

COMMANDS = {
    "init": "Scaffold a new bot project with starter files",
    "validate": "Check if a bot will pass the platform upload and build process",
    "run-external": (
        "Run an external-API bot from a Python file "
        "(chipzen.toml + Bot subclass discovery)"
    ),
}


def _print_help() -> None:
    """Print top-level help with all available commands."""
    print("Chipzen Poker Bot SDK")
    print()
    print("Usage: chipzen <command> [options]")
    print()
    print("Commands:")
    for cmd, desc in COMMANDS.items():
        print(f"  {cmd:<14} {desc}")
    print()
    print("Run 'chipzen <command> --help' for details on a specific command.")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        _print_help()
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    command = sys.argv[1]
    remaining = sys.argv[2:]

    if command == "validate":
        from chipzen.validate import validate_cli

        validate_cli(remaining)
    elif command == "init":
        from chipzen.scaffold import init_cli

        init_cli(remaining)
    elif command == "run-external":
        from chipzen.run_external import run_external_cli

        run_external_cli(remaining)
    else:
        print(f"Unknown command: {command}")
        print()
        _print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
