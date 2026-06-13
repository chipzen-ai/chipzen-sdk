"""Environment-aware connection helper for the external-API lobby.

Devs running an external-API bot against Chipzen shouldn't need to
remember the exact lobby WebSocket URL per environment. This module
exposes :func:`connect_to_chipzen` — a small one-liner that returns a
fully-populated :class:`ConnectionConfig` containing the resolved
``url``, ``token``, and ``retry_policy``, ready to hand off to
:func:`chipzen.client.run_bot`.

External-API issue breakdown reference:
``management/ophir-track/external-api-issue-breakdown.md`` (Issue 24,
chipzen-ai/chipzen-sdk#43). Depends on Issue 23
(chipzen-ai/chipzen-sdk#42) for the ``chipzen.toml`` discovery /
precedence pattern this helper extends.

Environment → URL mapping
-------------------------

::

    prod    -> wss://chipzen.ai/ws/external/bot/{bot_id}
    staging -> wss://staging.chipzen.ai/ws/external/bot/{bot_id}
    local   -> ws://localhost:8001/ws/external/bot/{bot_id}

The path ``/ws/external/bot/{bot_id}`` is the external-API lobby
endpoint landed by Chipzen Issue 8 (``chipzen-ai/Chipzen#2211``).

Precedence rules
----------------

Resolution order for the final WebSocket URL, highest priority first:

1. ``[external_api].url`` from a discovered ``chipzen.toml``. A
   config-file URL ALWAYS wins — it's the most explicit, user-managed
   override and matches the precedence pattern Issue 23 chose for the
   ``token`` field. Devs who want a config file to override everything
   else don't need to remember a different ordering per field.
2. An **explicitly passed** ``env`` argument (anything other than the
   sentinel ``None``). ``env="prod"`` / ``env="staging"`` / ``env="local"``
   all count as explicit — the function signature uses ``None`` as the
   "not specified" sentinel so the helper can tell "caller said prod"
   apart from "caller didn't say anything and got the default".
3. The ``CHIPZEN_ENV`` environment variable, if set to a recognized
   value. This is the typical CI / shell-export pattern (export once
   per terminal, every bot in that shell talks to the same env).
4. The function's default of ``"prod"``.

A `chipzen.toml` URL ALWAYS short-circuits steps 2-4 — once the dev has
written one down, the env switching is by definition irrelevant.

Why this precedence is correct
------------------------------

Two competing intuitions exist for this kind of helper:

A. *Explicit arg wins over everything, including config file.*
B. *Config file wins over env-derived defaults, but explicit arg still
   wins over env var (current choice).*

Issue 23 picked (B) for ``token`` resolution: the config-file value
beats the env-derived default, but an explicit kwarg beats both. We
follow the same rule here. A dev who really wants to override their own
config file can do so by either (a) editing the config file, or
(b) using :func:`chipzen.client.run_bot` directly with an explicit
``url=`` kwarg (which IS specified in Issue 23 to win over the
config-file URL). This helper is the "happy path" for env switching;
the override valve already exists at the ``run_bot`` layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from chipzen.config import (
    ChipzenConfig,
    load_chipzen_config,
    resolve_token,
    resolve_url,
)
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

# Recognized environment names. Kept as a module-level constant (rather
# than just relying on ``Literal[...]``) so error messages can list the
# valid values without resorting to ``typing.get_args(Literal[...])``
# and so the env-var path can validate strings at runtime against the
# same canonical list.
EnvName = Literal["prod", "staging", "local"]
ENV_NAMES: tuple[str, ...] = ("prod", "staging", "local")

# Canonical lobby-URL templates per env. The ``{bot_id}`` placeholder is
# substituted at resolve time; we don't pre-build them so a typo in
# ``bot_id`` produces an obvious malformed URL rather than a partial
# template leaking through.
_ENV_URL_TEMPLATES: dict[str, str] = {
    "prod": "wss://chipzen.ai/ws/external/bot/{bot_id}",
    "staging": "wss://staging.chipzen.ai/ws/external/bot/{bot_id}",
    "local": "ws://localhost:8001/ws/external/bot/{bot_id}",
}

# Name of the environment variable consulted when ``env=`` is not
# explicitly passed. Kept as a constant so tests can monkeypatch the
# string in one place if it ever needs to change.
ENV_VAR_NAME = "CHIPZEN_ENV"


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Fully-resolved connection parameters ready for :func:`run_bot`.

    Returned by :func:`connect_to_chipzen`. Holds everything
    :func:`chipzen.client.run_bot` needs to open a session against the
    external-API lobby endpoint.

    Attributes:
        url: WebSocket URL the bot should connect to. Either env-derived
            from the ``env`` argument / ``CHIPZEN_ENV`` env var, or the
            verbatim ``[external_api].url`` from a discovered
            ``chipzen.toml``.
        token: Long-lived API token from a discovered ``chipzen.toml``,
            or ``None`` if no config-file token is available. ``None``
            here doesn't mean "auth-less"; it just means the helper
            didn't find one. The caller may pass an explicit ``token=``
            to :func:`run_bot` to supply one separately.
        retry_policy: :class:`RetryPolicy` controlling reconnect pacing.
            Defaults to :data:`chipzen.retry.DEFAULT_RETRY_POLICY` (5
            attempts, exponential backoff capped at the server's 30s
            grace window). Devs on flaky networks can pass a custom
            policy via the ``retry_policy=`` kwarg.
        env: The resolved environment name (``"prod"`` / ``"staging"`` /
            ``"local"``), or ``None`` if the URL was supplied verbatim
            via a config file and no env mapping applied. Mostly useful
            for logs / debug output.
        config: The :class:`ChipzenConfig` that was discovered during
            resolution (if any). ``None`` when no ``chipzen.toml`` was
            found on the search path. Exposed so callers can pass it
            through to :func:`run_bot` as ``config=`` and avoid a second
            filesystem stat.
    """

    url: str
    token: str | None
    retry_policy: RetryPolicy
    env: str | None
    config: ChipzenConfig | None


