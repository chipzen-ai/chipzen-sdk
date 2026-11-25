"""Chess checks for ``chipzen validate``. PREVIEW: chess is not live on Chipzen yet.

``chipzen validate`` runs these against a bot whose game is ``chess`` (a
:class:`chipzen.ChessBot` subclass, or any bot with ``--game chess``). It hands
the bot every request in the conformance fixtures (``fixtures/chess/move.json``:
real ``move`` requests recorded from Chipzen's chess engine, White and Black,
from the start, the middle and the end of games, with castling, promotion and
en passant on offer) and judges each answer the way the platform does:

- ``ok``: one of ``state.legal_moves``, in the right shape;
- ``rejected``: anything else, with the code the platform sends in
  ``action_rejected.details.code`` (``bad_shape``, ``not_in_valid_actions``,
  ``bad_param``, ``not_in_legal_set``). In chess a rejected answer is never
  corrected: if no legal move arrives in time, the seat loses on time.

For a :class:`chipzen.ChessBot` the checks call :meth:`~chipzen.ChessBot.choose_move`
directly, so an illegal answer is reported even though the SDK would replace
it with the fallback move during a match.

The judge here checks membership in ``legal_moves`` and the UCI shape only;
like the rest of the SDK it has no chess rules. ``scripts/extract_chess_fixtures.py``
regenerates the fixtures, and the tests hold this judge to every label in them.
"""

from __future__ import annotations

import copy
import functools
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from chipzen.bot import ChipzenBot
from chipzen.chess import (
    MOVE_ACTION,
    ChessState,
    ChessStateParseError,
    is_uci_move,
    parse_chess_turn_request,
)
from chipzen.chess_bot import ChessBot
from chipzen.models import Action

Severity = Literal["pass", "warn", "fail"]

FIXTURE_FILE = Path(__file__).parent / "fixtures" / "chess" / "move.json"

#: Layer 1 cap on a bot message, as sent (TRANSPORT-PROTOCOL section 3.5).
MAX_BOT_MESSAGE_BYTES = 4096


def judge_answer(request: Mapping[str, Any], message: object) -> tuple[str, str | None]:
    """How the platform treats ``message`` (a decoded ``turn_action``) for ``request``.

    Returns ``("ok", None)`` or ``("rejected", <code>)``.
    """
    if not isinstance(message, Mapping):
        return "rejected", "bad_shape"
    if message.get("action") != MOVE_ACTION:
        return "rejected", "not_in_valid_actions"
    params = message.get("params")
    if not isinstance(params, Mapping) or set(params) != {"move"}:
        return "rejected", "bad_shape"
    move = params["move"]
    if not isinstance(move, str):
        return "rejected", "bad_shape"
    if not is_uci_move(move):
        return "rejected", "bad_param"
    state = request.get("state")
    legal = state.get("legal_moves") if isinstance(state, Mapping) else None
    if not isinstance(legal, list) or move not in legal:
        return "rejected", "not_in_legal_set"
    return "ok", None


def judge_turn_action(request: Mapping[str, Any], text: str) -> tuple[str, str | None]:
    """:func:`judge_answer` for a ``turn_action`` as sent: JSON text."""
    if len(text.encode("utf-8")) > MAX_BOT_MESSAGE_BYTES:
        return "rejected", "bad_shape"
    try:
        message = json.loads(text)
    except ValueError:
        return "rejected", "bad_shape"
    return judge_answer(request, message)


@functools.lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
    return data


def load_fixtures() -> dict[str, Any]:
    """The fixture file: ``match_start`` and ``pairs`` (each a ``request`` and its ``answers``)."""
    return copy.deepcopy(_load())


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value) if not isinstance(value, BaseException) else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _answer_of(bot: ChipzenBot, state: ChessState) -> object:
    """The bot's raw answer: ``choose_move`` for a ChessBot, else ``decide``."""
    if isinstance(bot, ChessBot):
        answer: object = bot.choose_move(state)
        if isinstance(answer, str):
            return {"action": MOVE_ACTION, "params": {"move": answer}}
    else:
        answer = bot.decide(state)  # type: ignore[arg-type]
    if isinstance(answer, Action):
        return answer.to_wire()
    return answer


