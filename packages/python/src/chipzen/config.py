"""``chipzen.toml`` discovery and parsing for the SDK.

Devs running an external-API bot should be able to drop their long-lived
API token into a config file once and forget about it, instead of
hard-coding ``token="cz_extbot_..."`` into source. This module implements
the discovery + parsing half of that convention; the
:func:`chipzen.client.run_bot` entry point consumes the result and
prefers explicit kwargs over config-file values.

External-API issue breakdown reference:
``management/ophir-track/external-api-issue-breakdown.md`` (Issue 23,
chipzen-ai/chipzen-sdk#42).

Discovery
---------

Search order, first match wins:

1. ``./chipzen.toml`` (current working directory)
2. ``~/.chipzen/chipzen.toml`` (user-home config)
3. ``/etc/chipzen/chipzen.toml`` (system config, POSIX only — silently
   skipped on Windows where ``/etc`` does not exist)

If no file is found, :func:`load_chipzen_config` returns ``None`` and the
caller falls back to whatever explicit arguments were passed. A clear
error is only raised when a file IS found but is malformed or missing
the expected section.

File format
-----------

::

    [external_api]
    token  = "cz_extbot_<32-char-base62-random>"
    url    = "wss://chipzen.ai/ws/external/bot/<bot_id>"  # optional
    bot_id = "<bot-uuid>"                                 # optional

All three fields are optional. ``url`` (when set) overrides the
env-aware lobby URL helper (External-API Issue 24). ``bot_id`` is the
external-API bot UUID; it's consumed by the ``chipzen run-external``
CLI wrapper (External-API Issue 25) to build the env-derived URL when
no explicit ``url`` is configured.

Precedence rules
----------------

For each field consumed by :func:`run_bot`:

- An **explicit kwarg** (``token="..."`` / ``url="..."``) always wins.
- Otherwise, the value from ``chipzen.toml`` is used.
- If neither is present and the field is required (e.g. token for an
  external-API endpoint), :func:`run_bot` raises a clear ``ValueError``
  pointing the dev at the config-file convention.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# stdlib ``tomllib`` is 3.11+. For 3.10 we fall back to the third-party
# ``tomli`` package (same API; tomllib is literally vendored from tomli).
# We don't list ``tomli`` as a hard dep — bots on 3.10 that want
# config-file support install it themselves, and the missing-import path
# raises a clear error instead of a cryptic ``ModuleNotFoundError``.
if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover - covered indirectly by 3.10 CI run
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ImportError:  # pragma: no cover
        _toml = None  # type: ignore[assignment]


CONFIG_FILENAME = "chipzen.toml"
SECTION_NAME = "external_api"


@dataclass(frozen=True, slots=True)
class ChipzenConfig:
    """Parsed contents of a ``chipzen.toml`` file.

    Attributes:
        path: Filesystem path the config was loaded from. Useful for
            error messages so the dev knows which file was picked up
            when multiple are present on the search path.
        token: Value of ``[external_api] token`` if present, else
            ``None``.
        url: Value of ``[external_api] url`` if present, else ``None``.
        bot_id: Value of ``[external_api] bot_id`` if present, else
            ``None``. Consumed by the ``chipzen run-external`` CLI
            wrapper (External-API Issue 25) to build the env-derived
            lobby URL via :func:`chipzen.connect.connect_to_chipzen`
            when no explicit ``url`` override is set. Not used at the
            :func:`chipzen.client.run_bot` layer (which only needs the
            already-fully-formed URL).
    """

    path: Path
    token: str | None = None
    url: str | None = None
    bot_id: str | None = None


class ChipzenConfigError(ValueError):
    """Raised when a ``chipzen.toml`` is found but cannot be used.

    Subclasses ``ValueError`` so existing ``except ValueError`` blocks
    keep catching configuration errors, and so the SDK's existing error
    surface stays consistent (``RetryPolicy`` validation also raises
    ``ValueError``).
    """


def _search_paths() -> list[Path]:
    """Return the ordered list of candidate config-file locations.

    Order:

    1. ``./chipzen.toml`` in the current working directory.
    2. ``~/.chipzen/chipzen.toml`` (user home).
    3. ``/etc/chipzen/chipzen.toml`` — POSIX only; on Windows this entry
       is omitted because ``/etc`` is not a meaningful path. (We don't
       try to invent a Windows-equivalent system path here; the home
       dir entry is enough for the typical Windows dev workflow.)
    """
    paths: list[Path] = [
        Path.cwd() / CONFIG_FILENAME,
        Path.home() / ".chipzen" / CONFIG_FILENAME,
    ]
    if os.name == "posix":
        paths.append(Path("/etc/chipzen") / CONFIG_FILENAME)
    return paths


def discover_config_path(search_paths: list[Path] | None = None) -> Path | None:
    """Return the first existing ``chipzen.toml`` on the search path, or ``None``.

    Args:
        search_paths: Override the default search order (mostly useful
            for tests). When ``None``, uses :func:`_search_paths`.

    Returns:
        The path of the first matching file, or ``None`` if nothing on
        the search path exists.
    """
    candidates = search_paths if search_paths is not None else _search_paths()
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            # Path objects on Windows can raise on some malformed paths
            # (e.g. embedded null bytes). Treat as "not found" rather
            # than letting an OSError leak out of discovery.
            continue
    return None


def _ensure_toml_parser_available() -> None:
    """Raise a helpful error if no TOML parser is importable.

    On 3.11+, stdlib ``tomllib`` is always available. On 3.10, the user
    needs the optional ``tomli`` dependency installed.
    """
    if _toml is None:
        raise ChipzenConfigError(
            "Reading chipzen.toml on Python 3.10 requires the 'tomli' package. "
            "Install it with: pip install tomli (or upgrade to Python 3.11+ "
            "where TOML parsing is in the stdlib)."
        )


def load_chipzen_config(
    search_paths: list[Path] | None = None,
) -> ChipzenConfig | None:
    """Discover and parse a ``chipzen.toml`` from the search path.

    Args:
        search_paths: Override the default search order. When ``None``,
            uses cwd → ``~/.chipzen/`` → ``/etc/chipzen/`` (POSIX only).

    Returns:
        A :class:`ChipzenConfig` if a file was found and parsed; ``None``
        if no file exists on the search path. The "no file" case is NOT
        an error — the SDK falls back to explicit kwargs in that case.

    Raises:
        ChipzenConfigError: If a file is found but is malformed, lacks
            the ``[external_api]`` section, or has an unparseable token /
            url value. A "found but unusable" file is always a hard
            error — silent fallback would mask typos that would
            otherwise be obvious.
    """
    path = discover_config_path(search_paths)
    if path is None:
        return None

    _ensure_toml_parser_available()

    try:
        with path.open("rb") as fh:
            data = _toml.load(fh)
    except _toml.TOMLDecodeError as exc:  # type: ignore[union-attr]
        raise ChipzenConfigError(
            f"Failed to parse {path}: {exc}. Fix the syntax or delete "
            f"the file to fall back to explicit run_bot(token=...) args."
        ) from exc
    except OSError as exc:
        raise ChipzenConfigError(f"Failed to read {path}: {exc}") from exc

    if SECTION_NAME not in data:
        raise ChipzenConfigError(
            f"{path} has no [{SECTION_NAME}] section. Add one with at least:\n"
            f'\n  [{SECTION_NAME}]\n  token = "cz_extbot_..."\n'
        )

    section = data[SECTION_NAME]
    if not isinstance(section, dict):
        raise ChipzenConfigError(
            f"{path}: [{SECTION_NAME}] must be a table (key=value pairs), "
            f"got {type(section).__name__}."
        )

    token = section.get("token")
    if token is not None and not isinstance(token, str):
        raise ChipzenConfigError(
            f"{path}: [{SECTION_NAME}].token must be a string, got {type(token).__name__}."
        )

    url = section.get("url")
    if url is not None and not isinstance(url, str):
        raise ChipzenConfigError(
            f"{path}: [{SECTION_NAME}].url must be a string, got {type(url).__name__}."
        )

    bot_id = section.get("bot_id")
    if bot_id is not None and not isinstance(bot_id, str):
        raise ChipzenConfigError(
            f"{path}: [{SECTION_NAME}].bot_id must be a string, got {type(bot_id).__name__}."
        )

    return ChipzenConfig(path=path, token=token, url=url, bot_id=bot_id)


def resolve_token(
    *,
    explicit_token: str | None,
    explicit_ticket: str | None = None,
    config: ChipzenConfig | None = None,
) -> str | None:
    """Return the token to use, honoring the precedence rules.

    Precedence:

    1. If ``explicit_token`` is non-``None``, return it. Even an empty
       string wins — the dev was explicit.
    2. If ``explicit_ticket`` is non-``None``, return ``None`` (the
       caller is using ticket-auth and doesn't need a token).
    3. Otherwise, if ``config`` carries a ``token``, return it.
    4. Otherwise, return ``None`` and let the caller decide whether
       that's a hard error (external-API endpoint) or a soft fallback
       (sidecar / localhost dev where empty token is acceptable).

    The function is deliberately split out from :func:`run_bot` so the
    same precedence is testable in isolation and re-usable from the
    forthcoming ``connect_to_chipzen`` / ``chipzen run-external``
    helpers (External-API Issues 24, 25).
    """
    if explicit_token is not None:
        return explicit_token
    if explicit_ticket is not None:
        return None
    if config is not None and config.token is not None:
        return config.token
    return None


def resolve_url(
    *,
    explicit_url: str | None,
    config: ChipzenConfig | None = None,
) -> str | None:
    """Return the URL override to use, honoring the precedence rules.

    1. If ``explicit_url`` is non-``None``, return it.
    2. Otherwise, if ``config`` carries a ``url``, return it.
    3. Otherwise, return ``None`` and let the caller fall back to its
       own default (or raise if a URL is required).
    """
    if explicit_url is not None:
        return explicit_url
    if config is not None and config.url is not None:
        return config.url
    return None
