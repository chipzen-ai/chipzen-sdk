"""Starter images ship no setuid or setgid files (chipzen-ai/Chipzen#7830).

Two halves:

* every starter Dockerfile clears the bits in its final stage, before USER;
* ``scripts/check_image_setuid.py``, which CI runs on each built starter
  image, finds such files the way the platform's review does (final
  filesystem after whiteouts, opaque directories and hard links).
"""

import gzip
import importlib.util
import io
import json
import re
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_image_setuid.py"
_spec = importlib.util.spec_from_file_location("check_image_setuid", SCRIPT)
check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

STRIP = "find / -xdev -perm /6000 -type f -exec chmod a-s {} +"

# ---------------------------------------------------------------------------
# The Dockerfiles
# ---------------------------------------------------------------------------

STARTER_DOCKERFILES = sorted(REPO_ROOT.glob("packages/*/starters/**/Dockerfile"))


def _final_stage(text: str) -> list[str]:
    """The final stage's instructions, one string each, continuations joined."""
    joined = re.sub(r"\\\n", " ", text)
    lines = [line.strip() for line in joined.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    last_from = max(i for i, line in enumerate(lines) if line.upper().startswith("FROM "))
    return lines[last_from:]


def test_every_starter_dockerfile_is_found():
    # Python (poker + 3 Big Bad Bots), JavaScript (poker + 1), Rust: a glob
    # that matched nothing would pass every check below vacuously.
    assert len(STARTER_DOCKERFILES) >= 7, STARTER_DOCKERFILES


@pytest.mark.parametrize(
    "dockerfile", STARTER_DOCKERFILES, ids=lambda p: p.relative_to(REPO_ROOT).parent.as_posix()
)
def test_final_stage_strips_setuid_bits_before_user(dockerfile):
    stage = _final_stage(dockerfile.read_text(encoding="utf-8"))
    strip_at = [i for i, line in enumerate(stage) if line.startswith("RUN ") and STRIP in line]
    assert strip_at, f"{dockerfile}: the final stage must run `{STRIP}`"
    users = [i for i, line in enumerate(stage) if line.upper().startswith("USER ")]
    assert users and strip_at[-1] < users[-1], "strip the bits while still root, before USER"
    installs = [
        i for i, line in enumerate(stage) if line.startswith("RUN ") and "apt-get install" in line
    ]
    assert all(i <= strip_at[-1] for i in installs), "strip after every package install"


def test_rust_scaffold_template_strips_too():
    # `chipzen-sdk init` (Rust) writes its own copy of the starter Dockerfile;
    # the Rust tests keep the two byte-identical, this keeps the fix in both.
    template = (REPO_ROOT / "packages/rust/chipzen-sdk/src/scaffold.rs").read_text(encoding="utf-8")
    assert STRIP in template


# ---------------------------------------------------------------------------
# The image check
# ---------------------------------------------------------------------------


def _entry(name, *, mode=0o755, kind="file", link=None):
    return {"name": name, "mode": mode, "kind": kind, "link": link}


def _layer_bytes(entries, *, compress=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for e in entries:
            info = tarfile.TarInfo(e["name"])
            info.mode = e["mode"]
            data = b""
            if e["kind"] == "dir":
                info.type = tarfile.DIRTYPE
            elif e["kind"] == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = e["link"]
            elif e["kind"] == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = e["link"]
            else:
                data = b"x"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data else None)
    raw = buf.getvalue()
    return gzip.compress(raw) if compress else raw


def _image(tmp_path, layers, *, oci=False, compress=False, raw_layers=None):
    """Write a `docker save`-shaped tar with one image made of ``layers``."""
    blobs = raw_layers or [_layer_bytes(entries, compress=compress) for entries in layers]
    names = [f"blobs/sha256/{i:064d}" if oci else f"{i:064d}/layer.tar" for i in range(len(blobs))]
    manifest = [{"Config": "config.json", "RepoTags": ["t:ci"], "Layers": names}]
    out = tmp_path / "image.tar"
    with tarfile.open(out, mode="w") as tar:
        for name, blob in [
            ("manifest.json", json.dumps(manifest).encode()),
            *zip(names, blobs),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
    return out


DEBIAN_LIKE = [
    _entry("usr/", kind="dir"),
    _entry("usr/bin/", kind="dir"),
    _entry("usr/bin/su", mode=0o4755),
    _entry("usr/bin/chage", mode=0o2755),
    _entry("usr/bin/ls"),
    _entry("var/mail/", mode=0o2775, kind="dir"),  # setgid directory: ignored
]


@pytest.mark.parametrize("oci", [False, True])
@pytest.mark.parametrize("compress", [False, True])
def test_debian_like_base_is_flagged(tmp_path, oci, compress):
    tar = _image(tmp_path, [DEBIAN_LIKE], oci=oci, compress=compress)
    assert check.find_privileged_files(tar) == [("usr/bin/chage", 0o2755), ("usr/bin/su", 0o4755)]


def test_chmod_in_a_later_layer_clears_the_bits(tmp_path):
    fixed = [_entry("usr/bin/su", mode=0o755), _entry("usr/bin/chage", mode=0o755)]
    tar = _image(tmp_path, [DEBIAN_LIKE, fixed])
    assert check.find_privileged_files(tar) == []


def test_setuid_and_setgid_both(tmp_path):
    tar = _image(tmp_path, [[_entry("bin/both", mode=0o6755)]])
    assert check.find_privileged_files(tar) == [("bin/both", 0o6755)]


def test_whiteout_deletes_the_file(tmp_path):
    tar = _image(tmp_path, [DEBIAN_LIKE, [_entry("usr/bin/.wh.su"), _entry("usr/bin/.wh.chage")]])
    assert check.find_privileged_files(tar) == []


def test_whiteout_of_a_directory_deletes_what_is_under_it(tmp_path):
    tar = _image(tmp_path, [DEBIAN_LIKE, [_entry("usr/.wh.bin")]])
    assert check.find_privileged_files(tar) == []


def test_opaque_directory_hides_lower_layers_but_not_its_own(tmp_path):
    upper = [_entry("usr/bin/.wh..wh..opq"), _entry("usr/bin/new", mode=0o4755)]
    tar = _image(tmp_path, [DEBIAN_LIKE, upper])
    assert check.find_privileged_files(tar) == [("usr/bin/new", 0o4755)]


def test_a_file_over_a_directory_hides_what_was_under_it(tmp_path):
    tar = _image(tmp_path, [DEBIAN_LIKE, [_entry("usr/bin", mode=0o644)]])
    assert check.find_privileged_files(tar) == []


def test_hard_link_to_a_setuid_file_counts(tmp_path):
    layer = [
        _entry("usr/bin/su", mode=0o4755),
        _entry("opt/su2", mode=0o755, kind="hardlink", link="usr/bin/su"),
    ]
    tar = _image(tmp_path, [layer])
    assert [p for p, _ in check.find_privileged_files(tar)] == ["opt/su2", "usr/bin/su"]


def test_symlink_to_a_setuid_file_does_not_count_twice(tmp_path):
    layer = [
        _entry("usr/bin/su", mode=0o4755),
        _entry("bin/su", mode=0o777, kind="symlink", link="/usr/bin/su"),
    ]
    tar = _image(tmp_path, [layer])
    assert check.find_privileged_files(tar) == [("usr/bin/su", 0o4755)]


def test_dot_slash_names_are_the_same_path(tmp_path):
    tar = _image(tmp_path, [[_entry("./usr/bin/su", mode=0o4755)], [_entry("usr/bin/su")]])
    assert check.find_privileged_files(tar) == []


def test_unreadable_layer_fails_closed(tmp_path):
    tar = _image(tmp_path, [], raw_layers=[b"\x28\xb5\x2f\xfd" + b"\x00" * 600])  # zstd magic
    with pytest.raises(check.ImageReadError):
        check.find_privileged_files(tar)


def test_not_docker_save_output_fails(tmp_path):
    out = tmp_path / "x.tar"
    with tarfile.open(out, mode="w") as tar:
        info = tarfile.TarInfo("hello")
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(check.ImageReadError, match="manifest.json"):
        check.find_privileged_files(out)


def test_cli_exit_codes_and_fix_hint(tmp_path, capsys):
    dirty = _image(tmp_path, [DEBIAN_LIKE])
    assert check.main(["--tar", str(dirty)]) == 1
    err = capsys.readouterr().err
    assert "/usr/bin/su (4755)" in err and STRIP in err

    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    clean = _image(clean_dir, [[_entry("bin/ok")]])
    assert check.main(["--tar", str(clean)]) == 0
    assert "no setuid or setgid files" in capsys.readouterr().out

    assert check.main(["--tar", str(tmp_path / "missing.tar")]) == 2
