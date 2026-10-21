"""Big Bad Bots checks for ``chipzen validate``: answer shapes and conformance fixtures.

``chipzen validate`` runs these against a bot whose game is ``bigbadbots``
(a :class:`chipzen.BigBadBotsBot` subclass, or any bot with ``--game
bigbadbots``). It hands the bot every request in the conformance fixtures,
real ``turn_request`` messages from recorded matches covering all four calls
(``choose_body``, ``bid``, ``fit`` and ``act``), and judges each answer the way
the server does (protocol section 3.7):

- ``ok``: applied as sent;
- ``corrected``: well-shaped but not legal (a body that is not on the offer, a
  bid outside ``0..me.credits``, a fitting entry that does not fit). The server
  applies the nearest legal answer and logs a violation;
- ``malformed``: not the call's shape. The server rejects it and, unless a
  retry lands in time, applies the call's default.

It also reports a handler that raises (``SystemExit`` and other
``BaseException`` included), one that mutates the state it is handed, and one
that is slow for the per-tick deadline.

The fixtures (``chipzen/fixtures/bigbadbots/<call>.json``) hold wire messages
only: each pairs a request with an example answer, as the Python client would
send it, labelled ``ok``, ``corrected`` or ``malformed`` by the reference
engine's own checks. ``scripts/extract_bigbadbots_fixtures.py`` regenerates
them.

See ``docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md``. Section numbers below
refer to it.
"""

from __future__ import annotations

import collections
import copy
import functools
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from chipzen.bigbadbots import (
    FIGHT_ACTIONS,
    STATE_SHAPE,
    BigBadBotsTurnState,
    StateParseError,
    parse_bigbadbots_turn_request,
)
from chipzen.bot import ChipzenBot
from chipzen.models import Action

Severity = Literal["pass", "warn", "fail"]
Verdict = Literal["ok", "corrected", "malformed"]

#: The four calls, in match order, by ``state.phase``.
CALL_OF_PHASE = {"body_shop": "choose_body", "auction": "bid", "fitting": "fit", "fight": "act"}
CALLS = tuple(CALL_OF_PHASE.values())

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bigbadbots"

#: Layer 1 cap on a bot message, as sent (TRANSPORT-PROTOCOL section 3.5).
MAX_BOT_MESSAGE_BYTES = 4096

#: Every integer on the wire is signed 64-bit (section 4).
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

#: A rule-set id (a body or a part): lower case, digits and ``_``, at most 32
#: characters.
_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")

#: A fitting's slot index: a decimal string without leading zeros. No body has
#: more than :data:`MAX_FIT_ENTRIES` slots, so two digits is the most it takes.
_SLOT = re.compile(r"0|[1-9][0-9]?")
MAX_FIT_ENTRIES = 8

#: The fields a ``turn_action`` may carry (Layer 1 section 9.2).
_ENVELOPE = {"type", "match_id", "request_id", "action", "params"}
_PARAM_KEY = {"choose_body": "body", "bid": "amount", "fit": "fit"}

#: The ``match_start.game_config`` the checks open with: the protocol doc's
#: published, abridged example (section 4.1). A bot must answer legally with
#: only these sections; a real match sends the full table.
GAME_CONFIG: dict[str, Any] = {
    "variant": STATE_SHAPE,
    "rule_set_id": "bigbadbots-1.0",
    "num_players": 2,
    "rule_set": {
        "gearup": {"budget": 1000, "body_offer_size": 4, "auction_lots": 6},
        "arena": {
            "width": 12.0,
            "start_x": [4.0, 8.0],
            "start_jitter": 1.0,
            "min_separation": 1.15,
        },
        "rounds": {"ticks_per_second": 10, "round_ticks": 600, "rounds_to_win": 2, "max_rounds": 5},
        "forfeit": {"grace_seconds": 5, "rounds_for_match": 2},
        "deadline": {
            "deadline_ms": 100,
            "time_bank_ms": 3000,
            "default_action_policy": "repeat_block_or_move",
            "late_answer": "drop",
            "miss_counts_toward_forfeit": True,
        },
    },
}

