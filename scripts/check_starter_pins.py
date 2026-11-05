#!/usr/bin/env python3
"""Guard: every starter's SDK pin admits the SDK version in this repo.

A starter pins the SDK from the registry, so nothing else in CI notices when a
release moves past the pin: ``^0.3.0`` stayed on 0.3.x through two minor
releases of ``@chipzen-ai/bot`` while local tests ran whatever was installed
(issue #218). CI's "Starter pin guard" job runs this script, so a version bump
that leaves a starter behind goes red in the PR that makes the bump.

Starters are found by globbing, so a new one is covered without touching this
file. A starter with no pin, or a pin this script cannot read, is a failure
rather than a pass.

The registries are not consulted. Each ``packages/<language>/RELEASING.md`` says
how to move a pin in the same PR as a bump, before the new version is published.

Usage (from anywhere; stdlib only on 3.11+)::

    python scripts/check_starter_pins.py
"""

from __future__ import annotations

import json
import operator
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, where the SDK's tests also run this
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
NPM_NAME = "@chipzen-ai/bot"

Version = tuple[int, int, int]

_CLAUSE = re.compile(r"(\^|~=|~|>=|<=|>|<|==|=|!=)?(\d+(?:\.\d+){0,2})")
_COMPARE = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "=": operator.eq,
    "==": operator.eq,
    "!=": operator.ne,
}
# A short version is a range for these in some ecosystems (npm's `=0.5` means
# 0.5.x), so they take a full X.Y.Z. `>=` and `<` mean the same either way.
_NEEDS_FULL_VERSION = {">", "<=", "=", "==", "!="}
_BUILD_DIRS = {"node_modules", "target", ".venv", "venv", "__pycache__"}


class UnreadablePinError(ValueError):
    """The pin is outside the small grammar this script understands."""


