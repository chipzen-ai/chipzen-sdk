"""Tests for the bundled IP-protected Python starter.

These verify the shipped scaffold (`packages/python/starters/python/`)
stays valid as the SDK evolves — `pip install chipzen-bot` is no good
to anyone if the canonical starter project regresses.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import importlib.util
from pathlib import Path

import pytest

from chipzen.validate import validate_bot

STARTER_DIR = Path(__file__).parent.parent / "starters" / "python"


def test_starter_directory_exists():
    """The shipped Python starter is present where docs claim it is."""
    assert STARTER_DIR.is_dir(), f"Starter dir missing: {STARTER_DIR}"


def test_starter_has_required_files():
    """bot.py + requirements.txt + Dockerfile + .dockerignore + README."""
    expected = {"bot.py", "requirements.txt", "Dockerfile", ".dockerignore", "README.md"}
    actual = {p.name for p in STARTER_DIR.iterdir() if p.is_file()}
    missing = expected - actual
    assert not missing, f"Starter missing required files: {missing}"


def test_starter_bot_imports_and_has_main():
    """bot.py is valid Python, defines MyBot(Bot), and exposes main()."""
    spec = importlib.util.spec_from_file_location("_starter_bot_under_test", STARTER_DIR / "bot.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The Dockerfile ENTRYPOINT calls `from bot import main; main()` —
    # both symbols must exist after the .so is built.
    assert callable(mod.main), "bot.main must be callable"

    from chipzen.bot import ChipzenBot

    assert hasattr(mod, "MyBot"), "bot.MyBot must exist for the scaffold to be useful"
    assert issubclass(mod.MyBot, ChipzenBot), "MyBot must subclass chipzen.Bot"


def test_starter_passes_validate():
    """`chipzen-sdk validate` against the starter dir reports no failures.

    This is the same check we tell users to run before docker-build.
    If the starter itself doesn't pass validate, the docs are lying.
    """
    results = validate_bot(STARTER_DIR)
    failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
    assert failures == [], f"Validation failures on shipped starter: {failures}"


def test_starter_passes_check_connectivity():
    """`validate --check-connectivity` against the starter passes too."""
    results = validate_bot(STARTER_DIR, check_connectivity=True)
    failures = [(name, msg) for sev, name, msg in results if sev == "fail"]
    assert failures == [], f"Connectivity-check failures on shipped starter: {failures}"

    # And the new check should actually have run (not been skipped).
    names = {name for _sev, name, _msg in results}
    assert "connectivity_full_match" in names, (
        "connectivity_full_match scenario was not executed against the starter"
    )


def test_dockerfile_compiles_then_strips_source():
    """The Dockerfile invariant: bot.py is removed in stage 1 after cythonize.

    Catches the failure mode where someone reorders the RUN to leave
    bot.py present in stage 1 (which would still be fine for stage 2's
    COPY --from=builder /build/*.so but defeats the IP-protection
    intent because the source is reachable from anything else mounting
    that builder layer).
    """
    text = (STARTER_DIR / "Dockerfile").read_text()
    # Builder stage must remove bot.py after cythonize.
    assert "cythonize -i bot.py && rm bot.py" in text, (
        "Dockerfile must remove bot.py in the same RUN that compiles it; "
        "leaving the source in the builder layer defeats the IP-protection."
    )
    # Stage 2 must copy only .so files from the builder.
    assert "COPY --from=builder /build/*.so /bot/" in text, (
        "Dockerfile must copy only the compiled .so files into the runtime "
        "stage — pulling /build/*.py would leak the source we just stripped."
    )


def test_dockerfile_runs_as_non_root():
    """uid 10001 + USER directive — defense in depth alongside platform sandbox."""
    text = (STARTER_DIR / "Dockerfile").read_text()
    assert "USER 10001" in text, "Dockerfile must end with USER 10001"
    assert "groupadd" in text and "useradd" in text, (
        "Dockerfile must create the bot user/group before USER 10001"
    )
