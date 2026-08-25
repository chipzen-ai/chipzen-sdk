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

**What must stay identical across the repos is the NORMALIZATION — not this
whole file.** ``MIRROR_HEADER_END``, ``PATH_REWRITES``, :func:`normalize` and
:func:`compute_hash` are what decide a digest; if those diverge the two sides
compute different hashes for the same text and the guard silently stops meaning
anything. The ``.sha256`` files stay byte-identical too — that is the artefact
the pairing rests on. Everything else may legitimately differ: the prose names a
repo, and ``MIRRORED_DOCS`` names layout-specific paths (``docs/protocol/`` here,
``docs/arch/`` in the private repo).

:data:`EXPECTED_DIGESTS` is what makes ``--check`` a **cross-repo** guard rather
than a self-consistency check (chipzen-ai/Chipzen#4242). Without it, editing a
doc here and re-running ``--write`` here went green on this side while the other
side stayed green too — its own pinned expectations were the only cross-repo
anchor and this side had no equivalent, so the pairing could be broken with no
red anywhere. A real content edit therefore has to bump the pin **in both repos**,
which is a reviewable diff instead of a silent divergence.

Same shape as the vendored ``rabbot_dsl`` drift-guard.

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
    # The two Layer 2 variant dialects (chipzen-ai/Chipzen#4242).
    "DRAW27-GAME-STATE-PROTOCOL": "docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md",
    "OFC-GAME-STATE-PROTOCOL": "docs/protocol/OFC-GAME-STATE-PROTOCOL.md",
    # The variant-agnostic Layer 2 baseline the two dialect specs above (and
    # every future one) inherit instead of copy-pasting
    # (chipzen-ai/Chipzen#4484). Mirrored on exactly the same terms they are.
    "LAYER2-COMMON": "docs/protocol/LAYER2-COMMON.md",
}

#: The CROSS-REPO digest of every mirrored doc, pinned. See the module docstring:
#: the ``.sha256`` files alone only prove this side is self-consistent, so
#: ``--check`` compares against these constants as well.
#:
#: Changing a doc for real means: edit BOTH repos to the same bytes, run
#: ``--write`` in BOTH, and bump this dict here and the matching expectations in
#: the private repo's mirror test, in lockstep. A digest that moves on one side
#: only is exactly the divergence this exists to catch.
EXPECTED_DIGESTS: dict[str, str] = {
    "DRAW27-GAME-STATE-PROTOCOL": (
        "bdfd3cb3c22494ffcea4451b2209869ca869eee375415e9c483d18f0165166c1"
    ),
    "EXTERNAL-API-BOT-PROTOCOL": (
        "7722496749ca38a52e75b592df13ecba6b076d80a17721be63db14bb46eb9429"
    ),
    "LAYER2-COMMON": ("23e075ffa970c77f609c3fa137a62f2b95a077775bb9894ec3868f74f56478d7"),
    "OFC-GAME-STATE-PROTOCOL": ("3dcf7c806b28e0e117c182795384f40891e2e65dc52ea1e2a805398930abf409"),
    "POKER-GAME-STATE-PROTOCOL": (
        "417007916d3ad0a19b1c370fb1f15a8c3041803f69be48d4122129d016ec5a7b"
    ),
    "TRANSPORT-PROTOCOL": ("9833c50bbb468ac9e4a6bfeaed38a59788638c260c6e90411415c1f1c25ded91"),
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
    """Verify one mirrored doc against its committed digest AND the cross-repo pin.

    Two assertions, not one. The ``.sha256`` beside the doc only proves this
    repo is internally consistent — re-running ``--write`` here satisfies it
    unconditionally. :data:`EXPECTED_DIGESTS` is the value the OTHER repo also
    pins, so it is the assertion that survives a one-sided ``--write``.
    """
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

    pinned = EXPECTED_DIGESTS.get(name)
    if pinned is None:
        return False, (
            f"{name} is in MIRRORED_DOCS with no entry in EXPECTED_DIGESTS."
            "\n  A doc guarded only by its own .sha256 has a self-consistency check"
            "\n  and NO cross-repo anchor: an edit here plus --write here would go"
            "\n  green on both sides. Pin the digest in BOTH repos."
        )
    if computed != pinned:
        return False, (
            f"{name} drift vs the mirror in chipzen-ai/Chipzen."
            f"\n    pinned:   {pinned}"
            f"\n    computed: {computed}"
            "\n  Mirror the change into the other repo, run --write in BOTH, and bump"
            "\n  the pin in BOTH in lockstep. The .sha256 matching is not enough:"
            "\n  --write satisfies that on one side alone, which is the divergence"
            "\n  this pin exists to catch."
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
            # newline="" suppresses Python's text-mode translation. Without it,
            # a --write run on Windows emits CRLF while the same command on
            # Linux emits LF, so the two repos' .sha256 files stop being
            # byte-identical — the invariant this guard's whole cross-repo
            # story rests on. `--check` would not notice (it strips), so the
            # divergence is silent: exactly the failure mode this file exists
            # to prevent, one level down. The private repo's copy has carried
            # this since #4105; this side had not (chipzen-ai/Chipzen#4242).
            hash_path(name).write_text(digest + "\n", encoding="utf-8", newline="")
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
