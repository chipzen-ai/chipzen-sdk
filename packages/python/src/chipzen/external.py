"""External-API remote-play entry point: ``run_external_bot``.

Where :func:`chipzen.client.run_bot` connects a bot to a *known* match URL
(the containerized/upload path — the platform's executor hands the container
its ``/ws/match/{match_id}/{participant_id}`` URL), this module implements the
**external-API remote-play** path: a developer runs their bot on their own
machine, authenticates with a long-lived ``cz_extbot_`` token, and the platform
matches and dispatches them exactly like any other competitor.

The flow (documented in ``docs/EXTERNAL-API-BOT-PROTOCOL.md``)::

    lobby WS  /ws/external/bot/{bot_id}      (token in authenticate frame)
        -> "matched" notify (carries match_id + gateway_ws_url)
        -> per-match gateway WS  /ws/external/match/{mid}/{pid}
                                  (token in Sec-WebSocket-Protocol header)
        -> two-layer bot handshake + game loop to match_end

The **match data plane is identical** to the containerized path, so the game
loop here reuses :func:`chipzen.client._run_session` verbatim — the only
external-API-specific code is the lobby connection and the per-match gateway
handshake. A developer writes ONE :class:`chipzen.Bot` subclass and it works on
both paths.

The lobby is held open for the bot's whole session and each ``matched`` plays
in its own task, so the 15-second lobby heartbeat is answered even while a
multi-minute match is in flight. This is what lets a single connection serve a
whole tournament (the bot is "checked in" via lobby presence and matched once
per round). This mirrors the battle-tested pattern in the platform's
``remote_tournament_harness.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import websockets

from chipzen import __version__ as _VERSION  # noqa: N812
from chipzen.bot import ChipzenBot
from chipzen.client import BotDecisionError, _run_session
from chipzen.config import ChipzenConfig, load_chipzen_config, resolve_token
from chipzen.connect import EnvName, connect_to_chipzen
from chipzen.retry import DEFAULT_RETRY_POLICY, RetryPolicy

logger = logging.getLogger("chipzen.external")

#: Sentinel subprotocol that marks the ``cz_extbot_`` token in the
#: ``Sec-WebSocket-Protocol`` header (CZ issue 2932 moved the token off the
#: query string, where it leaked into proxy access logs). Must match the value
#: the platform's api gateway expects.
BOT_TOKEN_SUBPROTOCOL = "chipzen-bot-token"

#: Max WS frame size we accept (16 MiB) — large round_result / deck_reveal
#: frames can exceed the ``websockets`` 1 MiB default.
_MAX_WS_SIZE = 2**24

#: How long the lobby loop blocks on a single ``recv`` before waking to
#: re-check the stop signal. Short enough that a stop is honored promptly;
#: long enough that the loop isn't a busy-wait.
_LOBBY_RECV_TIMEOUT_S = 2.0

#: On teardown, how long to let still-in-flight matches finish before
#: cancelling them, so nothing is left orphaned. Generous enough that a
#: near-done match reports its result cleanly.
_MATCH_DRAIN_GRACE_S = 5.0


def bot_token_subprotocols(token: str) -> list[str]:
    """Build the ``Sec-WebSocket-Protocol`` offer that carries the bot token.

    Returns ``[sentinel, token]`` — the sentinel marks "the next value is my
    bot token". The api gateway extracts the token from this header (so it
    never appears in any access log / URL) and echoes the sentinel back on
    accept. Pass to ``websockets.connect(..., subprotocols=...)``.
    """
    return [BOT_TOKEN_SUBPROTOCOL, token]


def _normalise_base(url: str) -> str:
    """Strip any path/query from a URL, keeping scheme + netloc only.

    Tolerates a trailing slash, a full lobby URL, or a bare ``host:port``
    without a scheme (defaults to ``wss``). Used to resolve the relative
    ``gateway_ws_url`` path the ``matched`` notify carries against the same
    origin the lobby used.
    """
    parts = urlsplit(url)
    scheme = parts.scheme or "wss"
    netloc = parts.netloc or parts.path  # tolerate "host:port" without scheme
    return urlunsplit((scheme, netloc, "", "", "")).rstrip("/")


def resolve_gateway_url(lobby_url: str, gateway_ws_path: str) -> str:
    """Resolve the ``matched.gateway_ws_url`` path against the lobby origin.

    The ``matched`` notification carries ``gateway_ws_url`` as a *path*
    (``/ws/external/match/{mid}/{pid}``). The ``cz_extbot_`` token is NOT on
    the query string — it travels in the ``Sec-WebSocket-Protocol`` header (see
    :func:`bot_token_subprotocols`).

    A server that returns a full URL is honored ONLY if it stays on the same
    origin as the lobby and does not downgrade ``wss`` → ``ws`` — otherwise the
    bot token would be sent to an attacker- or misconfig-supplied host (or in
    cleartext). A relative path is always re-anchored to the lobby origin, so
    it's inherently same-origin.

    Raises:
        ValueError: if ``gateway_ws_path`` is an absolute URL whose origin
            differs from the lobby's, or downgrades a ``wss`` lobby to ``ws``.
    """
    if gateway_ws_path.startswith(("ws://", "wss://")):
        lobby = urlsplit(_normalise_base(lobby_url))
        gateway = urlsplit(gateway_ws_path)
        downgrade = lobby.scheme == "wss" and gateway.scheme != "wss"
        if gateway.netloc != lobby.netloc or downgrade:
            raise ValueError(
                f"refusing gateway URL {gateway_ws_path!r}: cross-origin or "
                f"insecure relative to lobby {lobby.scheme}://{lobby.netloc} "
                "(the bot token must not be sent to a different host or in cleartext)"
            )
        return gateway_ws_path
    return f"{_normalise_base(lobby_url)}{gateway_ws_path}"


def _loads(raw: str | bytes) -> dict:
    """Parse a WS frame into a dict (``{}`` on non-dict / bad JSON)."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return msg if isinstance(msg, dict) else {}


