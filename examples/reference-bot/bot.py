"""Reference Chipzen bot — non-trivial demonstration.

Intentionally simple but **competent** — the goal is to show, in
~150 lines, that the protocol carries real strategy state cleanly:

  * Per-match state via ``on_match_start`` (seat assignment).
  * Per-hand state via ``on_round_start`` (reset trackers each hand).
  * Live observation via ``on_turn_result`` (count opponent aggression
    in the current hand).
  * Branching on ``state.phase`` for preflop vs postflop.
  * Heuristic hand-strength bucketing using ``state.hole_cards``.
  * Made-hand detection from ``state.hole_cards`` + ``state.board``.
  * Action history awareness via ``self.opponent_raises_this_hand``.
  * Strict ``state.valid_actions`` checking — this bot will never
    return an action the server hasn't offered.

The strategy is **not strong** — it folds too much, doesn't bluff,
ignores pot odds, has no postflop draw recognition, and uses a
crude rank-bucket model. That's fine: the point of this file is to
show that a bot author **can** express real logic against the SDK,
not that the bot itself is competitive.

If you're starting your own bot, copy
``packages/python/starters/python/`` instead — it's a thin scaffold
with the IP-protected Cython Dockerfile.

Environment:
    CHIPZEN_WS_URL   WebSocket URL injected by the platform at launch.
    CHIPZEN_TOKEN    Bot API token (empty string is fine for localhost dev).
    CHIPZEN_TICKET   Alternative single-use ticket; unused here but forwarded
                     so the image works in either flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import Counter
from typing import Iterable

from chipzen import Action, Bot, GameState
from chipzen.client import run_bot
from chipzen.models import Card

logger = logging.getLogger("reference-bot")


# ---------------------------------------------------------------------------
# Card / hand helpers — pure functions, no SDK state
# ---------------------------------------------------------------------------

# Rank order, weakest to strongest. Index = numeric strength.
_RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A")
_RANK_INDEX = {r: i for i, r in enumerate(_RANKS)}


def _preflop_bucket(hole_cards: list[Card]) -> str:
    """Coarse preflop hand bucket: ``premium`` / ``strong`` / ``medium`` / ``weak``.

    Crude on purpose. A real bot would use range tables, position
    adjustments, and equity vs. an opponent range model. This buckets
    enough to demonstrate that the SDK's hole-card data is shaped
    correctly for that kind of work.
    """
    if len(hole_cards) != 2:
        return "weak"

    r1, r2 = hole_cards[0].rank, hole_cards[1].rank
    suited = hole_cards[0].suit == hole_cards[1].suit
    high, low = (r1, r2) if _RANK_INDEX[r1] >= _RANK_INDEX[r2] else (r2, r1)

    # Pocket pairs
    if r1 == r2:
        if r1 in {"J", "Q", "K", "A"}:
            return "premium"
        if r1 in {"9", "T"}:
            return "strong"
        return "medium"

    # AK
    if {high, low} == {"A", "K"}:
        return "premium"

    # Broadways with an ace
    if high == "A" and low in {"Q", "J", "T"}:
        return "strong" if suited else "medium"

    # KQ, KJ
    if high == "K" and low in {"Q", "J"}:
        return "strong" if suited else "medium"

    # Connected broadways
    if high in {"Q", "J"} and low in {"T", "9"}:
        return "strong" if suited else "medium"

    # Weak ace (any)
    if high == "A":
        return "medium"

    return "weak"


def _made_hand_class(hole_cards: list[Card], board: list[Card]) -> int:
    """Crude category of the best 7-card holding so far.

    Returns:
        0 — no pair (high card only).
        1 — one pair.
        2 — two pair.
        3 — three of a kind or better.
    """
    ranks: list[str] = [c.rank for c in (*hole_cards, *board)]
    counts = sorted(Counter(ranks).values(), reverse=True)
    if not counts:
        return 0
    if counts[0] >= 3:
        return 3
    if len(counts) >= 2 and counts[0] == 2 and counts[1] == 2:
        return 2
    if counts[0] == 2:
        return 1
    return 0


def _bounded_raise(target: int, state: GameState) -> int | None:
    """Return ``target`` clamped to ``[min_raise, max_raise]``, or ``None``.

    ``None`` means raising is illegal at this turn (the SDK reports
    ``min_raise == 0`` and ``max_raise == 0`` in that case).
    """
    if state.min_raise == 0 or state.max_raise == 0:
        return None
    if target < state.min_raise:
        return state.min_raise
    if target > state.max_raise:
        return state.max_raise
    return target


def _opponent_raises_in_history(history: Iterable[dict], my_seat: int | None) -> int:
    """Count raise / all_in actions by anyone other than ``my_seat`` in this hand."""
    n = 0
    for entry in history or ():
        seat = entry.get("seat")
        action = entry.get("action")
        if seat == my_seat:
            continue
        if action in ("raise", "all_in"):
            n += 1
    return n


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------


class ReferenceBot(Bot):
    """Non-trivial reference: position-blind preflop buckets, postflop
    on hand class + opponent aggression."""

    def __init__(self) -> None:
        super().__init__()
        # Per-match state
        self._my_seat: int | None = None
        # Per-hand state — reset by on_round_start
        self._opponent_raises_this_hand: int = 0

    # ----- lifecycle hooks -------------------------------------------------

    def on_match_start(self, match_info: dict) -> None:
        for seat in match_info.get("seats", ()) or ():
            if seat.get("is_self"):
                self._my_seat = int(seat["seat"])
                break
        logger.info("match_start my_seat=%s", self._my_seat)

    def on_round_start(self, message: dict) -> None:
        super().on_round_start(message)
        self._opponent_raises_this_hand = 0

    def on_turn_result(self, message: dict) -> None:
        details = message.get("details", {}) or {}
        if details.get("seat") == self._my_seat:
            return
        if details.get("action") in ("raise", "all_in"):
            self._opponent_raises_this_hand += 1

    # ----- decision --------------------------------------------------------

    def decide(self, state: GameState) -> Action:
        valid = list(state.valid_actions or ())

        # Action_history is also exposed on the GameState for bots that
        # prefer to derive aggression from the canonical history rather
        # than the on_turn_result hook. Use the hook-tracked counter as
        # the primary source and reconcile against history as a sanity
        # check — they should agree.
        history_raises = _opponent_raises_in_history(
            state.action_history, self._my_seat
        )
        opp_aggression = max(self._opponent_raises_this_hand, history_raises)

        if state.phase == "preflop":
            chosen = self._decide_preflop(state, valid)
        else:
            chosen = self._decide_postflop(state, valid, opp_aggression)

        logger.info(
            "decide hand=%s phase=%s legal=%s opp_aggro=%d action=%s",
            state.hand_number,
            state.phase,
            ",".join(valid) or "-",
            opp_aggression,
            chosen.action,
        )
        return chosen

    def _decide_preflop(self, state: GameState, valid: list[str]) -> Action:
        bucket = _preflop_bucket(state.hole_cards)

        # Premium: open-raise to ~3x BB if we can, otherwise call.
        if bucket == "premium":
            target = _bounded_raise(
                state.min_raise * 3 if state.min_raise else 0, state
            )
            if target is not None and "raise" in valid:
                return Action.raise_to(target)
            if "call" in valid:
                return Action.call()
            return Action.check() if "check" in valid else Action.fold()

        # Strong: raise unopened pots, otherwise call cheap.
        if bucket == "strong":
            if state.to_call == 0 and "raise" in valid:
                target = _bounded_raise(
                    state.min_raise * 2 if state.min_raise else 0, state
                )
                if target is not None:
                    return Action.raise_to(target)
            if "call" in valid and state.to_call <= state.your_stack // 10:
                return Action.call()
            return Action.check() if "check" in valid else Action.fold()

        # Medium: only call free / very cheap.
        if bucket == "medium":
            if "check" in valid:
                return Action.check()
            if "call" in valid and state.to_call <= state.your_stack // 30:
                return Action.call()
            return Action.fold()

        # Weak: check / fold.
        if "check" in valid:
            return Action.check()
        return Action.fold()

    def _decide_postflop(
        self, state: GameState, valid: list[str], opp_aggression: int
    ) -> Action:
        klass = _made_hand_class(state.hole_cards, state.board)

        # Two pair or better: bet 2/3 pot if we can, else call.
        if klass >= 2:
            if "raise" in valid and opp_aggression == 0:
                target = _bounded_raise(int(state.pot * 0.66), state)
                if target is not None:
                    return Action.raise_to(target)
            if "call" in valid:
                return Action.call()
            return Action.check() if "check" in valid else Action.fold()

        # One pair: check, call small bets, fold to pressure.
        if klass == 1:
            if "check" in valid:
                return Action.check()
            if "call" in valid and state.to_call <= state.pot // 3:
                return Action.call()
            return Action.fold()

        # Nothing made: check or fold. (No bluffs in the reference bot.)
        if "check" in valid:
            return Action.check()
        return Action.fold()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _amain() -> None:
    log_level = os.environ.get("REFERENCE_BOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    ws_url = os.environ.get("CHIPZEN_WS_URL") or os.environ.get("CHIPZEN_URL", "")
    if not ws_url:
        logger.error("CHIPZEN_WS_URL is required")
        sys.exit(2)

    token = os.environ.get("CHIPZEN_TOKEN")
    ticket = os.environ.get("CHIPZEN_TICKET")

    logger.info("reference-bot ready; connecting to %s", ws_url)
    await run_bot(
        ws_url,
        ReferenceBot(),
        token=token if token is not None else None,
        ticket=ticket if ticket else None,
        client_name="reference-bot",
        client_version="0.2.0",
    )


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")


if __name__ == "__main__":
    main()
