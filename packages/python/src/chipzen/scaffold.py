"""Scaffold a new bot project from the Python starter.

``chipzen-sdk init <name>`` writes a copy of the canonical starter at
``packages/python/starters/python/`` in the SDK repo: ``bot.py`` with its
``main()`` entry point, the IP-protected ``Dockerfile``, ``.dockerignore``,
``requirements.txt``, ``README.md`` and ``.gitignore``.

The files are not duplicated in this module. The wheel carries the starter
folder as package data (``chipzen/_starters/python``, mapped in
``pyproject.toml``), and a source checkout reads the starter folder itself,
so ``init`` and the starter cannot drift apart again (issue #260: the old
inline templates wrote a ``main.py`` with no ``main()`` and no Dockerfile).

``--template`` picks another starter: ``chess`` (the legal-move chess starter)
or ``chess-uci`` (the UCI bridge). Both are PREVIEW: chess is not live yet.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.abc import Traversable

#: Every file of the Python starter, in the order ``init`` writes them. A
#: test pins this list against the starter folder and against the wheel's
#: package-data mapping in ``pyproject.toml``, so a file added to one is not
#: silently left out of the other.
STARTER_FILES = (
    "bot.py",
    "requirements.txt",
    "Dockerfile",
    ".dockerignore",
    "README.md",
    ".gitignore",
)

#: Where the wheel puts the starter, relative to the ``chipzen`` package.
PACKAGED_STARTER = "_starters/python"

#: The starter folder in a source checkout (``src/chipzen/scaffold.py`` ->
#: ``packages/python/starters/python``). An editable install has no package
#: data, so it reads the files from here.
SOURCE_STARTER = Path(__file__).resolve().parent.parent.parent / "starters" / "python"

#: The chess starters (PREVIEW: chess is not live). Same scheme as the Python
#: starter: the wheel carries each as package data, a source checkout reads
#: the folder itself, and a test pins each list against its folder and the
#: wheel mapping.
CHESS_STARTER_FILES = (
    "bot.py",
    "requirements.txt",
    "Dockerfile",
    ".dockerignore",
    "README.md",
    ".gitignore",
)
CHESS_UCI_STARTER_FILES = (*CHESS_STARTER_FILES, "engine/README.md")


@dataclass(frozen=True)
class Template:
    """One starter ``init`` can write."""

    #: The folder under ``packages/python/starters`` in a source checkout.
    source: str
    #: Where the wheel puts it, relative to the ``chipzen`` package.
    packaged: str
    #: Every file, relative to the folder, in the order ``init`` writes them.
    files: tuple[str, ...]
    #: The method to edit, for the "Next steps" hint.
    edit_hint: str


TEMPLATES: dict[str, Template] = {
    "python": Template("python", PACKAGED_STARTER, STARTER_FILES, "decide()"),
    "chess": Template(
        "chess/legal-mover", "_starters/chess-legal-mover", CHESS_STARTER_FILES, "choose_move()"
    ),
    "chess-uci": Template(
        "chess/uci-bridge",
        "_starters/chess-uci-bridge",
        CHESS_UCI_STARTER_FILES,
        "the engine settings, and put your engine in engine/",
    ),
}

#: The template ``init`` writes when none is named.
DEFAULT_TEMPLATE = "python"


def starter_root(template: str = DEFAULT_TEMPLATE) -> Traversable:
    """The folder ``init`` copies: the packaged starter, else the source tree's.

    Raises:
        FileNotFoundError: Neither is present (a broken install).
        KeyError: ``template`` is not one of :data:`TEMPLATES`.
    """
    spec = TEMPLATES[template]
    packaged = resources.files("chipzen").joinpath(spec.packaged)
    if packaged.joinpath("bot.py").is_file():
        return packaged
    source = SOURCE_STARTER.parent / spec.source
    if (source / "bot.py").is_file():
        return source
    raise FileNotFoundError(
        "The Python starter is missing from this chipzen-bot install. "
        "Reinstall it with `pip install --force-reinstall chipzen-bot`."
    )


def scaffold_bot(
    name: str,
    *,
    parent_dir: Path | None = None,
    template: str = DEFAULT_TEMPLATE,
) -> Path:
    """Create a new bot project directory holding a copy of a starter.

    Args:
        name: Name of the bot project (used as directory name).
        parent_dir: Where to create the directory (default: cwd).
        template: Which starter: ``"python"`` (the default, a poker bot),
            ``"chess"`` or ``"chess-uci"`` (both preview).

    Returns:
        Path to the created directory.

    Raises:
        FileExistsError: If the directory already exists.
        FileNotFoundError: If the starter is missing from the install.
        ValueError: ``template`` is not one of :data:`TEMPLATES`.
    """
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; expected one of: {', '.join(TEMPLATES)}")
    if parent_dir is None:
        parent_dir = Path.cwd()

    bot_dir = parent_dir / name

    if bot_dir.exists():
        raise FileExistsError(f"Directory already exists: {bot_dir}")

    root = starter_root(template)
    # Read everything before creating the folder, so a broken install leaves
    # nothing half-written behind.
    contents = {
        filename: root.joinpath(*filename.split("/")).read_bytes()
        for filename in TEMPLATES[template].files
    }

    bot_dir.mkdir(parents=True)
    for filename, data in contents.items():
        # Bytes, not text: the copy is exact, line endings included.
        target = bot_dir.joinpath(*filename.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return bot_dir


def init_cli(args: list[str] | None = None) -> None:
    """CLI entry point for the init command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="chipzen-sdk init",
        description="Scaffold a new Chipzen poker bot project from the Python starter",
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
    parser.add_argument(
        "--template",
        choices=tuple(TEMPLATES),
        default=DEFAULT_TEMPLATE,
        help=(
            "Which starter to write: python (a poker bot, the default), chess (picks a "
            "legal move) or chess-uci (a bridge to your UCI engine). Chess is a PREVIEW: "
            "not live on Chipzen yet."
        ),
    )

    parsed = parser.parse_args(args)

    parent = Path(parsed.dir) if parsed.dir else None

    try:
        bot_dir = scaffold_bot(parsed.name, parent_dir=parent, template=parsed.template)
    except (FileExistsError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Created bot project: {bot_dir}")
    print()
    print("Next steps:")
    print(f"  cd {parsed.name}")
    hint = TEMPLATES[parsed.template].edit_hint
    print(f"  # Edit {hint} in bot.py. Keep main() at the bottom: the image calls it.")
    print("  chipzen-sdk validate .")
    print("  docker build -t my-bot:v1 .")
    print("  docker save my-bot:v1 | gzip > my-bot.tar.gz")
    print("  # Upload my-bot.tar.gz through the Chipzen web UI. See README.md.")