#: What the server applies when a call gets no acceptable answer (section 3.5).
_DEFAULT_OF_CALL = {
    "choose_body": "the first body on the offer",
    "bid": "a bid of 0",
    "fit": "the empty fitting",
    "act": "idle (or the previous block/move)",
}


# ---------------------------------------------------------------------------
# Judging one answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerCheck:
    """What the server does with one answer.

    Attributes:
        verdict: ``"ok"``, ``"corrected"`` or ``"malformed"``.
        reason: Why, for ``corrected`` and ``malformed``; empty for ``ok``.
    """

    verdict: Verdict
    reason: str = ""


_OK = AnswerCheck("ok")


def _malformed(reason: str) -> AnswerCheck:
    return AnswerCheck("malformed", reason)


def _corrected(reason: str) -> AnswerCheck:
    return AnswerCheck("corrected", reason)


def _short(value: Any, limit: int = 60) -> str:
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - an answer's repr is the bot's code
        text = f"<unprintable {type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def call_of(request: Mapping[str, Any]) -> str:
    """The call a ``turn_request`` asks for, from its ``state.phase``.

    Raises:
        ValueError: the request has no Big Bad Bots phase.
    """
    state = request.get("state")
    phase = state.get("phase") if isinstance(state, Mapping) else None
    if phase not in CALL_OF_PHASE:
        raise ValueError(f"not a Big Bad Bots turn_request (phase {phase!r})")
    return CALL_OF_PHASE[phase]


def answer_text(request: Mapping[str, Any], action: Action) -> str:
    """The ``turn_action`` the Python client sends for ``action``, as text.

    Built exactly as ``chipzen.client`` builds it, so a value that cannot go
    out as JSON fails here the way it would fail there.

    Raises:
        TypeError, ValueError: ``action.params`` cannot be serialised.
    """
    return json.dumps(
        {
            "type": "turn_action",
            "match_id": request.get("match_id"),
            "request_id": request.get("request_id"),
            **action.to_wire(),
        }
    )


def check_answer(request: Mapping[str, Any], answer: object) -> AnswerCheck:
    """Judge what a bot's handler returned for ``request``.

    ``answer`` is the handler's return value. Anything but a
    :class:`chipzen.Action` is malformed: the client cannot send it, and sends
    the call's default instead.
    """
    if not isinstance(answer, Action):
        return _malformed(f"returned {_short(answer)}, not an Action")
    try:
        text = answer_text(request, answer)
    except (TypeError, ValueError) as exc:
        return _malformed(f"{_short(answer.to_wire())} cannot be sent as JSON ({exc})")
    return check_turn_action(request, text)


def _strict_json(text: str) -> Any:
    def _reject(constant: str) -> Any:
        raise ValueError(f"{constant} is not JSON")

    return json.loads(text, parse_constant=_reject)