def _resolve_env_name(
    explicit_env: EnvName | None,
    *,
    env_var_value: str | None,
) -> EnvName:
    """Pick the env name to use, applying explicit-arg-then-env-var rules.

    Args:
        explicit_env: The ``env`` argument passed to
            :func:`connect_to_chipzen`. ``None`` means "not specified"
            (the helper uses ``None`` as a sentinel rather than the
            literal string ``"prod"`` so it can distinguish a deliberate
            ``env="prod"`` from "no arg given").
        env_var_value: The current value of ``$CHIPZEN_ENV`` (or
            ``None`` if unset / empty). Passed in rather than read
            inline so tests can control the value without mutating
            ``os.environ``.

    Returns:
        One of the canonical env names.

    Raises:
        ValueError: If an explicit ``env`` argument or the env-var value
            isn't a recognized name. The error message lists the legal
            values so a typo (``env="prd"``) surfaces immediately.
    """
    if explicit_env is not None:
        if explicit_env not in ENV_NAMES:
            raise ValueError(f"Unknown env {explicit_env!r}. Valid values: {', '.join(ENV_NAMES)}.")
        return explicit_env

    # Env-var path. Empty string is treated as "not set" so an
    # accidental ``CHIPZEN_ENV=`` (shell-export with no value) falls
    # through to the default rather than tripping the unknown-env
    # error.
    if env_var_value:
        if env_var_value not in ENV_NAMES:
            raise ValueError(
                f"{ENV_VAR_NAME}={env_var_value!r} is not a recognized "
                f"environment. Valid values: {', '.join(ENV_NAMES)}."
            )
        # mypy can't narrow a runtime ``in ENV_NAMES`` check into the
        # Literal type, so cast explicitly. The check above guarantees
        # the value is one of the three legal strings.
        return env_var_value  # type: ignore[return-value]

    return "prod"


def _url_for_env(env: EnvName, *, bot_id: str) -> str:
    """Format the canonical lobby URL for a given env + bot id.

    Kept private because callers should go through
    :func:`connect_to_chipzen`. Surfaced separately so the resolver and
    tests can share a single source of truth for the templates.
    """
    return _ENV_URL_TEMPLATES[env].format(bot_id=bot_id)


