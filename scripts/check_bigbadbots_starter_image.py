#!/usr/bin/env python3
"""Check a built Big Bad Bots starter image plays every call with its COMPILED bot.

Runs inside the starter's runtime image (CI mounts this script and the protocol
doc), so it exercises the Cython-compiled ``bot`` module and the SDK exactly as
installed there:

* ``bot`` resolves to the ``.so`` and no ``.py`` source is in ``/bot``;
* ``bot.main`` exists (the image ENTRYPOINT calls it);
* the module's one ``BigBadBotsBot`` subclass answers each published
  ``turn_request`` of ``BIGBADBOTS-GAME-STATE-PROTOCOL.md`` (body shop, one
  auction lot, fitting, one fight tick) with one of its ``valid_actions``,
  both before and after ``match_start``.

Usage (from the repo root, after ``docker build -t <tag> <starter dir>``)::

    docker run --rm \
      -v "$PWD/scripts/check_bigbadbots_starter_image.py:/tmp/check.py" \
      -v "$PWD/docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md:/tmp/doc.md" \
      -e PYTHONPATH=/bot --entrypoint python <tag> /tmp/check.py /tmp/doc.md
"""

import inspect
import json
import os
import re
import sys
import time

import bot

from chipzen import BigBadBotsBot
from chipzen.bigbadbots import parse_bigbadbots_turn_request

FENCE = re.compile(r"^```json\n(.*?)\n```$", re.MULTILINE | re.DOTALL)


def main(doc_path: str) -> None:
    assert bot.__file__.endswith(".so"), bot.__file__
    assert callable(bot.main), "the image ENTRYPOINT calls bot.main()"
    leaked = [f for f in os.listdir("/bot") if f.endswith(".py")]
    assert not leaked, f"strategy source leaked into the runtime image: {leaked}"

    classes = [
        cls
        for _, cls in inspect.getmembers(bot, inspect.isclass)
        if issubclass(cls, BigBadBotsBot) and cls is not BigBadBotsBot
    ]
    assert len(classes) == 1, f"expected one BigBadBotsBot subclass, found {classes}"

    with open(doc_path, encoding="utf-8") as fh:
        blocks = [json.loads(body) for body in FENCE.findall(fh.read())]
    requests = [b for b in blocks if b.get("type") == "turn_request"]
    game_config = next(b for b in blocks if "rule_set_id" in b)
    assert len(requests) == 4, f"expected the four published requests, found {len(requests)}"

    for with_config in (False, True):
        player = classes[0]()
        if with_config:
            player.on_match_start({"game_config": game_config})
        for request in requests:
            state = parse_bigbadbots_turn_request(request)
            start = time.perf_counter()
            action = player.decide(state)
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert action.action in request["valid_actions"], (state.phase, action)
            assert elapsed_ms < 100, f"{state.phase}: {elapsed_ms:.1f} ms"
            print(f"{classes[0].__name__} {state.phase}: {action.action} {action.params}")

    print("compiled starter OK:", bot.__file__)


if __name__ == "__main__":
    main(sys.argv[1])