def check_turn_action(request: Mapping[str, Any], text: str) -> AnswerCheck:
    """Judge a ``turn_action`` exactly as sent (its JSON text) against ``request``."""
    size = len(text.encode("utf-8"))
    if size > MAX_BOT_MESSAGE_BYTES:
        return _malformed(
            f"the message is {size} bytes; a bot message is at most {MAX_BOT_MESSAGE_BYTES} "
            "and a longer one closes the connection (4008)"
        )
    try:
        message = _strict_json(text)
    except ValueError as exc:
        return _malformed(f"the message is not valid JSON ({exc}); NaN and Infinity are not JSON")
    if not isinstance(message, dict):
        return _malformed("the message is not a JSON object")
    extra = sorted(set(message) - _ENVELOPE)
    if extra:
        return _malformed(f"unexpected top-level keys {extra}")
    if message.get("type") != "turn_action":
        return _malformed(f"type is {_short(message.get('type'))}, not 'turn_action'")
    if message.get("request_id") != request.get("request_id"):
        return _malformed("request_id does not echo the request's")

    call = call_of(request)
    state = request["state"]
    action = message.get("action")
    valid = request.get("valid_actions") or ([call] if call != "act" else list(FIGHT_ACTIONS))
    if not isinstance(action, str) or action not in valid:
        return _malformed(f"action {_short(action)} is not one of {valid}")

    if "params" not in message:
        if call == "act":
            return _OK
        return _malformed(f"{call} needs params {{{_PARAM_KEY[call]!r}: ...}}")
    params = message["params"]
    if not isinstance(params, dict):
        return _malformed(f"params is {_short(params)}, not an object")
    if call == "act":
        return _malformed(f"act takes no params, got {_short(params)}") if params else _OK

    key = _PARAM_KEY[call]
    if set(params) != {key}:
        return _malformed(
            f"{call} params must be exactly {{{key!r}: ...}}, got keys {sorted(params)}"
        )
    value = params[key]
    if call == "choose_body":
        return _check_body(state, value)
    if call == "bid":
        return _check_bid(state, value)
    return _check_fit(state, value)


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _check_body(state: Mapping[str, Any], body: Any) -> AnswerCheck:
    if not _is_id(body):
        return _malformed(f"body {_short(body)} is not a body id")
    offer = [b.get("id") for b in state.get("offer", []) if isinstance(b, Mapping)]
    if body not in offer:
        return _corrected(f"body {body!r} is not on your offer {offer}; the cheapest one is bought")
    return _OK


def _check_bid(state: Mapping[str, Any], amount: Any) -> AnswerCheck:
    if isinstance(amount, bool) or not isinstance(amount, int):
        if isinstance(amount, float) and not math.isfinite(amount):
            return _malformed(f"bid {amount!r} is not finite")
        return _malformed(f"bid {_short(amount)} is not a JSON integer")
    if not INT64_MIN <= amount <= INT64_MAX:
        return _malformed(f"bid {_short(amount)} is outside the signed 64-bit range")
    me = state.get("me")
    credits = me.get("credits") if isinstance(me, Mapping) else None
    if isinstance(credits, int) and not 0 <= amount <= credits:
        clamped = min(max(amount, 0), credits)
        return _corrected(f"bid {amount} is outside 0..{credits}; it is clamped to {clamped}")
    return _OK


def _check_fit(state: Mapping[str, Any], fit: Any) -> AnswerCheck:
    if not isinstance(fit, dict):
        return _malformed(f"fit is {_short(fit)}, not an object of slot index to part id")
    if len(fit) > MAX_FIT_ENTRIES:
        return _malformed(f"fit has {len(fit)} entries; at most {MAX_FIT_ENTRIES}")
    for slot, part in fit.items():
        if _SLOT.fullmatch(slot) is None:
            return _malformed(f"fit slot {slot!r} is not a slot index ('0', '1', ...)")
        if not _is_id(part):
            return _malformed(f"fit slot {slot} holds {_short(part)}, not a part id")

    slots = list(state.get("slots", []))
    owned = [p for p in state.get("owned", []) if isinstance(p, Mapping)]
    slot_of_part = {p.get("id"): p.get("slot") for p in owned}
    left = collections.Counter(p.get("id") for p in owned)
    dropped: list[str] = []
    for slot, part in fit.items():
        index = int(slot)
        if index >= len(slots):
            dropped.append(f"slot {slot} does not exist")
        elif left[part] <= 0:
            dropped.append(f"{part!r} is not a part you own (or is already fitted)")
        elif slot_of_part.get(part) != slots[index]:
            dropped.append(
                f"{part!r} is a {slot_of_part.get(part)} part, slot {slot} is {slots[index]}"
            )
        else:
            left[part] -= 1
    if dropped:
        return _corrected("fit entries dropped: " + "; ".join(dropped))
    return _OK


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _load(call: str) -> tuple[dict[str, Any], ...]:
    data = json.loads((FIXTURE_DIR / f"{call}.json").read_text(encoding="utf-8"))
    return tuple(data["pairs"])