def connect_to_chipzen(
    bot_id: str,
    env: EnvName | None = None,
    *,
    retry_policy: RetryPolicy | None = None,
    config: ChipzenConfig | None = None,
) -> ConnectionConfig:
    """Resolve a :class:`ConnectionConfig` for the external-API lobby.

    Maps ``env`` to a canonical lobby URL and combines it with whatever
    config-file token / URL / retry policy the dev has set up.

    Args:
        bot_id: External-API bot UUID issued by the Chipzen platform.
            Required and must be non-empty — the lobby endpoint
            ``/ws/external/bot/{bot_id}`` won't accept a partial URL.
        env: Target environment. One of ``"prod"`` / ``"staging"`` /
            ``"local"``. ``None`` (the default) means "look at
            ``$CHIPZEN_ENV`` first, then fall back to ``"prod"``". Pass
            an explicit value to override the env-var path.

            Why ``None`` rather than ``"prod"`` for the default? So the
            helper can tell ``connect_to_chipzen(bot_id, env="prod")``
            (caller said prod) apart from
            ``connect_to_chipzen(bot_id)`` (caller didn't say anything,
            let ``$CHIPZEN_ENV`` decide). Both resolve to prod when the
            env var is unset, but the env-var path is only consulted in
            the second case.
        retry_policy: Optional override for the reconnect-pacing policy.
            Defaults to :data:`chipzen.retry.DEFAULT_RETRY_POLICY`.
        config: Pre-loaded :class:`ChipzenConfig` to avoid a second
            filesystem stat when the caller has already discovered it.
            ``None`` (the default) triggers discovery via
            :func:`chipzen.config.load_chipzen_config`.

    Returns:
        A fully populated :class:`ConnectionConfig`.

    Raises:
        ValueError: If ``bot_id`` is empty / missing, ``env`` is not a
            recognized string, or ``$CHIPZEN_ENV`` is set to an
            unrecognized value.
        chipzen.config.ChipzenConfigError: Propagated from
            :func:`chipzen.config.load_chipzen_config` if a
            ``chipzen.toml`` is found on the search path but is
            malformed or missing the ``[external_api]`` section. We
            don't swallow this — same rationale as Issue 23: silent
            fallback would mask typos.

    Example:
        Most callers don't need this helper directly — :func:`run_external_bot`
        resolves the connection for you. Use it when you want the resolved
        ``url`` / ``token`` before connecting::

            from chipzen import connect_to_chipzen, run_external_bot, Bot

            class MyBot(Bot):
                ...

            cfg = connect_to_chipzen(bot_id="<your-bot-uuid>", env="staging")
            await run_external_bot(
                MyBot(),
                url=cfg.url,
                token=cfg.token,
                retry_policy=cfg.retry_policy,
                config=cfg.config,
            )

        Or letting ``$CHIPZEN_ENV`` decide::

            # CHIPZEN_ENV=staging in your shell
            cfg = connect_to_chipzen(bot_id="<your-bot-uuid>")
            # cfg.url == "wss://staging.chipzen.ai/ws/external/bot/<your-bot-uuid>"
    """
    # ``bot_id`` is positional + required for a reason — every env's URL
    # template embeds it. A missing / empty value would silently produce
    # ``/ws/external/bot/`` which the lobby would reject. Raise early
    # with a clear pointer so the failure mode is obvious.
    if not bot_id or not isinstance(bot_id, str):
        raise ValueError(
            "connect_to_chipzen() requires a non-empty bot_id string. "
            "Pass the external-API bot UUID issued by the Chipzen "
            "platform, e.g. connect_to_chipzen(bot_id='abc123', env='prod')."
        )

    # Resolve the env name first. This happens BEFORE config-file URL
    # resolution because even when the config file overrides the
    # env-derived URL, we still want to validate that an explicit
    # ``env=`` arg or ``$CHIPZEN_ENV`` value is a legal string. Silently
    # ignoring a typo there would re-introduce the "your config file
    # accidentally hid a bug" footgun.
    resolved_env = _resolve_env_name(
        env,
        env_var_value=os.environ.get(ENV_VAR_NAME),
    )

    env_derived_url = _url_for_env(resolved_env, bot_id=bot_id)

    # Load the config file (if any) so the config-file URL can override
    # the env-derived one. We do the discovery exactly once and pass
    # the parsed object through on the return value so callers don't
    # have to repeat the filesystem stat from inside ``run_bot``.
    if config is None:
        config = load_chipzen_config()

    final_url = resolve_url(
        explicit_url=None,  # the helper itself does not take a url= override
        config=config,
    )
    if final_url is None:
        final_url = env_derived_url
        # Resolved from env mapping; surface the env name on the return
        # value so callers can log / report which env they're talking
        # to.
        env_for_return: str | None = resolved_env
    else:
        # Config file supplied a verbatim URL; the env name is not
        # meaningful in this case (we don't know which env that URL
        # points at without parsing it, which we deliberately don't
        # do — the URL is whatever the dev put there).
        env_for_return = None

    token = resolve_token(explicit_token=None, config=config)

    policy = retry_policy if retry_policy is not None else DEFAULT_RETRY_POLICY

    return ConnectionConfig(
        url=final_url,
        token=token,
        retry_policy=policy,
        env=env_for_return,
        config=config,
    )
