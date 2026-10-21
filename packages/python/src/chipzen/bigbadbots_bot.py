"""Base class for Big Bad Bots bots, and the safe fallback answers.

Subclass :class:`BigBadBotsBot` and implement :meth:`~BigBadBotsBot.act` (one
fight tick). The three gear-up calls have working defaults you can override:
:meth:`~BigBadBotsBot.choose_body`, :meth:`~BigBadBotsBot.bid` and
:meth:`~BigBadBotsBot.fit`.

The session loop is the same one every Chipzen bot runs
(:func:`chipzen.client.run_bot` / :func:`chipzen.run_external_bot`): at a
table whose server ``hello`` declares ``state_shape: "bigbadbots"`` it parses
each ``turn_request`` into the model for its call and hands it to
:meth:`~BigBadBotsBot.decide`, which routes on ``state.phase``. A
:class:`BigBadBotsBot` declares ``supported_games=["bigbadbots"]`` in its
``hello`` unless the caller passes its own list.

The rule set a match is played under reaches every call as ``state.rules``
(:class:`chipzen.bigbadbots_rules.RuleSet`); read every number from there.
Rule sets are versioned (``bigbadbots-1.1``, ``bigbadbots-1.2``, ...): see
https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md

See ``docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md``. Section numbers below
refer to it.
"""

from __future__ import annotations

import dataclasses
import logging
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any

from chipzen.bigbadbots import (
    FIGHT_ACTIONS,
    STATE_SHAPE,
    AuctionState,
    BigBadBotsGameConfig,
    BigBadBotsTurnState,
    BodyShopState,
    FightState,
    FittingState,
    StateParseError,
)
from chipzen.bigbadbots_rules import RuleSet, rule_set_warnings
from chipzen.bot import ChipzenBot
from chipzen.models import Action, GameState

logger = logging.getLogger("chipzen")

#: The fight actions the server repeats on a missed tick (section 3.5). Any
#: other previous action defaults to ``idle``.
REPEATED_ON_MISS = ("block", "move_in", "move_out")


class BigBadBotsBot(ChipzenBot):
    """Base class for a Big Bad Bots bot: one method per phase.

    ======================  ===============  ======================
    ``state.phase``         Method           State model
    ======================  ===============  ======================
    ``body_shop``           :meth:`choose_body`  :class:`~chipzen.bigbadbots.BodyShopState`
    ``auction`` (per lot)   :meth:`bid`      :class:`~chipzen.bigbadbots.AuctionState`
    ``fitting``             :meth:`fit`      :class:`~chipzen.bigbadbots.FittingState`
    ``fight`` (per tick)    :meth:`act`      :class:`~chipzen.bigbadbots.FightState`
    ======================  ===============  ======================

    Only :meth:`act` is required. Every step is simultaneous: both seats are
    asked at the same moment, and nothing about the opponent's answer reaches
    you before you answer.

    **Rule sets.** Every state carries ``state.rules``, the rule set this
    match is played under (:class:`~chipzen.bigbadbots_rules.RuleSet`): its
    id and every number it plays by, as ``match_start`` sent them. Read the
    numbers from there and the bot runs unchanged when a new rule set
    changes them. A ``rule_set_id`` this SDK release does not know is logged
    as a warning and the bot plays on the server's numbers. See
    https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md

    **Timing.** A fight tick's ``state.timeout_ms`` is the per-tick deadline
    plus what is left of your time bank (section 3.4). The SDK waits for
    :meth:`act` until just short of ``timeout_ms``; past that it sends the
    protocol's default for the call (see :func:`fallback_action`) and logs a
    warning. The same default is sent when a method raises (under
    ``safe_mode``) or returns something that is not one of the call's
    ``valid_actions``. While a timed-out call is still running, later
    requests are answered with the default without calling your method again.

    The lifecycle hooks of :class:`chipzen.Bot` (``on_match_start``,
    ``on_round_start``, ``on_turn_result``, ``on_round_result``,
    ``on_match_end``, ``on_decision_latency``, ...) all fire as usual.

    Example::

        from chipzen import Action, BigBadBotsBot
        from chipzen.bigbadbots import FightState

        class MyFighter(BigBadBotsBot):
            def act(self, state: FightState) -> Action:
                if state.distance > state.me.strike_range:
                    return Action.act("move_in")
                return Action.act("punch_high")
    """

    #: The rule set and its full parameter table, parsed from
    #: ``match_start.game_config`` (or ``reconnected.game_config``) by the
    #: default :meth:`on_match_start` / :meth:`on_reconnected`. ``None`` until
    #: then. If you override either hook, call ``super()`` to keep it.
    game_config: BigBadBotsGameConfig | None = None

    #: :attr:`game_config`'s rule set, typed and read-only; the same object
    #: every call gets as ``state.rules``. ``None`` until ``match_start``.
    rules: RuleSet | None = None

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def decide(self, state: GameState | BigBadBotsTurnState) -> Action:
        """Route one ``turn_request`` to the method for its phase.

        You do not normally override this; override the per-phase methods.
        It fills in ``state.rules`` from :attr:`rules` before routing.

        Raises:
            TypeError: ``state`` is not a Big Bad Bots state (the bot was
                seated at a poker table).
            ValueError: ``state.phase`` is not one of the four phases.
        """
        if isinstance(state, GameState):
            raise TypeError("BigBadBotsBot received a poker GameState; it only plays bigbadbots")
        if self.rules is not None and state.rules is None:
            state = dataclasses.replace(state, rules=self.rules)
        if isinstance(state, FightState):
            return self.act(state)
        if isinstance(state, AuctionState):
            return self.bid(state)
        if isinstance(state, BodyShopState):
            return self.choose_body(state)
        if isinstance(state, FittingState):
            return self.fit(state)
        raise ValueError(f"not a Big Bad Bots phase: {getattr(state, 'phase', None)!r}")

    # ------------------------------------------------------------------
    # One method per phase
    # ------------------------------------------------------------------

    def choose_body(self, state: BodyShopState) -> Action:
        """Body shop: buy one body from ``state.offer``.

        The default buys the **first body on the offer**, which is also what
        the server applies when no answer arrives in time (section 3.5). It
        is a legal answer, not a strategy.
        """
        return Action.choose_body(state.offer[0].id)

    def bid(self, state: AuctionState) -> Action:
        """Auction: one sealed bid on ``state.lot``, from ``0`` to ``state.me.credits``.

        Called once per lot. The default bids ``0`` (pass), which is also
        the server's default for a missed bid.
        """
        return Action.bid(0)

    def fit(self, state: FittingState) -> Action:
        """Fitting: put parts from ``state.owned`` into the slots of ``state.slots``.

        Name slots by their index into ``state.slots``. The default fits each
        slot, in order, with the first unused owned part of that slot's type.
        That is **legal, not good**: it ignores what the parts do. (The
        server's default for a missed fitting is the empty fitting.)
        """
        fitting: dict[int | str, str] = {}
        used: set[int] = set()
        for slot_index, slot_type in enumerate(state.slots):
            for part_index, part in enumerate(state.owned):
                if part_index not in used and part.slot == slot_type:
                    fitting[slot_index] = part.id
                    used.add(part_index)
                    break
        return Action.fit(fitting)

    @abstractmethod
    def act(self, state: FightState) -> Action:
        """Fight: this tick's action, built with :meth:`chipzen.Action.act`.

        Called once per tick, several times a second, so keep it fast: stay
        inside ``state.rules.deadline.deadline_ms`` and the time
        bank is never touched. ``state.valid_actions`` is always all ten fight
        actions; whether one can be performed now (stamina, a busy fighter, a
        used special) is decided when the tick resolves.
        """

    # ------------------------------------------------------------------
    # Match context
    # ------------------------------------------------------------------

    def on_match_start(self, match_info: dict) -> None:
        """Parse ``match_start.game_config`` into :attr:`game_config` and :attr:`rules`.

        Logs a warning when the rule set is not one this SDK release knows,
        or differs from its published numbers
        (:func:`chipzen.bigbadbots_rules.rule_set_warnings`); the bot plays
        on the values ``match_start`` sent either way.

        If you override this, call ``super().on_match_start(match_info)``.
        """
        self._learn_game_config(match_info)

    def on_reconnected(self, message: dict) -> None:
        """Re-learn :attr:`game_config` and :attr:`rules` from a ``reconnected`` message.

        If you override this, call ``super().on_reconnected(message)``.
        """
        if "game_config" in message:
            self._learn_game_config(message)

    def _learn_game_config(self, message: Mapping[str, Any]) -> None:
        self.game_config = BigBadBotsGameConfig.from_match_start(message)
        try:
            self.rules = RuleSet.from_game_config(self.game_config)
        except StateParseError as exc:
            # The server's table lacks a section the typed view needs. Keep
            # playing: game_config.rule_set still holds everything it sent.
            self.rules = None
            logger.warning(
                "rule set %r could not be read into state.rules (%s); it is None for "
                "this match, and game_config.rule_set holds the raw table",
                self.game_config.rule_set_id,
                exc,
            )
            return
        for warning in rule_set_warnings(self.rules):
            logger.warning(warning)


