"""Registry / directory metadata stays consistent with the package.

``server.json`` is what the release workflow's ``registry-publish`` job hands
to ``mcp-publisher``; a version that disagrees with ``pyproject.toml`` makes
that job wait on a PyPI release that does not exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parents[1]

#: Registry entry name, also the PyPI ownership marker in the README.
REGISTRY_NAME = "io.github.chipzen-ai/chipzen-mcp"


def _pyproject_version() -> str:
    # tomllib is 3.11+ and CI also runs 3.10, so read the one field directly.
    text = (PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match, "no version in pyproject.toml"
    return match.group(1)


def _server_json() -> dict:
    return json.loads((PACKAGE_DIR / "server.json").read_text(encoding="utf-8"))


def test_server_json_version_matches_the_package_version() -> None:
    server = _server_json()
    version = _pyproject_version()
    assert server["version"] == version
    pypi = [p for p in server["packages"] if p["registryType"] == "pypi"]
    assert len(pypi) == 1
    assert pypi[0]["identifier"] == "chipzen-mcp"
    assert pypi[0]["version"] == version


def test_server_json_identity_matches_the_readme_marker() -> None:
    server = _server_json()
    assert server["name"] == REGISTRY_NAME
    readme = (PACKAGE_DIR / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {REGISTRY_NAME}" in readme
    # The registry caps the description at 100 characters.
    assert 0 < len(server["description"]) <= 100


def test_server_json_declares_the_credentials_as_env_vars() -> None:
    (pypi,) = (p for p in _server_json()["packages"] if p["registryType"] == "pypi")
    env = {var["name"]: var for var in pypi["environmentVariables"]}
    assert env["CHIPZEN_EXTBOT_TOKEN"]["isSecret"] is True
    assert env["CHIPZEN_EXTBOT_TOKEN"]["isRequired"] is True
    assert env["CHIPZEN_BOT_ID"]["isRequired"] is True
    assert pypi["transport"] == {"type": "stdio"}


def test_glama_json_names_the_maintainers() -> None:
    glama = json.loads((REPO_ROOT / "glama.json").read_text(encoding="utf-8"))
    assert glama["$schema"] == "https://glama.ai/mcp/schemas/server.json"
    assert glama["maintainers"] == ["Dave-London"]
