#!/usr/bin/env python3
"""Drift-guard for the Big Bad Bots log-cap wrapper (``log_cap.py``).

Every Big Bad Bots bot runs under the platform's per-match log cap: the image's
ENTRYPOINT is ``python -I -S -u /opt/chipzen/log_cap.py --`` and the bot
command is the ``CMD`` it wraps. The wrapper caps how much one bot may log in
one match (lines and bytes, set by the platform through
``CHIPZEN_LOG_CAP_LINES`` / ``CHIPZEN_LOG_CAP_BYTES``), then prints one
``[chipzen] log capped:`` marker and drops the rest.

The platform owns the wrapper's behaviour, so its canonical home is the
platform repo (``chipzen-ai/Chipzen``, ``src/chipzen/bots/log_cap.py``). This
repo carries a byte-identical copy:

* ``packages/common/log_cap.py``: the SDK's reference copy, pinned by
  ``packages/common/log_cap.py.sha256``;
* one copy in each Big Bad Bots starter folder (:data:`STARTER_COPIES`), so a
  builder who copies a starter folder out of this repo still has everything
  ``docker build .`` needs.

``--check`` (CI's "Log-cap wrapper mirror guard" job) fails unless the
reference copy, its ``.sha256``, :data:`EXPECTED_DIGEST` and every starter copy
all agree. The platform repo pins the same digest in its own test, so changing
the wrapper is a paired change: update it there, copy it here with ``--sync``,
and bump :data:`EXPECTED_DIGEST` and the platform's pin together.

Hashes are over the raw bytes. ``.gitattributes`` marks every copy ``-text``
so no checkout converts line endings.

Usage::

    python scripts/log_cap_hash.py            # print the reference copy's digest
    python scripts/log_cap_hash.py --check    # verify every copy against the pin
    python scripts/log_cap_hash.py --sync     # copy the reference into every starter
                                              # and rewrite the .sha256
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The SDK's reference copy and its committed digest.
REFERENCE = REPO_ROOT / "packages" / "common" / "log_cap.py"
PIN = REPO_ROOT / "packages" / "common" / "log_cap.py.sha256"

#: Every starter folder that ships the wrapper next to its Dockerfile.
STARTER_COPIES = (
    "packages/python/starters/bigbadbots/rusher/log_cap.py",
    "packages/python/starters/bigbadbots/zoner/log_cap.py",
    "packages/python/starters/bigbadbots/turtle/log_cap.py",
    "packages/javascript/starters/bigbadbots/rusher/log_cap.py",
)

#: The cross-repo anchor: the sha256 of ``src/chipzen/bots/log_cap.py`` in
#: chipzen-ai/Chipzen, which pins the same value. A wrapper change bumps both.
EXPECTED_DIGEST = "06d8129270e752ca95163b4bc50687dec6b93b5e94486c445ec01651fc6b365e"

_BOTH_REPOS = (
    "The wrapper's canonical home is chipzen-ai/Chipzen (src/chipzen/bots/log_cap.py). "
    "Change it there, copy it to packages/common/log_cap.py, run "
    "`python scripts/log_cap_hash.py --sync`, and bump EXPECTED_DIGEST here and the "
    "platform repo's pin together."
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check() -> list[str]:
    """Every problem found, as one message each (empty when all copies agree)."""
    problems: list[str] = []
    if not REFERENCE.is_file():
        return [f"{REFERENCE.relative_to(REPO_ROOT)} is missing. {_BOTH_REPOS}"]
    reference = digest(REFERENCE)
    pinned = PIN.read_text(encoding="utf-8").strip() if PIN.is_file() else "<missing>"
    if reference != pinned:
        problems.append(
            "packages/common/log_cap.py does not match packages/common/log_cap.py.sha256 "
            f"(file {reference}, pin {pinned}). {_BOTH_REPOS}"
        )
    if reference != EXPECTED_DIGEST:
        problems.append(
            "packages/common/log_cap.py does not match EXPECTED_DIGEST, the platform's copy "
            f"(file {reference}, expected {EXPECTED_DIGEST}). {_BOTH_REPOS}"
        )
    for relative in STARTER_COPIES:
        path = REPO_ROOT / relative
        if not path.is_file():
            problems.append(f"{relative} is missing. Run `python scripts/log_cap_hash.py --sync`.")
            continue
        found = digest(path)
        if found != EXPECTED_DIGEST:
            problems.append(
                f"{relative} differs from the pinned wrapper (file {found}, expected "
                f"{EXPECTED_DIGEST}). Starters ship the wrapper unchanged; run "
                "`python scripts/log_cap_hash.py --sync`."
            )
    return problems


def sync() -> None:
    for relative in STARTER_COPIES:
        shutil.copyfile(REFERENCE, REPO_ROOT / relative)
    PIN.write_text(digest(REFERENCE) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify every copy against the pin")
    group.add_argument("--sync", action="store_true", help="copy the reference into the starters")
    args = parser.parse_args(argv)

    if args.sync:
        sync()
        print(f"synced {len(STARTER_COPIES)} starter copies; pin = {digest(REFERENCE)}")
        if digest(REFERENCE) != EXPECTED_DIGEST:
            print("now bump EXPECTED_DIGEST here and the platform repo's pin together")
        return 0
    if args.check:
        problems = check()
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(
            f"log_cap.py OK: reference, pin and {len(STARTER_COPIES)} starter copies = "
            f"{EXPECTED_DIGEST}"
        )
        return 0
    print(digest(REFERENCE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