def parse_version(text: str) -> Version:
    """Parse a plain X.Y.Z. A pre-release is not one: the guard says so loudly."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        raise UnreadablePinError(f"{text!r} is not a plain X.Y.Z version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _pad(parts: list[int]) -> Version:
    major, minor, patch = (parts + [0, 0])[:3]
    return major, minor, patch


def _caret_upper(parts: list[int]) -> Version:
    """Upper bound of a caret range: the leftmost non-zero part may not change."""
    major, minor, patch = _pad(parts)
    if major > 0 or len(parts) == 1:
        return major + 1, 0, 0
    if minor > 0 or len(parts) == 2:
        return 0, minor + 1, 0
    return 0, 0, patch + 1


def _tilde_upper(parts: list[int]) -> Version:
    """Upper bound of an npm or Cargo tilde range."""
    major, minor, _ = _pad(parts)
    return (major + 1, 0, 0) if len(parts) == 1 else (major, minor + 1, 0)


def _compatible_upper(parts: list[int]) -> Version:
    """Upper bound of PEP 440 ``~=``: all but the last given part stay fixed."""
    if len(parts) < 2:
        raise UnreadablePinError("PEP 440 `~=` needs at least two version parts")
    prefix = parts[:-1]
    prefix[-1] += 1
    return _pad(prefix)


def _clause_admits(clause: str, default_op: str | None, version: Version) -> bool:
    match = _CLAUSE.fullmatch(clause)
    if match is None:
        raise UnreadablePinError(f"cannot read {clause!r}")
    op = match.group(1) or default_op
    if op is None:
        raise UnreadablePinError(f"{clause!r} has no operator")
    parts = [int(part) for part in match.group(2).split(".")]
    floor = _pad(parts)
    if op == "^":
        return floor <= version < _caret_upper(parts)
    if op == "~":
        return floor <= version < _tilde_upper(parts)
    if op == "~=":
        upper = _compatible_upper(parts)  # first: it rejects `~=1` whatever the version
        return floor <= version < upper
    if op in _NEEDS_FULL_VERSION and len(parts) != 3:
        raise UnreadablePinError(f"{clause!r} needs a full X.Y.Z version")
    return _COMPARE[op](version, floor)


def _all_clauses(clauses: list[str], default_op: str | None, version: Version) -> bool:
    # A list, not a generator: every clause must be readable, even after a miss.
    return all([_clause_admits(clause, default_op, version) for clause in clauses])


def npm_admits(pin: str, version: Version) -> bool:
    """``||`` between alternatives, spaces between clauses; bare means exact."""
    alternatives = [alternative.split() for alternative in pin.split("||")]
    if not all(alternatives):
        raise UnreadablePinError(f"cannot read npm range {pin!r}")
    return any([_all_clauses(clauses, "=", version) for clauses in alternatives])


def cargo_admits(pin: str, version: Version) -> bool:
    """Commas between clauses; bare means caret."""
    return _all_clauses([clause.strip() for clause in pin.split(",")], "^", version)


def pep440_admits(pin: str, version: Version) -> bool:
    """Commas between clauses; an operator is required."""
    return _all_clauses([clause.strip() for clause in pin.split(",")], None, version)


def _requirements_pin(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"chipzen[-_]bot\b(.*)$", line.split("#")[0].strip(), re.IGNORECASE)
        if match:
            return match.group(1).strip() or None
    return None


def _package_json_pin(path: Path) -> str | None:
    return json.loads(path.read_text(encoding="utf-8")).get("dependencies", {}).get(NPM_NAME)


def _cargo_pin(path: Path) -> str | None:
    dependencies = tomllib.loads(path.read_text(encoding="utf-8")).get("dependencies", {})
    dependency = dependencies.get("chipzen-bot")
    return dependency.get("version") if isinstance(dependency, dict) else dependency


def _pyproject_version(path: Path) -> str:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"]


def _package_json_version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def _cargo_workspace_version(path: Path) -> str:
    return tomllib.loads(path.read_text(encoding="utf-8"))["workspace"]["package"]["version"]


# (package, manifest holding its version, reader of that version, starters dir,
#  starter manifest name, reader of a starter's pin, does a pin admit a version)
PACKAGES = (
    (
        "chipzen-bot (PyPI)",
        "packages/python/pyproject.toml",
        _pyproject_version,
        "packages/python/starters",
        "requirements.txt",
        _requirements_pin,
        pep440_admits,
    ),
    (
        NPM_NAME,
        "packages/javascript/package.json",
        _package_json_version,
        "packages/javascript/starters",
        "package.json",
        _package_json_pin,
        npm_admits,
    ),
    (
        "chipzen-bot (crates.io)",
        "packages/rust/Cargo.toml",
        _cargo_workspace_version,
        "packages/rust/starters",
        "Cargo.toml",
        _cargo_pin,
        cargo_admits,
    ),
)


def check(root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """Return (lines for starters that pass, lines for failures) under ``root``."""
    passed: list[str] = []
    failed: list[str] = []
    for name, manifest, read_version, starters, filename, read_pin, admits in PACKAGES:
        try:
            current = read_version(root / manifest)
            version = parse_version(current)
        except (OSError, KeyError, ValueError) as exc:
            failed.append(f"{manifest}: cannot read the {name} version: {exc}")
            continue
        found = [
            path
            for path in sorted((root / starters).rglob(filename))
            if not _BUILD_DIRS.intersection(path.relative_to(root).parts)
        ]
        if not found:
            failed.append(f"{starters}: no {filename} found, so no {name} starter was checked")
        for path in found:
            where = path.relative_to(root).as_posix()
            try:
                pin = read_pin(path)
                if not isinstance(pin, str):
                    failed.append(f"{where}: declares no version pin for {name}")
                elif admits(pin, version):
                    passed.append(f"{where}: {pin} admits {current}")
                else:
                    failed.append(
                        f"{where}: {pin} does not admit {current}, the {name} version in "
                        f"{manifest}. Move the pin in this PR (see that package's RELEASING.md)."
                    )
            except ValueError as exc:  # UnreadablePinError, or a malformed manifest
                failed.append(f"{where}: cannot read the {name} pin: {exc}")
    return passed, failed


def main() -> int:
    passed, failed = check()
    for line in passed:
        print(f"OK    {line}")
    for line in failed:
        print(f"FAIL  {line}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
