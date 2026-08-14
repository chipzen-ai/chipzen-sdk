"""The runtime ``__version__`` must never drift from the packaged version.

Issue #89: ``chipzen.__version__`` was a hand-maintained constant. It said
``0.3.0`` while the published dist was ``0.3.1``, so every WS handshake from
that wheel advertised a ``chipzen-sdk-python/0.3.0`` User-Agent and a stale
``client_version``. ``__version__`` is now derived from the installed
distribution metadata, which hatchling fills from ``pyproject`` ``version``.

These tests close the loop from both ends:

* the runtime constant equals the *installed distribution's* version, and
* that version equals the one *declared in pyproject.toml*,

so a bump that touches only one of the two goes red here instead of shipping.
"""

import importlib.metadata
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chipzen  # noqa: E402

# stdlib ``tomllib`` is 3.11+; on 3.10 the dev extra installs ``tomli``
# (identical API), the same fallback ``chipzen.config`` uses.
try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on the 3.10 CI leg
    import tomli as _toml  # type: ignore[no-redef,import-not-found]

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _declared_version() -> str:
    with PYPROJECT.open("rb") as handle:
        return str(_toml.load(handle)["project"]["version"])


def test_version_matches_installed_distribution() -> None:
    """The exported constant IS the installed dist version, not a copy of it."""
    assert chipzen.__version__ == importlib.metadata.version("chipzen-bot")


def test_installed_distribution_matches_pyproject() -> None:
    """A version bump in pyproject reaches the installed dist metadata.

    Run against an editable install (what CI and `make dev` produce) this
    catches a bump that was never reinstalled; run against a wheel it proves
    the wheel carries the version the source tree declares.
    """
    assert importlib.metadata.version("chipzen-bot") == _declared_version()


def test_version_has_no_placeholder() -> None:
    """No ``dev`` suffix and no uninstalled-source-tree sentinel.

    ``0.0.0+unknown`` is the deliberate fallback when the package is imported
    from a source tree that was never installed. Seeing it under test means
    the test environment is not exercising real package metadata, which would
    make the assertions above vacuous.
    """
    assert chipzen.__version__ != "0.0.0+unknown"
    assert "dev" not in chipzen.__version__


@pytest.mark.parametrize("module_name", ["chipzen.client", "chipzen.external"])
def test_default_handshake_version_tracks_the_package(module_name: str) -> None:
    """Both WS entrypoints default ``client_version`` to the same constant.

    They import it as ``_VERSION`` at module load, which is what ends up in
    the ``chipzen-sdk-python/<version>`` User-Agent and the handshake payload.
    """
    module = importlib.import_module(module_name)
    assert module._VERSION == chipzen.__version__
