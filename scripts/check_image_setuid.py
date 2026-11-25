#!/usr/bin/env python3
"""Check: a built bot image has no setuid or setgid files.

A setuid or setgid file runs with its owner's or group's rights whoever
starts it, so one owned by root is a way back to uid 0 from the image's
non-root ``USER``. A bot needs none. Debian and ``python:*-slim`` bases ship
about a dozen (``su``, ``passwd``, ``mount`` and more). Every starter's final
stage clears them with::

    RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

and CI runs this script on each built starter image, so a starter that loses
that step (or a new starter without it) goes red (chipzen-ai/Chipzen#7830).

The scan reads the layers of ``docker save`` output (the tarball you upload),
not a running container, and works out the FINAL filesystem the container
starts from:

* layers apply bottom-up; an upper entry at the same path replaces a lower
  one, so ``chmod a-s`` in a later layer clears the bit;
* a whiteout (``.wh.<name>``) deletes a path and everything under it, and an
  opaque marker (``.wh..wh..opq``) hides what lower layers put in that
  directory;
* a non-directory entry hides whatever lower layers had below that path;
* a hard link shares its target's inode, so it counts when its target does.

Directories are ignored: setgid on a directory (Debian's ``/var/mail``) only
sets the group of new files and grants nothing at exec. A layer this script
cannot read (zstd, say) is a failure, not a pass.

Usage (stdlib only)::

    python scripts/check_image_setuid.py <image>          # runs `docker save`
    python scripts/check_image_setuid.py --tar <file>     # a saved tar (plain or gzip)

Exit status: 0 when the image is clean, 1 when it has setuid or setgid files,
2 when the image cannot be read.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: setuid (04000) and setgid (02000).
PRIVILEGE_BITS = 0o6000

#: The fix every starter uses, quoted in the failure message.
STRIP_COMMAND = "find / -xdev -perm /6000 -type f -exec chmod a-s {} +"

_WHITEOUT_PREFIX = ".wh."
_OPAQUE_MARKER = ".wh..wh..opq"


class ImageReadError(Exception):
    """The archive, its manifest or one of its layers cannot be read."""


@dataclass(frozen=True)
class _Write:
    mode: int
    is_dir: bool
    link_target: str | None = None


@dataclass
class _Layer:
    whiteouts: set[str] = field(default_factory=set)
    opaque_dirs: set[str] = field(default_factory=set)
    writes: dict[str, _Write] = field(default_factory=dict)


def _norm(name: str) -> str:
    """A tar member name as a path relative to ``/``, with no trailing slash."""
    path = posixpath.normpath("/" + name).lstrip("/")
    return "" if path == "." else path


def _read_layer(layer: tarfile.TarFile) -> _Layer:
    record = _Layer()
    for entry in layer:
        name = _norm(entry.name)
        directory, base = posixpath.split(name)
        if base == _OPAQUE_MARKER:
            record.opaque_dirs.add(directory)
            continue
        if base.startswith(_WHITEOUT_PREFIX):
            if base.startswith(_WHITEOUT_PREFIX * 2):
                continue  # other aufs metadata (.wh..wh.plnk ...), not a deletion
            record.whiteouts.add(posixpath.join(directory, base[len(_WHITEOUT_PREFIX) :]))
            continue
        counts = entry.isreg() or entry.islnk()
        link = _norm(entry.linkname) if entry.islnk() else None
        record.writes.pop(name, None)  # a repeated name: the last one wins
        record.writes[name] = _Write(entry.mode if counts else 0, entry.isdir(), link)
    return record


def _drop(state: dict[str, int], path: str, *, itself: bool) -> None:
    prefix = f"{path}/" if path else ""
    for key in [k for k in state if k.startswith(prefix) or (itself and k == path)]:
        del state[key]


def _apply(state: dict[str, int], record: _Layer) -> None:
    """Lay one layer over ``state`` (path -> mode of a privileged file), as overlayfs would."""
    for path in record.whiteouts:
        _drop(state, path, itself=True)
    for path in record.opaque_dirs:
        _drop(state, path, itself=False)
    for path, write in record.writes.items():
        state.pop(path, None)
        if not write.is_dir:
            _drop(state, path, itself=False)
    for path, write in record.writes.items():
        mode = write.mode
        if write.link_target is not None and not mode & PRIVILEGE_BITS:
            mode = state.get(write.link_target, 0)  # a hard link shares its target's inode
        if mode & PRIVILEGE_BITS:
            state[path] = mode


def find_privileged_files(tar_path: Path | str) -> list[tuple[str, int]]:
    """``(path, mode)`` of every setuid / setgid file in the images of a ``docker save`` tar.

    Raises :class:`ImageReadError` when the archive, its manifest or a layer
    cannot be read.
    """
    try:
        archive = tarfile.open(tar_path, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise ImageReadError(f"cannot open {tar_path}: {exc}") from exc
    with archive:
        try:
            manifest_file = archive.extractfile("manifest.json")
        except KeyError:
            manifest_file = None
        if manifest_file is None:
            raise ImageReadError("no manifest.json: not `docker save` output")
        try:
            manifest = json.load(manifest_file)
        except ValueError as exc:
            raise ImageReadError(f"manifest.json is not JSON: {exc}") from exc
        if not isinstance(manifest, list) or not manifest:
            raise ImageReadError("manifest.json lists no images")

        records: dict[str, _Layer] = {}
        found: set[tuple[str, int]] = set()
        for image in manifest:
            layers = image.get("Layers") if isinstance(image, dict) else None
            if not isinstance(layers, list) or not all(isinstance(x, str) for x in layers):
                raise ImageReadError("manifest.json entry has malformed Layers")
            state: dict[str, int] = {}
            for name in layers:
                if name not in records:
                    records[name] = _read_named_layer(archive, name)
                _apply(state, records[name])
            found.update(state.items())
    return sorted(found)


def _read_named_layer(archive: tarfile.TarFile, name: str) -> _Layer:
    try:
        blob = archive.extractfile(name)
    except KeyError:
        blob = None
    if blob is None:
        raise ImageReadError(f"layer {name} is missing from the archive")
    try:
        with tarfile.open(fileobj=blob, mode="r:*") as layer:
            return _read_layer(layer)
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ImageReadError(f"layer {name} is not a plain or gzip tar: {exc}") from exc


def _save(image: str, directory: str) -> Path:
    out = Path(directory) / "image.tar"
    try:
        subprocess.run(["docker", "save", "-o", str(out), image], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ImageReadError(f"`docker save {image}` failed: {exc}") from exc
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("image", nargs="?", help="a local image to `docker save` and scan")
    source.add_argument("--tar", type=Path, help="scan a `docker save` tar instead")
    args = parser.parse_args(argv)
    label = str(args.tar) if args.tar else args.image

    try:
        if args.tar:
            found = find_privileged_files(args.tar)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                found = find_privileged_files(_save(args.image, tmp))
    except ImageReadError as exc:
        print(f"FAIL: {label}: {exc}", file=sys.stderr)
        return 2

    if not found:
        print(f"OK: {label} has no setuid or setgid files")
        return 0
    print(f"FAIL: {label} has {len(found)} setuid or setgid file(s):", file=sys.stderr)
    for path, mode in found:
        print(f"  /{path} ({mode & 0o7777:04o})", file=sys.stderr)
    print(
        f"Add `RUN {STRIP_COMMAND}` to the final stage, after anything that "
        "installs packages and before USER.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