def _snapshot(state: ChessState) -> str:
    return json.dumps(state.to_state(), sort_keys=True)


def run_chess_checks(
    bot: ChipzenBot, *, fixtures: Mapping[str, Any] | None = None
) -> list[tuple[Severity, str, str]]:
    """Answer every fixture request with ``bot`` and judge each answer.

    Returns ``(severity, check_name, message)`` tuples: ``chess_match_start``
    (only when it fails), ``chess_move``, ``chess_state_mutation`` and
    ``chess_timing``. Nothing a handler does escapes, except
    ``KeyboardInterrupt``.
    """
    data = load_fixtures() if fixtures is None else fixtures
    results: list[tuple[Severity, str, str]] = []

    try:
        bot.on_match_start(copy.deepcopy(data["match_start"]))
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - SystemExit included, on purpose
        results.append(("fail", "chess_match_start", f"on_match_start() raised {_short(exc)}"))

    asked = 0
    rejected: list[str] = []
    crashes: list[str] = []
    mutated: list[str] = []
    slow: list[str] = []
    over_budget: list[str] = []
    margin = getattr(bot, "send_margin_ms", 150)
    for pair in data["pairs"]:
        request = pair["request"]
        where = str(request.get("request_id"))
        try:
            state = parse_chess_turn_request(request)
        except ChessStateParseError as exc:  # pragma: no cover - the fixtures all parse (tested)
            results.append(("fail", "chess_move", f"fixture {where} failed to parse: {exc}"))
            continue
        asked += 1
        before = _snapshot(state)
        start = time.perf_counter()
        try:
            answer = _answer_of(bot, state)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - SystemExit included, on purpose
            crashes.append(f"{where}: {type(exc).__name__}: {_short(exc)}")
            continue
        elapsed_ms = (time.perf_counter() - start) * 1000
        if _snapshot(state) != before:
            mutated.append(where)
        if elapsed_ms > state.timeout_ms - margin:
            over_budget.append(f"{where}: {elapsed_ms:.0f}ms")
        elif elapsed_ms > state.clock.window_ms:
            slow.append(f"{where}: {elapsed_ms:.0f}ms")
        verdict, code = judge_answer(request, answer)
        if verdict != "ok":
            rejected.append(f"{where} ({state.side}): {_short(answer, 60)} -> {code}")

    if crashes:
        results.append(
            (
                "fail",
                "chess_move",
                f"{len(crashes)} of {asked} requests raised (the SDK sends the fallback move; "
                f"fix the bug): {'; '.join(crashes[:3])}",
            )
        )
    if rejected:
        results.append(
            (
                "fail",
                "chess_move",
                f"{len(rejected)} of {asked} answers would be rejected (an answer must be one of "
                f"state.legal_moves; a seat with no legal move in time loses on time): "
                f"{'; '.join(rejected[:3])}",
            )
        )
    if not crashes and not rejected:
        results.append(("pass", "chess_move", f"All {asked} answers are moves from legal_moves"))

    if mutated:
        results.append(
            (
                "fail",
                "chess_state_mutation",
                f"the bot changed the state it was handed on {len(mutated)} requests "
                f"({', '.join(mutated[:3])}); copy a list before changing it",
            )
        )
    else:
        results.append(("pass", "chess_state_mutation", "No request's state was changed"))

    if over_budget:
        results.append(
            (
                "fail",
                "chess_timing",
                f"{len(over_budget)} answers took longer than timeout_ms less the send margin, "
                f"so the SDK would send the fallback move: {'; '.join(over_budget[:3])}",
            )
        )
    elif slow:
        results.append(
            (
                "warn",
                "chess_timing",
                f"{len(slow)} answers took longer than the free window, so each spends bank: "
                f"{'; '.join(slow[:3])}",
            )
        )
    else:
        results.append(("pass", "chess_timing", "Every answer came inside the free window"))
    return results


__all__ = ["judge_answer", "judge_turn_action", "load_fixtures", "run_chess_checks"]
