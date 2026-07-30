#!/usr/bin/env python3
"""Drift-guard for the mirrored External-API bot-protocol doc.

``docs/EXTERNAL-API-BOT-PROTOCOL.md`` exists in two repos:

* **canonical** — ``chipzen-ai/chipzen-sdk`` (public; the copy bot developers read)
* **mirror**    — ``chipzen-ai/Chipzen`` (private platform repo)

They are kept in lockstep by hashing a *normalized* form of the doc and
committing the digest to ``docs/EXTERNAL-API-BOT-PROTOCOL.sha256`` in **both**
repos. Each repo's CI runs ``python scripts/protocol_doc_hash.py --check``, so a
one-sided edit turns that side red immediately, and re-hashing only one side
leaves the *other* side red until the mirror is updated too.

This file and the ``.sha256`` beside the doc are byte-identical across the two
repos — do not diverge them. Same shape as the vendored ``rabbot_dsl``
drift-guard (``tests/test_rabbot_dsl_drift.py`` on the platform side).

Normalization exists because the two copies legitimately cannot be
byte-identical:

1. The mirror carries a short "this is a mirror, edit the canonical" prologue,
   terminated by the marker line ``<!-- /mirror-header -->``. Everything up to
   and including that marker is dropped. The canonical has no marker, so
   nothing is dropped there.
2. Intra-repo links to the two-layer protocol specs resolve to ``docs/protocol/``
   in the SDK and ``docs/arch/`` in the platform repo. Those path segments are
   normalized onto the canonical ``protocol/`` form.

Everything else must match exactly — including wording, tables, and issue
references (always fully qualified, e.g. ``chipzen-ai/Chipzen#3742``, so they
resolve identically from either repo).

Usage::

    python scripts/protocol_doc_hash.py            # print the normalized digest
    python scripts/protocol_doc_hash.py --check    # verify against the .sha256
    python scripts/protocol_doc_hash.py --write    # re-hash after a real edit
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every doc mirrored across chipzen-ai/Chipzen and chipzen-ai/chipzen-sdk,
#: as {name: path relative to the repo root}.
#:
#: The two protocol specs were added after #4053. They previously had **no
#: guard at all**, and in that gap the public copy came to document
#: ``total_hands`` as a REQUIRED ``game_config`` field that does not exist
#: (matches are elimination-only since #1588) while omitting ``pot`` and
#: ``post_blind_stacks``, which the server does send. A spec nobody diffs is a
#: spec that drifts, and these are the specs every external bot author reads.
#:
#: This repo keeps them under ``docs/arch/``; the SDK under ``docs/protocol/``.
#: ``PATH_REWRITES`` normalizes that away, so the same digest is expected on
#: both sides.
MIRRORED_DOCS: dict[str, str] = {
    "EXTERNAL-API-BOT-PROTOCOL": "docs/EXTERNAL-API-BOT-PROTOCOL.md",
    "POKER-GAME-STATE-PROTOCOL": "docs/protocol/POKER-GAME-STATE-PROTOCOL.md",
    "TRANSPORT-PROTOCOL": "docs/protocol/TRANSPORT-PROTOCOL.md",
}


def doc_path(name: str) -> Path:
    return REPO_ROOT / MIRRORED_DOCS[name]


def hash_path(name: str) -> Path:
    """The digest file sits beside its doc, same stem."""
    return doc_path(name).with_suffix(".sha256")


DOC_PATH = REPO_ROOT / MIRRORED_DOCS["EXTERNAL-API-BOT-PROTOCOL"]
HASH_PATH = REPO_ROOT / "docs" / "EXTERNAL-API-BOT-PROTOCOL.sha256"

#: End of the mirror-only prologue. Present in the mirror, absent in the canonical.
MIRROR_HEADER_END = "<!-- /mirror-header -->"

#: Repo-local doc-path segments rewritten onto the canonical public form.
PATH_REWRITES = (
    ("docs/arch/", "docs/protocol/"),
    ("](arch/", "](protocol/"),
)


def normalize(text: str) -> str:
    """Return the comparable body of the doc (see the module docstring)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    marker_at = text.find(MIRROR_HEADER_END)
    if marker_at != -1:
        after = text.find("\n", marker_at)
        text = "" if after == -1 else text[after + 1 :]

    for old, new in PATH_REWRITES:
        text = text.replace(old, new)

    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def compute_hash(doc_path: Path = DOC_PATH) -> str:
    """sha256 of the normalized doc body, lowercase hex."""
    body = normalize(doc_path.read_text(encoding="utf-8"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def check_one(name: str) -> tuple[bool, str]:
    """Verify one mirrored doc against its committed digest."""
    path = doc_path(name)
    if not path.exists():
        return False, f"{name}: {path} not found"

    digest_file = hash_path(name)
    if not digest_file.exists():
        return False, f"{name}: {digest_file.name} is missing — run with --write"

    computed = compute_hash(path)
    committed = digest_file.read_text(encoding="utf-8").strip()
    if computed != committed:
        return False, (
            f"{name} drift: the normalized doc does not match its committed digest."
            f"\n    committed: {committed}"
            f"\n    computed:  {computed}"
            "\n  Mirror the change into the OTHER repo, then run --write in BOTH so"
            "\n  the digests stay identical. Re-hashing only one side leaves the"
            "\n  other side red — that is the point, not a bug."
        )
    return True, f"{name}: {computed}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify against the committed digests")
    group.add_argument("--write", action="store_true", help="rewrite the committed digests")
    parser.add_argument(
        "--doc",
        choices=sorted(MIRRORED_DOCS),
        help="operate on one doc only (default: every mirrored doc)",
    )
    args = parser.parse_args(argv)

    names = [args.doc] if args.doc else sorted(MIRRORED_DOCS)

    if args.write:
        for name in names:
            if not doc_path(name).exists():
                print(f"error: {doc_path(name)} not found", file=sys.stderr)
                return 2
            digest = compute_hash(doc_path(name))
            hash_path(name).write_text(digest + "\n", encoding="utf-8")
            print(f"wrote {hash_path(name).name}: {digest}")
        return 0

    if not args.check:
        for name in names:
            print(f"{name}: {compute_hash(doc_path(name))}")
        return 0

    failures = []
    for name in names:
        ok, message = check_one(name)
        if ok:
            print(f"  {message}")
        else:
            print(f"FAIL {message}", file=sys.stderr)
            failures.append(name)

    if failures:
        print(f"\nprotocol doc drift in: {', '.join(failures)}", file=sys.stderr)
        return 1

    print(f"all {len(names)} mirrored protocol docs match their committed digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
