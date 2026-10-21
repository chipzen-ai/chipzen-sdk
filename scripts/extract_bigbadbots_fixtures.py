#!/usr/bin/env python3
"""Regenerate the Big Bad Bots conformance fixtures from the golden replay corpus.

The fixtures (``packages/python/src/chipzen/fixtures/bigbadbots/``) are what
``chipzen validate`` feeds a Big Bad Bots bot: real ``turn_request`` messages
paired with example ``turn_action`` answers, each answer labelled with how the
server treats it (``ok``, ``corrected`` or ``malformed``, protocol section 3.7).

The corpus is private: it is the engine's golden match logs, which hold engine
records, not wire messages. This script replays a few of those logs through the
engine, encodes every request the engine makes of a bot exactly as the server
puts it on the wire, and writes out **wire messages only**: the requests, and
the answers the logged bots gave, serialised the way the Python SDK client sends
them. No engine, simulator or bot code, and no log, is copied into this repo.

Run it from a checkout of the private engine repo's environment, pointing at
that checkout (nothing is written there):

    <engine-repo>/.venv/Scripts/python -B scripts/extract_bigbadbots_fixtures.py \\
        --corpus <engine-repo>

Each answer's label comes from the engine's own reference checks, not from the
SDK: an answer that is not strict JSON, is over the 4096-byte Layer 1 cap, or
fails the call's published response schema is ``malformed``; a schema-valid
answer the engine logged a violation for is ``corrected``; anything else is
``ok``. ``tests/test_bigbadbots_fixtures.py`` then holds the SDK's own checker
to those labels.
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sys
from pathlib import Path

SDK_SRC = Path(__file__).resolve().parent.parent / "packages" / "python" / "src"
OUT_DIR = SDK_SRC / "chipzen" / "fixtures" / "bigbadbots"

#: Logs whose every gear-up call (both seats) becomes a fixture: four ordered
#: pairings of the sample bots, and one match with a forced body.
GEARUP_LOGS = (
    "reader-spacer",
    "greedy-random",
    "spacer-greedy-2",
    "random-reader-2",
    "forced-body",
)

#: Logs sampled for fight ticks, one tick per distinct situation (see
#: ``_signature``), so the fixtures cover the masks and fighter states without
#: shipping whole fights.
FIGHT_LOGS = ("reader-spacer", "greedy-random", "random-random-2", "body-mantis", "body-kite")

#: At most this many fight ticks from each of ``FIGHT_LOGS``.
TICKS_PER_LOG = 12

#: The log whose bot answers with every bad shape. Each of its answers that a
#: Python bot can put on the wire becomes a fixture.
BAD_ANSWER_LOG = "mischief"

#: Layer 1 cap on a bot message (TRANSPORT-PROTOCOL section 3.5).
MAX_BOT_MESSAGE_BYTES = 4096

#: ``timeout_ms`` of the fixture requests: the provisional per-tick deadline
#: plus an untouched time bank for ``act``, the illustrative gear-up timeout
#: for the rest (protocol section 3.4).
TIMEOUT_MS = {"act": 3100, "choose_body": 1000, "bid": 1000, "fit": 1000}

SERVER_TS = "2026-09-29T12:00:00.000Z"


def _strict_loads(text: str):
    def _reject(constant: str):
        raise ValueError(f"{constant} is not JSON")

    return json.loads(text, parse_constant=_reject)


def _signature(state: dict) -> tuple:
    me, opp = state["me"], state["opp"]
    return (
        me["phase"],
        opp["phase"],
        bool(me["buffs"] or opp["buffs"]),
        me["special_used"] or opp["special_used"],
        state["facing"],
    )


def _wire_value_is_faithful(recorded) -> bool:
    """Whether a recorded answer decodes to exactly what the bot returned.

    The log keeps a prefix of a long string or list, and only a description of
    an object it cannot carry; those cannot be rebuilt, so they are skipped.
    """
    if isinstance(recorded, list):
        return len(recorded) < 64 and all(_wire_value_is_faithful(x) for x in recorded)
    if isinstance(recorded, dict):
        if any(k in recorded for k in ("$str", "$opaque", "$crash", "$no_answer")):
            return False
        if "$tuple" in recorded:
            return _wire_value_is_faithful(recorded["$tuple"])
        if "$dict" in recorded:
            return all(
                _wire_value_is_faithful(k) and _wire_value_is_faithful(v)
                for k, v in recorded["$dict"]
            )
    return True


def _replay(log: dict, modules):
    """Yield ``(engine request, recorded answer)`` for every bot call of ``log``."""
    Match, ForfeitRule, DeadlineRule, load_rule_set, Recorded = modules
    rules = load_rule_set(log["rule_set_id"])
    force = log["inputs"].get("force_bodies")
    match = Match(
        log["seed"],
        names=tuple(log["bots"]),
        record_frames=True,
        force_bodies=tuple(force) if force else None,
        forfeit_rule=ForfeitRule(**log["forfeit_rule"]) if "forfeit_rule" in log else None,
        rule_set=rules,
        deadline_rule=DeadlineRule(**log["deadline"]) if "deadline" in log else None,
    )
    seats = (Recorded(log, 0), Recorded(log, 1))
    gearup = {
        seat: {
            "choose_body": [log["inputs"]["choose_body"][seat]],
            "bid": list(log["inputs"]["bid"][seat]),
            "fit": [log["inputs"]["fit"][seat]],
        }
        for seat in (0, 1)
    }
    act = {seat: {(r, t): rec for r, t, rec in log["inputs"]["act"][seat]} for seat in (0, 1)}
    while not match.done:
        requests = match.requests()
        for r in requests:
            if r.call == "act":
                key = (r.round, r.tick)
                if key in act[r.seat]:
                    recorded = act[r.seat][key]
                else:
                    recorded = log["rounds"][r.round - 1]["frames"]["input"][r.seat][r.tick]
            else:
                recorded = gearup[r.seat][r.call].pop(0)
            yield r, recorded
        match.submit([seats[r.seat].deliver(r) for r in requests])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", required=True, help="checkout of the private engine repo")
    args = parser.parse_args()

    sys.dont_write_bytecode = True
    corpus = Path(args.corpus).resolve()
    sys.path.insert(0, str(corpus))

    from engine import answers
    from engine.deadline import DeadlineRule
    from engine.match import ForfeitRule, Match
    from engine.replay import _Recorded
    from engine.rules import load_rule_set
    from protocol import validate as reference
    from protocol import wire

    # The SDK's parser module needs only the standard library; load it alone so
    # the engine's environment does not need the SDK's dependencies.
    spec = importlib.util.spec_from_file_location(
        "_sdk_bigbadbots", SDK_SRC / "chipzen" / "bigbadbots.py"
    )
    assert spec is not None and spec.loader is not None
    sdk_models = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sdk_models  # dataclasses look the module up
    spec.loader.exec_module(sdk_models)
    parse_bigbadbots_turn_request = sdk_models.parse_bigbadbots_turn_request

    modules = (Match, ForfeitRule, DeadlineRule, load_rule_set, _Recorded)
    golden = corpus / "engine" / "golden"
    rule_sets = corpus / "engine" / "rulesets"

    pairs: dict[str, list[dict]] = {call: [] for call in TIMEOUT_MS}
    seen_signatures: set[tuple] = set()
    seen_bad: set[str] = set()
    fight_ticks = dict.fromkeys(FIGHT_LOGS, 0)
    counter = {"seq": 0}

    def _request(name: str, r) -> dict:
        counter["seq"] += 1
        message = wire.request(
            r.call,
            r.args,
            match_id=f"fixture-{name}",
            seq=counter["seq"],
            seat=r.seat,
            request_id=f"req_{counter['seq']:05d}",
            timeout_ms=TIMEOUT_MS[r.call],
            server_ts=SERVER_TS,
        )
        problems = reference.errors(f"{r.call}.request", message)
        if problems:
            raise SystemExit(f"{name}: a request fails its reference schema: {problems[:3]}")
        if r.call == "act":
            # The corpus predates engine 1.1.0, which added `walls`. Its rule
            # set has no closing walls, so they always stand at the arena's
            # edges (protocol section 4.3.4).
            message["state"]["walls"] = [0.0, float(r_width[name])]
        parse_bigbadbots_turn_request(message)  # the SDK must read every fixture
        return message

    def _answer(r, message: dict, recorded):
        """The answer's wire text, as the SDK client sends it, or None."""
        if answers.is_crash(recorded) or answers.is_no_answer(recorded):
            return None
        if not _wire_value_is_faithful(recorded):
            return None
        value = answers.decode(recorded)
        if isinstance(value, tuple):
            value = list(value)
        action = wire.turn_action(
            r.call, value, match_id=message["match_id"], request_id=message["request_id"]
        )
        if r.call == "act":
            action["params"] = {}
        try:
            return json.dumps(action)  # chipzen.client._send_json
        except (TypeError, ValueError):
            return None

    def _expect(log: dict, r, text: str) -> str:
        try:
            parsed = _strict_loads(text)
        except ValueError:
            return "malformed"
        if len(text.encode("utf-8")) > MAX_BOT_MESSAGE_BYTES:
            return "malformed"
        if reference.errors(f"{r.call}.response", parsed):
            return "malformed"
        for v in log["violations"][r.seat]:
            if (v["call"], v["lot"], v["round"], v["tick"]) == (r.call, r.lot, r.round, r.tick):
                return "corrected"
        return "ok"

    r_width: dict[str, float] = {}
    names = dict.fromkeys(GEARUP_LOGS + FIGHT_LOGS + (BAD_ANSWER_LOG,))
    for name in names:
        log = json.loads(gzip.decompress((golden / f"{name}.json.gz").read_bytes()))
        table = json.loads((rule_sets / f"{log['rule_set_id']}.json").read_text(encoding="utf8"))
        if "stall_close_after" in table["rounds"]:
            raise SystemExit(f"{name}: closing walls; this script cannot rebuild `walls`")
        r_width[name] = table["arena"]["width"]
        for r, recorded in _replay(log, modules):
            if name == BAD_ANSWER_LOG:
                if r.call == "act" and (r.round, r.tick) not in {
                    (rd, t) for rd, t, _ in log["inputs"]["act"][r.seat]
                }:
                    continue  # a legal tick: the other logs cover those
                if r.seat != 0:
                    continue  # seat 1 is a sample bot
            elif r.call == "act":
                if name not in FIGHT_LOGS:
                    continue
                state = wire.encode_state("act", r.args)
                signature = _signature(state)
                if signature in seen_signatures or fight_ticks[name] >= TICKS_PER_LOG:
                    continue
                seen_signatures.add(signature)
                fight_ticks[name] += 1
            elif name not in GEARUP_LOGS:
                continue
            message = _request(name, r)
            text = _answer(r, message, recorded)
            if text is None:
                continue
            if name == BAD_ANSWER_LOG:
                # The bad answers repeat in a cycle: keep each one once.
                shape = json.loads(text, parse_constant=str)
                key = json.dumps([r.call, shape.get("action"), shape.get("params")], default=str)
                if key in seen_bad:
                    continue
                seen_bad.add(key)
            pairs[r.call].append(
                {
                    "source": name,
                    "request": message,
                    "answer": text,
                    "expect": _expect(log, r, text),
                }
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for call, items in pairs.items():
        path = OUT_DIR / f"{call}.json"
        lines = ",\n".join("    " + json.dumps(item, sort_keys=True) for item in items)
        path.write_text(
            f'{{\n  "call": "{call}",\n  "pairs": [\n' + lines + "\n  ]\n}\n",
            encoding="utf-8",
            newline="\n",
        )
        counts = {
            k: sum(1 for i in items if i["expect"] == k) for k in ("ok", "corrected", "malformed")
        }
        print(f"{path.relative_to(SDK_SRC.parent.parent.parent)}: {len(items)} pairs {counts}")


if __name__ == "__main__":
    main()
