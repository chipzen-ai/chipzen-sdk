"""Static checks on a bot folder's Dockerfile and the entry point it starts.

The platform runs the Docker image a builder uploads, not the folder
``validate`` reads. A folder can pass every check on ``decide()`` and still
build an image that starts nothing: issue #260 was ``chipzen-sdk init``
writing a ``main.py`` with no ``main()`` and no Dockerfile. The checks here
close that gap for poker bots:

* ``dockerfile``: the folder has a ``Dockerfile`` whose final stage sets an
  ENTRYPOINT or CMD, and when that command names the bot's module, it names
  the entry point ``validate`` checked.
* ``entry_main``: the entry point has what that command runs. A command that
  imports ``main`` from the module (the starter's
  ``from bot import main; main()``) needs a top-level ``def main()``; a
  command that runs the file as a script needs an
  ``if __name__ == "__main__":`` block. With no Dockerfile, or a command this
  module cannot read, the starter's contract applies: ``def main()``.

The Dockerfile parsing helpers are shared with the Big Bad Bots log-cap check
in :mod:`chipzen.validate`.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Literal

Severity = Literal["pass", "warn", "fail"]
Result = tuple[Severity, str, str]

#: Where a fresh copy of the files comes from, for the fix-it messages.
_STARTER_HINT = (
    "Run `chipzen-sdk init <name>` for a fresh project to copy from (it writes the "
    "Python starter, packages/python/starters/python in the SDK repo)."
)

_IMPORT_MAIN = re.compile(r"\bfrom\s+([A-Za-z_][\w.]*)\s+import\s+(?:\(?\s*)?main\b")
_RUN_MODULE = re.compile(r"(?:^|\s)-m\s+([A-Za-z_][\w.]*)")
_RUN_SCRIPT = re.compile(r"([\w./-]+)\.py\b")

StartMode = Literal["import_main", "script"]


def dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """``(KEYWORD, arguments)`` per instruction, comments and line continuations resolved."""
    instructions: list[tuple[str, str]] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        instructions.append(_instruction(current + line))
        current = ""
    if current.strip():
        instructions.append(_instruction(current))
    return instructions


def _instruction(text: str) -> tuple[str, str]:
    parts = text.strip().split(None, 1)
    return parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""


def copy_operands(arguments: str) -> tuple[list[str], list[str]]:
    """``(flags, operands)`` of a COPY / ADD, in shell or JSON form."""
    words = arguments.split()
    flags = []
    while words and words[0].startswith("--"):
        flags.append(words.pop(0))
    rest = " ".join(words)
    if rest.startswith("["):
        try:
            parsed = json.loads(rest)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(part, str) for part in parsed):
            return flags, parsed
    return flags, words


def final_stage(instructions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The instructions after the last ``FROM`` (all of them if there is none)."""
    starts = [i for i, (keyword, _) in enumerate(instructions) if keyword == "FROM"]
    return instructions[starts[-1] + 1 :] if starts else instructions


def _command_words(arguments: str) -> list[str]:
    """An ENTRYPOINT / CMD as words: exec form decoded, shell form split."""
    if arguments.startswith("["):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(part, str) for part in parsed):
            return parsed
    return arguments.split()


def start_command(bot_dir: Path) -> str | None:
    """The command the image starts (final stage ENTRYPOINT + CMD), or None if unset."""
    stage = final_stage(dockerfile_instructions((bot_dir / "Dockerfile").read_text("utf-8")))
    words: list[str] = []
    for keyword in ("ENTRYPOINT", "CMD"):
        found = [arguments for kw, arguments in stage if kw == keyword]
        if found:
            words.extend(_command_words(found[-1]))
    return " ".join(words) if words else None


def _started_module(command: str) -> tuple[StartMode, str] | None:
    """What the command starts: ``(mode, module or file name)``, or None if unclear."""
    match = _IMPORT_MAIN.search(command)
    if match:
        return "import_main", match.group(1)
    match = _RUN_MODULE.search(command)
    if match:
        return "script", match.group(1)
    match = _RUN_SCRIPT.search(command)
    if match:
        return "script", match.group(1).rsplit("/", 1)[-1]
    return None


def _has_main_function(tree: ast.Module) -> bool:
    return any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body)


def _has_main_guard(tree: ast.Module) -> bool:
    """A top-level ``if __name__ == "__main__":`` (either operand order)."""
    for node in tree.body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        operands = [test.left, *test.comparators]
        names = [o for o in operands if isinstance(o, ast.Name) and o.id == "__name__"]
        values = [o for o in operands if isinstance(o, ast.Constant) and o.value == "__main__"]
        if names and values and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
            return True
    return False


def check_poker_image(bot_dir: Path, entry_point: Path, tree: ast.Module) -> list[Result]:
    """The ``dockerfile`` and ``entry_main`` checks for a poker bot folder."""
    results: list[Result] = []
    mode: StartMode = "import_main"
    module = entry_point.stem

    if not (bot_dir / "Dockerfile").is_file():
        results.append(
            (
                "fail",
                "dockerfile",
                f"No Dockerfile in {bot_dir}. The platform runs the Docker image you upload "
                "(docker build, then docker save | gzip), so this folder cannot be packaged. "
                "Copy Dockerfile and .dockerignore into it. " + _STARTER_HINT,
            )
        )
    else:
        command = start_command(bot_dir)
        started = _started_module(command) if command else None
        if command is None:
            results.append(
                (
                    "fail",
                    "dockerfile",
                    "The Dockerfile's final stage sets no ENTRYPOINT or CMD, so the image "
                    "runs its base image's default command, not your bot. The starter's is "
                    '`ENTRYPOINT ["python", "-c", "from bot import main; main()"]`.',
                )
            )
        elif started is not None and started[1] != module:
            results.append(
                (
                    "fail",
                    "dockerfile",
                    f"The Dockerfile starts `{command}`, which runs {started[1]}, but the "
                    f"entry point is {entry_point.name}. Rename the file or change the "
                    "ENTRYPOINT so they match.",
                )
            )
        else:
            if started is not None:
                mode = started[0]
            results.append(("pass", "dockerfile", f"Dockerfile starts the bot with: {command}"))

    if mode == "script":
        if _has_main_guard(tree):
            results.append(
                ("pass", "entry_main", f'{entry_point.name} has an `if __name__ == "__main__":`')
            )
        else:
            results.append(
                (
                    "fail",
                    "entry_main",
                    f"The Dockerfile runs {entry_point.name} as a script, but it has no "
                    '`if __name__ == "__main__":` block that starts the bot, so the container '
                    "would define the bot class and exit. Add `main()` and the block from the "
                    "starter. " + _STARTER_HINT,
                )
            )
    elif _has_main_function(tree):
        results.append(("pass", "entry_main", f"{entry_point.name} defines main()"))
    else:
        results.append(
            (
                "fail",
                "entry_main",
                f"{entry_point.name} has no top-level main() function. The starter's image "
                f"starts the bot with `from {module} import main; main()`, and main() reads "
                "CHIPZEN_WS_URL and connects (run_bot), so without it the image builds but "
                "the bot never plays after upload. Copy main() from the starter. " + _STARTER_HINT,
            )
        )
    return results