def load_fixtures(call: str | None = None) -> list[dict[str, Any]]:
    """The conformance fixtures: ``{"source", "request", "answer", "expect"}`` pairs.

    ``request`` is a ``turn_request``; ``answer`` is a ``turn_action`` as sent
    (its JSON text, which may not be valid JSON); ``expect`` is ``"ok"``,
    ``"corrected"`` or ``"malformed"``. ``call`` picks one call's fixtures;
    the default is all four, in match order. Returns fresh copies.
    """
    calls = CALLS if call is None else (call,)
    return [copy.deepcopy(pair) for c in calls for pair in _load(c)]


# ---------------------------------------------------------------------------
# Running a bot through the fixtures
# ---------------------------------------------------------------------------


def _snapshot(state: BigBadBotsTurnState) -> str:
    return json.dumps([state.valid_actions, state.to_state()], sort_keys=True, default=repr)


@dataclass
class _CallReport:
    asked: int = 0
    crashes: list[str] | None = None
    malformed: list[str] | None = None
    corrected: list[str] | None = None
    slowest_ms: float = 0.0
    over_deadline: int = 0
    over_timeout: int = 0

    def add(self, kind: str, text: str) -> None:
        items = getattr(self, kind)
        if items is None:
            items = []
            setattr(self, kind, items)
        items.append(text)


def _examples(items: list[str]) -> str:
    shown = "; ".join(items[:2])
    return shown + (f"; and {len(items) - 2} more" if len(items) > 2 else "")


