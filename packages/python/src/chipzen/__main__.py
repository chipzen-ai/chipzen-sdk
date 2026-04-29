"""CLI entry point for the chipzen-sdk package.

Usage:
    chipzen-sdk init     my_bot
    chipzen-sdk validate ./my_bot/
"""

from __future__ import annotations

import sys

COMMANDS = {
    "init": "Scaffold a new bot project with starter files",
    "validate": "Check if a bot will pass the platform upload and build process",
}


def _print_help() -> None:
    """Print top-level help with all available commands."""
    print("Chipzen Poker Bot SDK")
    print()
    print("Usage: chipzen-sdk <command> [options]")
    print()
    print("Commands:")
    for cmd, desc in COMMANDS.items():
        print(f"  {cmd:<12} {desc}")
    print()
    print("Run 'chipzen-sdk <command> --help' for details on a specific command.")


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
    else:
        print(f"Unknown command: {command}")
        print()
        _print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
