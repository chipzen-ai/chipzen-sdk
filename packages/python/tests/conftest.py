"""Shared test configuration and fixtures for SDK tests."""

import os
import sys

# Ensure the SDK source is on the path for all tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


import pytest  # noqa: E402

#: A minimal Dockerfile for a test bot folder: what ``validate``'s
#: ``dockerfile`` check reads is the final stage's ENTRYPOINT.
_DOCKERFILE = (
    "FROM python:3.12-slim\n"
    "COPY . /bot\n"
    'ENTRYPOINT ["python", "-c", "from {module} import main; main()"]\n'
)


@pytest.fixture
def make_image_ready():
    """Give a test bot folder what an uploadable image needs (issue #260).

    ``validate`` fails a poker bot folder with no Dockerfile, or whose entry
    point lacks the ``main()`` the image's ENTRYPOINT calls. Tests about other
    checks call this so those two stay green: it appends a top-level
    ``main()`` to the entry file and writes a Dockerfile that calls it.
    """

    def _make(bot_dir, entry="main.py"):
        entry_file = bot_dir / entry
        if entry_file.exists():
            entry_file.write_text(entry_file.read_text() + "\n\ndef main():\n    pass\n")
        module = entry.rsplit(".", 1)[0].rsplit("/", 1)[-1]
        (bot_dir / "Dockerfile").write_text(_DOCKERFILE.format(module=module))
        return bot_dir

    return _make