# ---------------------------------------------------------------------------
# The safe fallback (section 3.5)
# ---------------------------------------------------------------------------


def fallback_action(request: Mapping[str, Any], previous_fight_action: str | None) -> Action | None:
    """The protocol's default answer for a Big Bad Bots ``turn_request``.

    This is exactly what the server applies to a seat that does not answer in
    time (section 3.5), so sending it never plays something the protocol
    would not have played anyway:

    - ``body_shop``: the first body on the offer;
    - ``auction``: a bid of ``0``;
    - ``fitting``: the empty fitting;
    - ``fight``: ``previous_fight_action`` if it is ``block``, ``move_in`` or
      ``move_out``, otherwise ``idle``.

    Works on the raw message, so it still answers when the payload itself
    failed to parse. Returns ``None`` when no default can be built (an unknown
    phase, or a body shop with no readable offer); the caller then sends
    nothing and the server applies its own default when the time runs out.
    """
    state = request.get("state")
    if not isinstance(state, Mapping):
        return None
    phase = state.get("phase")
    if phase == "fight":
        if previous_fight_action in REPEATED_ON_MISS:
            return Action.act(previous_fight_action)
        return Action.act("idle")
    if phase == "auction":
        return Action.bid(0)
    if phase == "fitting":
        return Action.fit({})
    if phase == "body_shop":
        offer = state.get("offer")
        if isinstance(offer, list) and offer:
            first = offer[0]
            if isinstance(first, Mapping) and isinstance(first.get("id"), str):
                return Action.choose_body(first["id"])
        return None
    return None


def is_answer_shaped(action: object, valid_actions: list[str]) -> bool:
    """Whether ``action`` is an :class:`Action` naming one of ``valid_actions``.

    Used to catch a handler that returned something the server would reject
    outright (``None``, a poker action, a gear-up action in the fight) before
    it is sent.
    """
    return isinstance(action, Action) and action.action in valid_actions


__all__ = [
    "BigBadBotsBot",
    "FIGHT_ACTIONS",
    "REPEATED_ON_MISS",
    "STATE_SHAPE",
    "fallback_action",
    "is_answer_shaped",
]