def _as_factory(
    bot: ChipzenBot | Callable[[], ChipzenBot],
) -> Callable[[], ChipzenBot]:
    """Normalize the ``bot`` argument into a per-match instance factory.

    Accepts either:

    * a :class:`ChipzenBot` **instance** — reused for every match (correct for
      the common case where a bot plays its tournament matches sequentially), or
    * a **callable** returning a fresh :class:`ChipzenBot` (a class, or a
      factory function) — a new instance per match, which is the right choice
      if you expect to be matched into overlapping matches and your bot keeps
      mutable per-match state.

    A ``ChipzenBot`` instance is not callable, so the ``isinstance`` check
    disambiguates cleanly (and passing the class itself "just works").
    """
    if isinstance(bot, ChipzenBot):
        return lambda: bot
    if callable(bot):
        return bot
    raise TypeError(
        "run_external_bot(bot=...) must be a chipzen.Bot instance or a callable "
        f"returning one, got {type(bot).__name__}."
    )


async def _play_one_match(
    gateway_url: str,
    match_id: str,
    token: str,
    bot: ChipzenBot,
    *,
    policy: RetryPolicy,
    client_name: str,
    client_version: str,
    safe_mode: bool,
    user_agent: str,
    supported_games: list[str] | None = None,
) -> dict | None:
    """Play one match end-to-end over the per-match gateway WS, reconnecting
    across a mid-match drop.

    Opens the gateway WS (token in the ``Sec-WebSocket-Protocol`` header) and
    hands off to :func:`chipzen.client._run_session` for the two-layer
    handshake + game loop — the exact same data-plane code the containerized
    path runs.

    If the socket drops before ``match_end``, reconnect (bounded by ``policy``)
    and let the platform's reconnect-resume re-deliver the pending turn:
    ``_run_session`` already consumes the server ``reconnected`` frame and
    replays its ``pending_request``, and the same ``bot`` instance carries its
    state across the gap. ``websockets``' built-in keepalive
    (``ping_interval``/``ping_timeout``) closes a dead or stalled socket, which
    surfaces here as a drop and triggers the same reconnect.

    Returns the ``match_end`` payload, or ``None`` if the match could not be
    completed within the reconnect budget.
    """
    attempt = 0
    while True:
        try:
            logger.info("match %s: connecting gateway", match_id)
            async with websockets.connect(
                gateway_url,
                max_size=_MAX_WS_SIZE,
                subprotocols=bot_token_subprotocols(token),  # type: ignore[arg-type]
                user_agent_header=user_agent,
            ) as ws:
                # The inner leg's token is the gateway's internal JWT
                # (authoritative); the executor ignores the value we send, but
                # the authenticate frame MUST be first. _run_session sends an
                # empty token when both token and ticket are None.
                end = await _run_session(
                    ws,
                    bot,
                    match_id=match_id,
                    token=None,
                    ticket=None,
                    client_name=client_name,
                    client_version=client_version,
                    safe_mode=safe_mode,
                    supported_games=supported_games,
                )
            if end is not None:
                return end  # clean match_end — done
            # _run_session returned None: the socket closed without a match_end
            # (a drop, or keepalive killed a stalled socket). Try to resume.
            reason = "closed without match_end"
        except BotDecisionError:
            raise  # deterministic bot bug — terminal, never reconnect-retry
        except (OSError, websockets.WebSocketException) as exc:
            reason = str(exc) or exc.__class__.__name__

        attempt += 1
        if attempt > policy.max_reconnect_attempts:
            logger.warning(
                "match %s: reconnect budget exhausted (%s); abandoning",
                match_id,
                reason,
            )
            return None
        delay = policy.backoff_ms(attempt) / 1000.0
        logger.info(
            "match %s: reconnecting in %.1fs (attempt %d/%d; %s)",
            match_id,
            delay,
            attempt,
            policy.max_reconnect_attempts,
            reason,
        )
        await asyncio.sleep(delay)


