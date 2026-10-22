"""Per-match cap on bot log volume (#5198).

A Big Bad Bots (``bigbadbots``) bot is asked for an action on every tick, so a
bot that logs once per decision writes about 780 lines in a typical match and
up to 3000 in the longest one ``bigbadbots-1.0`` allows (5 rounds x 600 ticks),
against a few dozen in a poker match. Every line lands in CloudWatch (a cost)
and in the logs the owner reads back. This module bounds that.

How the cap behaves
-------------------
:class:`LogCap` sits in a stream of bot output and passes it through until the
match's output reaches ``max_lines`` lines or ``max_bytes`` bytes, whichever
comes first. It then emits ONE marker line (it starts with
:data:`LOG_CAP_MARKER_PREFIX`) and drops everything after it. Nothing is
buffered: a dropped chunk is gone. The cut lands on a line boundary when the
line cap is hit, and mid-line only when one line alone would pass the byte cap.

Which matches are capped
------------------------
Only the game types in :data:`CAPPED_GAME_TYPES` (``bigbadbots``). Poker is
deliberately left alone: a normal poker bot writes far less than the cap, but
the poker parsed-decisions view (``chipzen.bots.log_parser``) joins a bot's
``DECIDE`` lines by position over the whole match, and a verbose poker bot can
legitimately write thousands of them in a 1000-hand match. Capping poker would
silently change that view, so poker keeps today's behaviour (only the 2 MiB
disk guard of the local file store).

The limits are runtime settings read from the environment at call time
(``CHIPZEN_BOT_LOG_CAP_LINES`` / ``CHIPZEN_BOT_LOG_CAP_BYTES``, SSM-backed on
staging/prod, see ``infra/terraform/ssm.tf``), not ``Settings`` fields, for
the reason given in :mod:`chipzen.runtime_flags` (``config.py`` is at its
file-size ratchet baseline). A missing or bad value falls back to the default;
``0`` turns the cap off.

Where the cap is applied
------------------------
* **Local dev / CI** (``FileBotLogStore``): the ``docker logs --follow`` tee
  runs the bot's output through a :class:`LogCap`, so the per-match file holds
  at most the cap plus the marker.
* **Staging / prod** (Fargate): bot stdout goes from the container straight to
  CloudWatch through the ``awslogs`` log driver. No platform process sits in
  that path and the driver has no volume cap, so the API cannot bound it from
  outside. The bound has to sit inside the task: :func:`main` is a stdlib-only
  wrapper (``python log_cap.py -- <bot command>``) that a platform base image
  can use as its ``ENTRYPOINT``. The Fargate runner passes the limits to every
  capped seat as :data:`CONTAINER_LINES_ENV` / :data:`CONTAINER_BYTES_ENV`, so
  the wrapper enforces the same numbers as the rest of the platform. The B3 bot
  base images (``docker/bigbadbots-bot-base/``, #5106) use it as their
  ENTRYPOINT.

This file imports nothing from ``chipzen`` on purpose, so it can be copied into
a bot base image as a single script.

Hardening the wrapper against the bot it runs (#6046)
-----------------------------------------------------
The bot is a child of the wrapper with the same uid, so two quiet bypasses are
closed here and in the base images:

* The bases run the wrapper as ``python -I``, so it ignores ``PYTHONPATH`` and
  the other ``PYTHON*`` variables and a bot's ``/bot/subprocess.py`` cannot
  shadow a stdlib module the wrapper imports. The child bot still inherits the
  environment, so ``PYTHONPATH=/bot`` keeps working for the bot.
* Before it spawns the bot, :func:`main` makes the wrapper non-dumpable
  (:func:`make_non_dumpable`, Linux ``prctl(PR_SET_DUMPABLE, 0)``). Its
  ``/proc/<pid>`` entries then belong to root, so the bot cannot open the
  wrapper's own stdout through ``/proc/<ppid>/fd/1`` and write past the cap.
  The flag does not pass to the bot: ``execve`` of the bot command resets it.

The in-image cap stays advisory: a builder controls every layer of the image.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "BIGBADBOTS_GAME_TYPE",
    "CAPPED_GAME_TYPES",
    "CONTAINER_BYTES_ENV",
    "CONTAINER_LINES_ENV",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "LOG_CAP_MARKER_PREFIX",
    "MAX_BYTES_ENV_VAR",
    "MAX_LINES_ENV_VAR",
    "LogCap",
    "LogCapLimits",
    "container_env",
    "ecs_environment",
    "limits_for_game",
    "main",
    "make_non_dumpable",
]

#: The Big Bad Bots game id. Spelled here, not imported: ``chipzen.bots`` may
#: not import a game plugin (``core-no-poker`` in ``.importlinter``), and this
#: file must stay importable on its own inside a bot image.
BIGBADBOTS_GAME_TYPE = "bigbadbots"

#: Game types whose matches get the per-match log cap. Poker is not listed (see
#: the module docstring).
CAPPED_GAME_TYPES: frozenset[str] = frozenset({BIGBADBOTS_GAME_TYPE})

#: One line per tick of the longest ``bigbadbots-1.0`` match (5 rounds x 600
#: ticks), so a bot that logs one line per decision is never cut by the line
#: cap. A typical match is about 780 ticks.
DEFAULT_MAX_LINES = 3000

#: 256 KiB: about 87 bytes a line at the line cap, and roughly three times what
#: a one-line-per-decision bot writes in a typical match (780 lines of ~100
#: bytes). Bounds a match at 512 KiB of bot output across both seats.
DEFAULT_MAX_BYTES = 256 * 1024

#: Platform-side settings (API / celery-worker env, SSM-backed).
MAX_LINES_ENV_VAR = "CHIPZEN_BOT_LOG_CAP_LINES"
MAX_BYTES_ENV_VAR = "CHIPZEN_BOT_LOG_CAP_BYTES"

#: What the platform hands a capped bot container, read by :func:`main`.
CONTAINER_LINES_ENV = "CHIPZEN_LOG_CAP_LINES"
CONTAINER_BYTES_ENV = "CHIPZEN_LOG_CAP_BYTES"

#: Every marker line starts with this. The logs viewer
#: (``frontend/src/pages/BotLogs.tsx``, ``LOG_CAP_MARKER_PREFIX``) matches on
#: it to show the marker plainly, so change both together.
LOG_CAP_MARKER_PREFIX = "[chipzen] log capped:"


@dataclass(frozen=True)
class LogCapLimits:
    """Per-match limits. ``0`` in a field means that dimension is unlimited."""

    max_lines: int
    max_bytes: int

    @property
    def enabled(self) -> bool:
        return self.max_lines > 0 or self.max_bytes > 0


def _read_limit(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def limits_for_game(
    game_type: str | None,
    environ: Mapping[str, str] | None = None,
) -> LogCapLimits | None:
    """The cap for a match of ``game_type``, or ``None`` when it is not capped."""
    if game_type not in CAPPED_GAME_TYPES:
        return None
    env = os.environ if environ is None else environ
    limits = LogCapLimits(
        max_lines=_read_limit(env, MAX_LINES_ENV_VAR, DEFAULT_MAX_LINES),
        max_bytes=_read_limit(env, MAX_BYTES_ENV_VAR, DEFAULT_MAX_BYTES),
    )
    return limits if limits.enabled else None


def container_env(limits: LogCapLimits | None) -> dict[str, str]:
    """Environment for a bot container so its in-image wrapper applies ``limits``."""
    if limits is None:
        return {}
    return {
        CONTAINER_LINES_ENV: str(limits.max_lines),
        CONTAINER_BYTES_ENV: str(limits.max_bytes),
    }


def ecs_environment(env: Mapping[str, str]) -> list[dict[str, str]]:
    """``env`` as ECS ``containerOverrides`` environment entries."""
    return [{"name": name, "value": value} for name, value in env.items()]


def _format_size(max_bytes: int) -> str:
    if max_bytes % 1024 == 0:
        return f"{max_bytes // 1024} KB"
    return f"{max_bytes} bytes"


class LogCap:
    """Pass bot output through until the per-match limit, then one marker, then nothing."""

    def __init__(self, limits: LogCapLimits) -> None:
        self.limits = limits
        self.lines = 0
        self.bytes = 0
        self.capped = False
        self._at_line_start = True

    def marker(self) -> bytes:
        parts = []
        if self.limits.max_lines:
            parts.append(f"{self.limits.max_lines} lines")
        if self.limits.max_bytes:
            parts.append(_format_size(self.limits.max_bytes))
        text = (
            f"{LOG_CAP_MARKER_PREFIX} this bot passed the per-match log limit "
            f"({' / '.join(parts)}). The rest of its output for this match was dropped.\n"
        )
        return text.encode("utf-8")

    def feed(self, chunk: bytes) -> bytes:
        """Return the part of ``chunk`` to keep (plus the marker, once)."""
        if self.capped or not chunk:
            return b""
        max_lines = self.limits.max_lines
        max_bytes = self.limits.max_bytes
        keep_end = len(chunk)
        cut = False

        if max_lines:
            remaining_lines = max_lines - self.lines
            if remaining_lines <= 0:
                keep_end = 0
                cut = True
            else:
                # Index just past the newline that ends line number ``max_lines``.
                pos = -1
                for _ in range(remaining_lines):
                    pos = chunk.find(b"\n", pos + 1)
                    if pos == -1:
                        break
                if pos != -1 and pos + 1 < len(chunk):
                    keep_end = pos + 1
                    cut = True

        if max_bytes and self.bytes + keep_end > max_bytes:
            keep_end = max(0, max_bytes - self.bytes)
            cut = True

        kept = chunk[:keep_end]
        self.bytes += len(kept)
        self.lines += kept.count(b"\n")
        if kept:
            self._at_line_start = kept.endswith(b"\n")
        if not cut:
            return kept
        self.capped = True
        prefix = b"" if self._at_line_start else b"\n"
        return kept + prefix + self.marker()


# ---------------------------------------------------------------------------
# In-image wrapper: ``python log_cap.py -- <bot command> [args...]``
# ---------------------------------------------------------------------------

#: ``PR_SET_DUMPABLE`` from ``<linux/prctl.h>``.
PR_SET_DUMPABLE = 4


def make_non_dumpable() -> bool:
    """Clear this process's "dumpable" flag on Linux, so a same-uid child cannot reach it (#6046).

    A non-dumpable process's ``/proc/<pid>`` entries belong to root, and opening
    ``/proc/<pid>/fd/N`` needs ptrace-level access, which the uid-10001 bot does
    not have. Reading the child's pipe and forwarding signals to it are not
    affected. Returns ``True`` when the flag was cleared. Elsewhere (not Linux)
    it does nothing. A failure is written to stderr and is not fatal: the cap
    still runs, only this extra guard is missing.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes  # noqa: PLC0415 - Linux only, and only in the capped path

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0:
            return True
        detail = f"errno {ctypes.get_errno()}"
    except (OSError, AttributeError, ImportError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    sys.stderr.write(
        f"[chipzen] log cap: could not make the wrapper non-dumpable ({detail}); "
        "the cap still applies.\n"
    )
    sys.stderr.flush()
    return False


def main(argv: list[str] | None = None) -> int:
    """Run the bot command with its stdout+stderr capped per the container env.

    With no limits in the environment (a poker match, or a launch that predates
    this) the bot command replaces this process, so nothing about it changes.
    Otherwise the bot runs as a child whose merged output is copied to our
    stdout through a :class:`LogCap`. Past the cap the child's output is still
    read, so the bot never blocks on a full pipe, and thrown away. SIGTERM and
    SIGINT are forwarded to the child and its exit code is ours. Before the
    child starts, this process is made non-dumpable (:func:`make_non_dumpable`).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        sys.stderr.write("usage: log_cap.py -- <command> [args...]\n")
        return 2

    limits = LogCapLimits(
        max_lines=_read_limit(os.environ, CONTAINER_LINES_ENV, 0),
        max_bytes=_read_limit(os.environ, CONTAINER_BYTES_ENV, 0),
    )
    if not limits.enabled:
        os.execvp(args[0], args)  # noqa: S606 - the image's own bot command

    make_non_dumpable()
    child = subprocess.Popen(  # noqa: S603 - the image's own bot command
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    def _forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _forward)
        except (ValueError, OSError):  # pragma: no cover - not main thread / platform
            pass

    cap = LogCap(limits)
    out = sys.stdout.buffer
    if child.stdout is None:  # pragma: no cover - stdout=PIPE above
        return child.wait()
    while True:
        chunk = child.stdout.read(4096)
        if not chunk:
            break
        kept = cap.feed(chunk)
        if kept:
            out.write(kept)
            out.flush()
    return child.wait()


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess in tests
    sys.exit(main())
