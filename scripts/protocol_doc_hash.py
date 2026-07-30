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
DOC_PATH = REPO_ROOT / "docs" / "EXTERNAL-API-BOT-PROTOCOL.md"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify against the committed digest")
    group.add_argument("--write", action="store_true", help="rewrite the committed digest")
    args = parser.parse_args(argv)

    if not DOC_PATH.exists():
        print(f"error: {DOC_PATH} not found", file=sys.stderr)
        return 2

    computed = compute_hash()

    if args.write:
        HASH_PATH.write_text(computed + "\n", encoding="utf-8")
        print(f"wrote {HASH_PATH.name}: {computed}")
        return 0

    if not args.check:
        print(computed)
        return 0

    if not HASH_PATH.exists():
        print(f"error: {HASH_PATH} is missing — run with --write", file=sys.stderr)
        return 1

    committed = HASH_PATH.read_text(encoding="utf-8").strip()
    if computed != committed:
        print(
            "EXTERNAL-API-BOT-PROTOCOL.md drift: the normalized doc does not match\n"
            "the committed digest.\n"
            f"  committed: {committed}\n"
            f"  computed:  {computed}\n\n"
            "Content edits belong in the CANONICAL copy first\n"
            "(chipzen-ai/chipzen-sdk docs/EXTERNAL-API-BOT-PROTOCOL.md). Mirror the\n"
            "change into the other repo, then run --write in BOTH repos so the two\n"
            "digests stay identical.",
            file=sys.stderr,
        )
        return 1

    print(f"EXTERNAL-API-BOT-PROTOCOL.md matches the committed digest: {computed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
