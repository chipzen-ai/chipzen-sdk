"""``chipzen run-external`` CLI wrapper for external-API bots.

Implements the External-API Issue 25 spec
(``management/ophir-track/external-api-issue-breakdown.md`` §25,
chipzen-ai/chipzen-sdk#44). Wraps the boilerplate of "load config,
connect, run bot" so a dev's main loop is just::

    chipzen run-external my_bot.py

The wrapper:

1. Loads token + url + bot_id from a discovered ``chipzen.toml``
   (External-API Issue 23 / chipzen-ai/chipzen-sdk#42).
2. Resolves the env-aware lobby URL via
   :func:`chipzen.connect.connect_to_chipzen` (External-API Issue 24 /
   chipzen-ai/chipzen-sdk#43) when no explicit ``url`` is set in
   ``chipzen.toml``.
3. Dynamically imports the user's bot module from a filesystem path
   using :func:`importlib.util.spec_from_file_location` — the dev
   doesn't have to fight ``PYTHONPATH`` or build a package.
4. Discovers the :class:`chipzen.Bot` subclass defined in that file
   (single subclass auto-selected; multiple subclasses require
   ``--bot-class <name>``).
5. Hands off to :func:`chipzen.client.run_bot` with the resolved
   url + token + retry policy.

Precedence rules (highest priority first), for each setting:

- **token**: ``--token`` arg > ``[external_api].token`` in ``chipzen.toml``.
- **url**:   ``[external_api].url`` in ``chipzen.toml`` > env-derived URL
  built from ``bot_id`` + env via :func:`connect_to_chipzen`. The
  config-file URL wins because that's the precedence pattern Issues
  23 + 24 already established — see ``chipzen.connect`` module
  docstring.
- **env**:   ``--env`` arg > ``$CHIPZEN_ENV`` > ``"prod"``. (Mirrors
  :func:`connect_to_chipzen`'s own resolution chain.)
- **bot_id**: ``--bot-id`` arg > ``[external_api].bot_id`` in
  ``chipzen.toml``. Required only when no explicit URL is configured.

Multiple ``Bot`` subclasses
---------------------------

Decision: when a bot file defines exactly one :class:`chipzen.Bot`
subclass, use it automatically. When more than one is defined, the
user must pick via ``--bot-class <name>``; we don't silently "pick
the first" because Python's class-definition order in a module is
fragile (e.g. helper classes defined below the main one would invert
the meaning of "first"). The error message lists the candidates.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Iterable

from chipzen.bot import ChipzenBot
from chipzen.config import ChipzenConfig, ChipzenConfigError, load_chipzen_config
from chipzen.connect import ENV_NAMES, ConnectionConfig, connect_to_chipzen
from chipzen.external import run_external_bot
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

logger = logging.getLogger("chipzen.run_external")

# Public name used in error messages + the synthetic module created
# during dynamic import. Kept as a module constant so tests can refer
# to it without re-typing.
_SYNTHETIC_MODULE_PREFIX = "chipzen_user_bot_"


# ---------------------------------------------------------------------------
# Dynamic bot-module loading
# ---------------------------------------------------------------------------


def _load_bot_module(bot_file: Path) -> object:
    """Load a Python file as a module without putting it on ``sys.path``.

    Uses ``importlib.util.spec_from_file_location`` so the bot file
    works regardless of cwd / ``PYTHONPATH``. The module is registered
    under a synthetic name in :data:`sys.modules` so repeated calls
    (e.g. in tests) don't trip ``importlib``'s dedupe logic.

    Args:
        bot_file: Filesystem path to the bot file. Must end in ``.py``
            and exist; both conditions are enforced by the caller
            (:func:`run_external_cli`) so a missing file produces a
            clean CLI error rather than a stack trace from this helper.

    Returns:
        The loaded module object.

    Raises:
        FileNotFoundError: If ``bot_file`` does not exist (defensive —
            the CLI checks first, but the helper stays standalone-safe).
        ImportError: If the file cannot be loaded as a Python module
            (syntax error, missing dependency, etc.). The original
            exception is chained for traceback context.
    """
    if not bot_file.is_file():
        raise FileNotFoundError(f"Bot file not found: {bot_file}")

    # The synthetic module name includes the file stem so multiple bots
    # in the same test run get distinct entries in ``sys.modules`` and
    # don't trample each other. The prefix guarantees no collision with
    # real packages.
    module_name = _SYNTHETIC_MODULE_PREFIX + bot_file.stem

    spec = importlib.util.spec_from_file_location(module_name, str(bot_file))
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Could not build an import spec for {bot_file}. "
            "Ensure the file ends in .py and is readable."
        )

    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so the module can reference itself by name
    # (e.g. via ``sys.modules[__name__]`` patterns or by importing
    # submodules). Removal-on-error is intentional to avoid stale
    # half-loaded modules leaking into ``sys.modules``.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ImportError(f"Failed to load {bot_file}: {exc}") from exc
    return module


def _find_bot_subclasses(module: object) -> list[type[ChipzenBot]]:
    """Return all :class:`chipzen.Bot` subclasses defined IN this module.

    ``inspect.getmembers`` returns every name in the module namespace,
    including re-exports (``from chipzen import Bot`` itself surfaces
    ``Bot`` as an attribute, and ``Bot`` is its own subclass for
    ``issubclass`` purposes). We filter on two conditions to avoid
    those false positives:

    1. The candidate must be a strict subclass of :class:`ChipzenBot`
       (i.e. ``cls is not ChipzenBot``). This excludes the base class
       itself, which is a common import in bot files.
    2. The candidate must have been *defined* in this module
       (``cls.__module__`` matches the module's ``__name__``). This
       excludes any other Bot subclasses the user might have imported
       from another package.
    """
    module_name = getattr(module, "__name__", None)
    found: list[type[ChipzenBot]] = []
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if obj is ChipzenBot:
            continue
        if not issubclass(obj, ChipzenBot):
            continue
        # Restrict to classes whose ``__module__`` is this module — the
        # ``inspect.getmembers`` scan would otherwise surface
        # ``chipzen.bot.ChipzenBot`` itself (excluded above) plus any
        # third-party Bot subclasses the file happens to import.
        if module_name is not None and getattr(obj, "__module__", None) != module_name:
            continue
        found.append(obj)
    return found


def _select_bot_class(
    candidates: Iterable[type[ChipzenBot]],
    *,
    bot_class_name: str | None,
    bot_file: Path,
) -> type[ChipzenBot]:
    """Pick a single Bot subclass from the discovered list.

    Args:
        candidates: Subclasses of :class:`ChipzenBot` defined in the
            user's module.
        bot_class_name: Optional explicit class name (from
            ``--bot-class``). When supplied, only the candidate with
            this name is accepted.
        bot_file: Used in error messages.

    Returns:
        The chosen :class:`ChipzenBot` subclass.

    Raises:
        RuntimeError: If no candidates exist, the explicit name doesn't
            match any candidate, or multiple candidates exist without
            an explicit pick. The error message always lists the
            options so the user can immediately fix the invocation.
    """
    cand_list = list(candidates)
    if bot_class_name is not None:
        # Explicit pick: must match exactly one candidate by class name.
        matches = [c for c in cand_list if c.__name__ == bot_class_name]
        if not matches:
            available = ", ".join(c.__name__ for c in cand_list) or "<none>"
            raise RuntimeError(
                f"No Bot subclass named {bot_class_name!r} in {bot_file}. "
                f"Available subclasses: {available}."
            )
        # Class names within a single module must be unique by Python's
        # rules, so a successful match always has length 1.
        return matches[0]

    if not cand_list:
        raise RuntimeError(
            f"No chipzen.Bot subclass found in {bot_file}. Define a class "
            f"that inherits from chipzen.Bot (e.g. class MyBot(Bot): ...)."
        )

    if len(cand_list) > 1:
        names = ", ".join(c.__name__ for c in cand_list)
        raise RuntimeError(
            f"Multiple chipzen.Bot subclasses found in {bot_file}: {names}. "
            f"Pick one with --bot-class <name>."
        )

    return cand_list[0]


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser(prog: str = "chipzen run-external") -> argparse.ArgumentParser:
    """Construct the ``run-external`` argument parser.

    Split out so tests can introspect the parser (e.g. assert that
    ``--env`` has the canonical three choices) without round-tripping
    through ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Run a Chipzen external-API bot from a Python file. Loads "
            "config from chipzen.toml, resolves the env-aware lobby URL, "
            "and plays via the SDK's run_external_bot() entry point."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Minimum (config-driven): token + bot_id in chipzen.toml,\n"
            "  # env defaults to prod.\n"
            "  chipzen run-external my_bot.py\n"
            "\n"
            "  # Override env via flag.\n"
            "  chipzen run-external my_bot.py --env staging\n"
            "\n"
            "  # Override env via $CHIPZEN_ENV.\n"
            "  CHIPZEN_ENV=staging chipzen run-external my_bot.py\n"
            "\n"
            "  # Override token + bot_id on the command line.\n"
            "  chipzen run-external my_bot.py --token cz_extbot_xyz --bot-id "
            "abc123\n"
            "\n"
            "  # Disambiguate when the file defines multiple Bot subclasses.\n"
            "  chipzen run-external my_bot.py --bot-class TightAggressive\n"
        ),
    )

    parser.add_argument(
        "bot_file",
        type=Path,
        help="Path to the Python file containing your Bot subclass.",
    )
    parser.add_argument(
        "--env",
        choices=ENV_NAMES,
        default=None,
        help=(
            "Target environment for the lobby URL. Defaults to $CHIPZEN_ENV "
            "if set, otherwise 'prod'."
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "External-API token (cz_extbot_...). Overrides [external_api].token in chipzen.toml."
        ),
    )
    parser.add_argument(
        "--bot-id",
        dest="bot_id",
        default=None,
        help=(
            "External-API bot UUID. Overrides [external_api].bot_id in "
            "chipzen.toml. Required when no [external_api].url is set."
        ),
    )
    parser.add_argument(
        "--bot-class",
        dest="bot_class",
        default=None,
        help=(
            "Name of the Bot subclass to run when the file defines more "
            "than one. Optional when only one subclass is present."
        ),
    )
    parser.add_argument(
        "--max-matches",
        dest="max_matches",
        type=int,
        default=None,
        help=(
            "Stop after this many matches complete. Default: run until the "
            "lobby closes (tournament check-in). Pass 1 for a single challenge."
        ),
    )
    parser.add_argument(
        "--no-safe-mode",
        dest="safe_mode",
        action="store_false",
        default=True,
        help=(
            "Let an exception in your bot's decide() crash the process (exit "
            "non-zero) instead of folding. Use for local dev / eval so bugs "
            "surface immediately. Default: safe_mode on (errors fold)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Python logging level for the SDK + this CLI. Default: INFO.",
    )
    return parser


# ---------------------------------------------------------------------------
# Connection-config resolution
# ---------------------------------------------------------------------------


def _resolve_connection(
    *,
    config: ChipzenConfig | None,
    explicit_env: str | None,
    explicit_token: str | None,
    explicit_bot_id: str | None,
    retry_policy: RetryPolicy | None = None,
) -> tuple[str, str | None, RetryPolicy, ChipzenConfig | None]:
    """Resolve (url, token, retry_policy, config) for ``run_bot``.

    Logic:

    1. If the discovered config has a verbatim ``url``, use it directly.
       Token comes from ``--token`` arg > config token. No bot_id
       resolution is needed in this branch.
    2. Otherwise, the URL must be env-derived. We need a ``bot_id``
       (from ``--bot-id`` > config) and call
       :func:`connect_to_chipzen` to build the lobby URL.
    3. The token resolution in branch 2 follows the same precedence:
       explicit > config.

    Args:
        config: Discovered ``chipzen.toml`` or ``None``.
        explicit_env: Value of ``--env`` (or ``None`` if not passed —
            the helper will consult ``$CHIPZEN_ENV`` then fall back to
            ``"prod"``).
        explicit_token: Value of ``--token`` (or ``None``).
        explicit_bot_id: Value of ``--bot-id`` (or ``None``).
        retry_policy: Override for the default retry policy. Mostly
            useful for tests; the CLI itself uses the default.

    Returns:
        4-tuple ``(url, token, retry_policy, config)`` ready to pass
        to :func:`chipzen.client.run_bot`.

    Raises:
        RuntimeError: If neither a config URL nor a bot_id is
            available, i.e. there's no way to build a lobby URL.
    """
    policy = retry_policy if retry_policy is not None else DEFAULT_RETRY_POLICY

    # Branch 1: verbatim URL in config wins outright (same precedence
    # rule connect_to_chipzen / run_bot already use).
    config_url = config.url if config is not None else None
    if config_url is not None:
        token = (
            explicit_token
            if explicit_token is not None
            else (config.token if config is not None else None)
        )
        return config_url, token, policy, config

    # Branch 2: env-derived URL. Need a bot_id.
    bot_id = explicit_bot_id or (config.bot_id if config is not None else None)
    if not bot_id:
        raise RuntimeError(
            "No lobby URL is configured. Either:\n"
            "  - Pass --bot-id <id> on the command line, or\n"
            "  - Set [external_api].bot_id in chipzen.toml, or\n"
            "  - Set [external_api].url in chipzen.toml for a verbatim URL."
        )

    # ``connect_to_chipzen`` handles env resolution itself
    # (explicit > $CHIPZEN_ENV > "prod"). We pass through the explicit
    # config object so the helper doesn't re-stat the filesystem.
    conn: ConnectionConfig = connect_to_chipzen(
        bot_id=bot_id,
        env=explicit_env,  # type: ignore[arg-type]
        retry_policy=policy,
        config=config,
    )

    # Explicit ``--token`` still wins over whatever connect_to_chipzen
    # picked up from the config file.
    token = explicit_token if explicit_token is not None else conn.token

    return conn.url, token, conn.retry_policy, conn.config


# ---------------------------------------------------------------------------
# Public CLI entry point
# ---------------------------------------------------------------------------


def run_external_cli(args: list[str] | None = None) -> None:
    """CLI entry point for ``chipzen run-external``.

    Invoked from :func:`chipzen.__main__.main` when the user runs
    ``chipzen run-external <bot_file.py> [...]``.

    Args:
        args: Optional pre-parsed argv list (excluding the program
            name and the ``run-external`` token). When ``None``,
            falls back to ``sys.argv[1:]`` — same convention as
            argparse's default.

    Exits the process via :func:`sys.exit` on error so the shell sees
    a non-zero return code. Successful runs return ``None``.
    """
    parser = _build_parser()
    parsed = parser.parse_args(args)

    # Configure logging early so any error path benefits.
    logging.basicConfig(
        level=getattr(logging, parsed.log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    bot_file: Path = parsed.bot_file
    if not bot_file.is_file():
        logger.error("Bot file not found: %s", bot_file)
        sys.exit(2)

    # Discover ``chipzen.toml`` once up front. The helpers below all
    # accept the resolved config so we never re-stat the filesystem.
    try:
        config = load_chipzen_config()
    except ChipzenConfigError as exc:
        logger.error("Invalid chipzen.toml: %s", exc)
        sys.exit(2)

    # Resolve url + token + retry policy.
    try:
        url, token, policy, config = _resolve_connection(
            config=config,
            explicit_env=parsed.env,
            explicit_token=parsed.token,
            explicit_bot_id=parsed.bot_id,
        )
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        sys.exit(2)

    # Dynamically import the user's bot module + pick a Bot subclass.
    try:
        module = _load_bot_module(bot_file)
    except (FileNotFoundError, ImportError) as exc:
        logger.error("%s", exc)
        sys.exit(2)

    candidates = _find_bot_subclasses(module)
    try:
        bot_cls = _select_bot_class(
            candidates,
            bot_class_name=parsed.bot_class,
            bot_file=bot_file,
        )
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(2)

    # Instantiate. We wrap the construction itself so an error in the
    # user's ``__init__`` doesn't surface as a bare stack trace.
    try:
        bot_instance = bot_cls()
    except Exception as exc:
        logger.error("Failed to instantiate %s: %s", bot_cls.__name__, exc)
        sys.exit(2)

    logger.info(
        "Connecting %s.%s -> %s",
        getattr(module, "__name__", "?"),
        bot_cls.__name__,
        url,
    )

    try:
        asyncio.run(
            run_external_bot(
                bot_instance,
                url=url,
                token=token,
                retry_policy=policy,
                config=config,
                safe_mode=parsed.safe_mode,
                max_matches=parsed.max_matches,
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted; exiting.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - surface a clean non-zero exit
        # Includes BotDecisionError from --no-safe-mode: a bot bug should exit
        # non-zero so a harness / CI sees the failure.
        logger.error("Bot run failed: %s", exc)
        sys.exit(1)