async def _run_lobby_once(
    lobby_url: str,
    token: str,
    factory: Callable[[], ChipzenBot],
    results: list[dict],
    match_tasks: list[asyncio.Task],
    completed: list[int],
    *,
    policy: RetryPolicy,
    client_name: str,
    client_version: str,
    safe_mode: bool,
    user_agent: str,
    max_matches: int | None,
    stop: asyncio.Event,
    fatal: list[BaseException],
    supported_games: list[str] | None = None,
) -> str:
    """Hold ONE lobby connection and dispatch every ``matched`` it delivers.

    Returns a status string describing why the session ended:

    * ``"stopped"``   — the caller's stop signal fired, or ``max_matches`` was
      reached (do not reconnect).
    * ``"evicted"``   — the lobby evicted us (a newer connection for the same
      bot replaced this one; do not reconnect).
    * ``"closed"``    — the lobby connection closed unexpectedly (the caller
      may reconnect per the retry policy).

    Each ``matched`` is played in its own task, appended to ``match_tasks``
    (which the CALLER owns) so the lobby heartbeat is answered during matches
    AND in-flight matches survive a lobby reconnect — a match runs on its own
    gateway socket, independent of the lobby. ``completed`` is a 1-element
    mutable counter shared across lobby sessions.
    """

    def _on_match_done(task: asyncio.Task) -> None:
        try:
            end = task.result()
        except asyncio.CancelledError:  # teardown — not a completed match
            return
        except BotDecisionError as exc:
            # safe_mode=False: a deterministic bot bug. Surface it (stop the
            # session and re-raise from run_external_bot) instead of quietly
            # logging — the whole point of safe_mode=False is a loud failure.
            logger.error("bot.decide() error (safe_mode=False): %s", exc)
            fatal.append(exc)
            stop.set()
            return
        except Exception as exc:  # noqa: BLE001 - record, never crash the lobby
            logger.warning("match play task raised: %s", exc)
            results.append({"match_id": None, "reason": "exception", "error": str(exc)})
        else:
            completed[0] += 1
            results.append({"match_id": end.get("match_id") if end else None, "end": end})
        if max_matches is not None and completed[0] >= max_matches:
            stop.set()

    async with websockets.connect(
        lobby_url,
        max_size=_MAX_WS_SIZE,
        user_agent_header=user_agent,
    ) as ws:
        await ws.send(json.dumps({"type": "authenticate", "token": token}))
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_LOBBY_RECV_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue  # periodic wake to re-check the stop signal
            except (websockets.ConnectionClosed, ConnectionError):
                logger.info("lobby: connection closed")
                return "closed"

            msg = _loads(raw)
            mtype = msg.get("type")
            if mtype == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif mtype == "hello":
                logger.info("lobby: connected (endpoint=%s)", msg.get("endpoint", "lobby"))
            elif mtype == "matched":
                try:
                    gateway_url = resolve_gateway_url(lobby_url, msg["gateway_ws_url"])
                except ValueError as exc:
                    # Untrusted gateway URL (cross-origin / downgrade); skip this
                    # match rather than send the token there.
                    logger.warning("lobby: ignoring matched with untrusted gateway URL: %s", exc)
                    continue
                match_id = msg["match_id"]
                logger.info(
                    "lobby: matched -> match %s (rated=%s); playing",
                    match_id,
                    msg.get("rated"),
                )
                task = asyncio.create_task(
                    _play_one_match(
                        gateway_url,
                        match_id,
                        token,
                        factory(),
                        policy=policy,
                        client_name=client_name,
                        client_version=client_version,
                        safe_mode=safe_mode,
                        user_agent=user_agent,
                        supported_games=supported_games,
                    )
                )
                task.add_done_callback(_on_match_done)
                match_tasks.append(task)
            elif mtype == "evict":
                logger.warning("lobby: evicted (replaced by a newer connection)")
                return "evicted"
            else:
                logger.debug("lobby: ignoring frame type=%s", mtype)
    return "stopped" if stop.is_set() else "closed"