def run_bigbadbots_checks(
    bot: ChipzenBot,
    *,
    fixtures: list[dict[str, Any]] | None = None,
) -> list[tuple[Severity, str, str]]:
    """Answer every fixture request with ``bot`` and judge each answer.

    The bot first gets a ``match_start`` carrying :data:`GAME_CONFIG`, then
    each request in match order, through ``bot.decide`` (which a
    :class:`chipzen.BigBadBotsBot` routes to ``choose_body``, ``bid``, ``fit``
    or ``act``). Nothing a handler does escapes, except ``KeyboardInterrupt``.

    Returns ``(severity, check_name, message)`` tuples: one
    ``bigbadbots_<call>`` per call, plus ``bigbadbots_state_mutation``,
    ``bigbadbots_timing`` and, when every fight tick crashed,
    ``bigbadbots_crash_every_tick``.
    """
    pairs = load_fixtures() if fixtures is None else fixtures
    results: list[tuple[Severity, str, str]] = []
    deadline_ms = GAME_CONFIG["rule_set"]["deadline"]["deadline_ms"]

    match_start = {
        "type": "match_start",
        "match_id": "fixture-validate",
        "turn_timeout_ms": 1000,
        "game_config": copy.deepcopy(GAME_CONFIG),
    }
    try:
        bot.on_match_start(match_start)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - SystemExit included, on purpose
        results.append(
            ("fail", "bigbadbots_match_start", f"on_match_start() raised {_short(exc, 120)}")
        )

    reports = {call: _CallReport() for call in CALLS}
    mutated: list[str] = []
    exits: list[str] = []
    for pair in pairs:
        request = pair["request"]
        call = call_of(request)
        report = reports[call]
        try:
            state = parse_bigbadbots_turn_request(request)
        except StateParseError as exc:  # pragma: no cover - the fixtures all parse (tested)
            results.append(("fail", f"bigbadbots_{call}", f"fixture failed to parse: {exc}"))
            continue
        report.asked += 1
        before = _snapshot(state)
        where = f"{request.get('request_id')}"
        start = time.perf_counter()
        try:
            answer = bot.decide(state)  # type: ignore[arg-type]
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - SystemExit included, on purpose
            report.add("crashes", f"{where}: {type(exc).__name__}: {_short(exc, 80)}")
            if not isinstance(exc, Exception):
                exits.append(f"{call}() raised {type(exc).__name__}")
            continue
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            report.slowest_ms = max(report.slowest_ms, elapsed_ms)
            if elapsed_ms > request.get("timeout_ms", math.inf):
                report.over_timeout += 1
            elif call == "act" and elapsed_ms > deadline_ms:
                report.over_deadline += 1
        if _snapshot(state) != before:
            mutated.append(f"{call}() at {where}")
        verdict = check_answer(request, answer)
        if verdict.verdict == "malformed":
            report.add("malformed", f"{where}: {verdict.reason}")
        elif verdict.verdict == "corrected":
            report.add("corrected", f"{where}: {verdict.reason}")

    for call, report in reports.items():
        name = f"bigbadbots_{call}"
        if report.asked == 0:
            continue
        if report.crashes:
            results.append(
                (
                    "fail",
                    name,
                    f"{call}() raised on {len(report.crashes)} of {report.asked} requests "
                    f"({_examples(report.crashes)}). The SDK sends the protocol default "
                    f"({_DEFAULT_OF_CALL[call]}) instead.",
                )
            )
        if report.malformed:
            results.append(
                (
                    "fail",
                    name,
                    f"{len(report.malformed)} of {report.asked} answers are malformed "
                    f"({_examples(report.malformed)}). The server rejects them and applies "
                    f"{_DEFAULT_OF_CALL[call]}.",
                )
            )
        if report.corrected:
            results.append(
                (
                    "warn",
                    name,
                    f"{len(report.corrected)} of {report.asked} answers are not legal and get "
                    f"corrected, with a violation logged ({_examples(report.corrected)}).",
                )
            )
        if not (report.crashes or report.malformed or report.corrected):
            results.append(("pass", name, f"{call}() answered all {report.asked} requests legally"))

    act = reports["act"]
    if act.asked and act.crashes and len(act.crashes) == act.asked:
        results.append(
            (
                "fail",
                "bigbadbots_crash_every_tick",
                f"act() raised on every one of {act.asked} fight ticks, so every tick plays the "
                "default and the bot never fights. A bot that stops answering altogether "
                "forfeits a round after rule_set.forfeit.grace_seconds of missed ticks and "
                "the match after rule_set.forfeit.rounds_for_match rounds (protocol 3.9).",
            )
        )
    if exits:
        results.append(
            (
                "fail",
                "bigbadbots_exit",
                f"{_examples(sorted(set(exits)))}. Outside validate that is not caught: it ends "
                "the bot's session, and a seat that stops answering forfeits (protocol 3.9).",
            )
        )

    if mutated:
        results.append(
            (
                "warn",
                "bigbadbots_state_mutation",
                f"the bot changed the state it was handed ({_examples(mutated)}). The server "
                "never sees it, but your own later reads do: copy what you need to change.",
            )
        )
    else:
        results.append(("pass", "bigbadbots_state_mutation", "no handler changed its state"))

    slow = [(c, r) for c, r in reports.items() if r.over_timeout]
    if slow:
        results.append(
            (
                "fail",
                "bigbadbots_timing",
                "; ".join(
                    f"{c}() ran past the request's timeout_ms {r.over_timeout} times "
                    f"(slowest {r.slowest_ms:.0f}ms)"
                    for c, r in slow
                )
                + ". A late answer is dropped and the call's default applies.",
            )
        )
    elif act.over_deadline:
        results.append(
            (
                "warn",
                "bigbadbots_timing",
                f"act() took over the {deadline_ms}ms per-tick deadline {act.over_deadline} times "
                f"(slowest {act.slowest_ms:.0f}ms). Each one spends the round's time bank.",
            )
        )
    else:
        slowest = max(r.slowest_ms for r in reports.values())
        results.append(
            ("pass", "bigbadbots_timing", f"every call answered in time (slowest {slowest:.1f}ms)")
        )
    return results


__all__ = [
    "AnswerCheck",
    "CALLS",
    "GAME_CONFIG",
    "answer_text",
    "call_of",
    "check_answer",
    "check_turn_action",
    "load_fixtures",
    "run_bigbadbots_checks",
]