async def _drain_and_cancel(
    match_tasks: list[asyncio.Task], *, grace: float = _MATCH_DRAIN_GRACE_S
) -> None:
    """Teardown: let still-in-flight matches finish for a short grace window,
    then cancel any stragglers and await everything so no task is orphaned."""
    pending = [t for t in match_tasks if not t.done()]
    if pending:
        _, still_pending = await asyncio.wait(pending, timeout=grace)
        for task in still_pending:
            task.cancel()
    if match_tasks:
        await asyncio.gather(*match_tasks, return_exceptions=True)


async def run_external_bot(
    bot: ChipzenBot | Callable[[], ChipzenBot],
    *,
    bot_id: str | None = None,
    env: EnvName | None = None,
    url: str | None = None,
    token: str | None = None,
    config: ChipzenConfig | None = None,
    retry_policy: RetryPolicy | None = None,
    client_name: str = "chipzen-sdk-python",
    client_version: str = _VERSION,
    safe_mode: bool = True,
    max_matches: int | None = None,
    user_agent: str | None = None,
    supported_games: list[str] | None = None,
) -> list[dict]:
    """Run a bot on the Chipzen external-API remote-play path.

    Connects to the lobby, then plays every match the platform dispatches to
    this bot (a single challenge, or every round of a tournament) until the
    lobby closes, the bot is evicted, or ``max_matches`` matches complete.

    Args:
        bot: Your :class:`chipzen.Bot` — either an instance (reused across
            matches) or a callable / the class itself (a fresh instance per
            match). See :func:`_as_factory`.
        bot_id: External-API bot UUID issued by the platform. Used to build the
            lobby URL when ``url`` is not given; falls back to
            ``[external_api].bot_id`` in ``chipzen.toml``.
        env: Target environment (``"prod"`` / ``"staging"`` / ``"local"``).
            ``None`` consults ``$CHIPZEN_ENV`` then defaults to ``"prod"``.
        url: Explicit full lobby URL
            (``wss://.../ws/external/bot/{bot_id}``). Overrides ``bot_id`` /
            ``env`` URL derivation when set.
        token: Long-lived ``cz_extbot_`` API token. Falls back to
            ``[external_api].token`` in ``chipzen.toml``. Required.
        config: Pre-loaded :class:`chipzen.config.ChipzenConfig`, to avoid a
            second filesystem stat. ``None`` triggers discovery.
        retry_policy: Reconnect-pacing policy for both lobby drops and mid-match
            gateway drops (a dropped match reconnects and resumes via the
            server's reconnect-resume). Defaults to
            :data:`chipzen.retry.DEFAULT_RETRY_POLICY`.
        client_name / client_version: Sent in the per-match ``hello``
            handshake. ``client_version`` defaults to the installed SDK version.
        safe_mode: When ``True`` (default), an exception in ``bot.decide()`` is
            logged and folded — a transient bug won't forfeit a competitive
            match. Set ``False`` for dev/eval so the first exception propagates
            and exits non-zero (see chipzen-ai/chipzen-sdk#52).
        max_matches: Stop after this many matches complete. ``None`` (default)
            runs until the lobby closes / the bot is evicted — the right
            behavior for a long-running tournament check-in. Pass ``1`` to play
            a single challenge and return.
        user_agent: Override the WS ``User-Agent`` header. Defaults to
            ``chipzen-sdk-python/<version>`` (a non-default UA also clears the
            platform's Cloudflare bot-fight rule; chipzen-ai/chipzen-sdk#46).
        supported_games: The games this client can actually play, declared in
            each per-match ``hello`` as ``supported_games``. ``None`` (the
            default) omits the field, and the platform reads that as "poker
            only" — so the default wire bytes are unchanged. See
            :func:`chipzen.client.run_bot` and
            ``docs/protocol/LAYER2-COMMON.md`` section 2.

    Returns:
        A list of per-match result dicts (``{"match_id", "end"}``), one per
        match played this session.

    Raises:
        ValueError: If no token can be resolved, or neither ``url`` nor a
            ``bot_id`` is available to build the lobby URL.
    """
    if config is None:
        config = load_chipzen_config()

    # --- Resolve lobby URL + token + retry policy --------------------------
    if url is not None:
        lobby_url = url
        policy = retry_policy if retry_policy is not None else DEFAULT_RETRY_POLICY
        resolved_token = resolve_token(explicit_token=token, config=config)
    else:
        resolved_bot_id = bot_id or (config.bot_id if config is not None else None)
        if not resolved_bot_id:
            raise ValueError(
                "run_external_bot() needs a lobby URL. Pass url=..., or bot_id=... "
                "(or set [external_api].bot_id / url in chipzen.toml)."
            )
        conn = connect_to_chipzen(resolved_bot_id, env, retry_policy=retry_policy, config=config)
        lobby_url = conn.url
        policy = conn.retry_policy
        # An explicit token kwarg still wins over the config-file token.
        resolved_token = token if token is not None else conn.token

    if not resolved_token:
        raise ValueError(
            "run_external_bot() requires an external-API token (cz_extbot_...). "
            "Pass token=..., or set [external_api].token in chipzen.toml."
        )

    ua = user_agent or f"chipzen-sdk-python/{client_version}"
    factory = _as_factory(bot)
    results: list[dict] = []
    stop = asyncio.Event()
    fatal: list[BaseException] = []
    # Owned HERE, not by a single lobby session, so in-flight matches survive a
    # lobby reconnect (a match plays on its own gateway socket). ``completed``
    # is a 1-element mutable counter shared across lobby sessions.
    match_tasks: list[asyncio.Task] = []
    completed = [0]

    logger.info("external: connecting lobby %s", lobby_url)

    # --- Lobby session loop with reconnect/backoff -------------------------
    consecutive_failures = 0
    ever_connected = False
    giveup_exc: BaseException | None = None
    while not stop.is_set():
        try:
            status = await _run_lobby_once(
                lobby_url,
                resolved_token,
                factory,
                results,
                match_tasks,
                completed,
                policy=policy,
                client_name=client_name,
                client_version=client_version,
                safe_mode=safe_mode,
                user_agent=ua,
                max_matches=max_matches,
                stop=stop,
                fatal=fatal,
                supported_games=supported_games,
            )
            ever_connected = True
        except (OSError, websockets.WebSocketException) as exc:
            # connect() itself failed — count it as a reconnect attempt.
            consecutive_failures += 1
            if consecutive_failures > policy.max_reconnect_attempts:
                # Only a hard error if we NEVER reached the lobby (bad URL /
                # token / network). If we connected and played, give up quietly
                # and return what we have.
                if not ever_connected:
                    giveup_exc = exc
                break
            delay = policy.backoff_ms(consecutive_failures) / 1000.0
            logger.warning(
                "lobby: connect failed (%s); retrying in %.1fs (attempt %d/%d)",
                exc,
                delay,
                consecutive_failures,
                policy.max_reconnect_attempts,
            )
            await asyncio.sleep(delay)
            match_tasks[:] = [t for t in match_tasks if not t.done()]
            continue

        # A live lobby session ran; reset the backoff counter.
        consecutive_failures = 0
        if status in ("stopped", "evicted") or fatal:
            break
        # status == "closed": the lobby dropped. In-flight matches keep playing
        # on their own sockets; reconnect the lobby per the policy.
        consecutive_failures += 1
        if consecutive_failures > policy.max_reconnect_attempts:
            logger.info("lobby: closed and reconnect budget exhausted; done")
            break
        delay = policy.backoff_ms(consecutive_failures) / 1000.0
        logger.info("lobby: closed; reconnecting in %.1fs", delay)
        await asyncio.sleep(delay)
        match_tasks[:] = [t for t in match_tasks if not t.done()]

    # --- Teardown: never orphan an in-flight match task --------------------
    await _drain_and_cancel(match_tasks)
    if fatal:
        # A bot.decide() error under safe_mode=False — re-raise so the process
        # exits non-zero (matches run_bot's behavior).
        raise fatal[0]
    if giveup_exc is not None:
        raise giveup_exc
    logger.info("external: session ended (%d match(es) played)", len(results))
    return results
