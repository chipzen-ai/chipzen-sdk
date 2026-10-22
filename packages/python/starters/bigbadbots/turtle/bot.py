# cython: language_level=3str
"""Turtle: a Big Bad Bots starter bot.

Armour and patience. Turtle buys a heavy, well-guarded body, walks to a
comfortable distance and waits. When you wind up, it blocks (or uses a counter
stance if its body has one); when you whiff, it punishes hard. It only goes
looking for a fight when nobody has landed a hit for a while, or when it is
behind with the round clock running down.

It is one of the Chipzen platform bots, rewritten as a short, readable starter on
the SDK's :class:`chipzen.BigBadBotsBot`. Change the constants at the top to
change its taste; change the methods to change how it thinks.

How a match runs (``docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md``):

1. ``choose_body``: buy one body from your offer.
2. ``bid``: one sealed bid per auction lot.
3. ``fit``: put the parts you won into your body's slots.
4. ``act``: one fight action per tick, ten ticks a second, until the match ends.

Every number this bot plays by (move speeds, ranges, stamina costs) is read from
``match_start.game_config.rule_set``, so it keeps working when a new rule set
changes them. The SDK handles the connection, the timing, and the safe default
answer if a decision ever runs late or raises.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys

from chipzen import Action, BigBadBotsBot
from chipzen.bigbadbots import AuctionState, BodyShopState, FightState, FittingState, Part
from chipzen.client import run_bot

# ---------------------------------------------------------------------------
# Turtle's taste. These are the knobs: change them and the bot plays differently.
# ---------------------------------------------------------------------------

#: How much Turtle values one point of each attribute when it shops. Guard and
#: health come first; speed hardly matters.
ATTRIBUTE_WEIGHTS = {
    "health": 1.4,
    "power": 1.0,
    "speed": 0.6,
    "reach": 1.0,
    "guard": 1.6,
    "stamina": 1.0,
}

#: Value per unit of a part's rule effect (``Part.effects``). A stabilizer's
#: ``recovery: -1`` is one tick less recovery after every move, so it is worth
#: twelve attribute points; the other effects are fractions.
EFFECT_VALUES = {"regen_mult": 40.0, "block_chip": 30.0, "recovery": -12.0}

#: A bonus, in the same points, for the bodies Turtle likes: the armoured ones.
BODY_TASTE = {"bastion": 25.0, "golem": 25.0, "warden": 25.0, "anvil": 10.0}

#: Rough value of one filled slot, used to compare bodies before any part is won.
SLOT_VALUE = 20.0

#: Rough price of a part, to guess how many slots the leftover credits can fill.
PART_PRICE_GUESS = 120

#: How hard it bids: 1.0 spends its fair share of credits on an average part.
#: Turtle is the thriftiest of the three.
BID_TEMPER = 0.65

#: What an average part is worth, in attribute points.
TYPICAL_PART_VALUE = 20.0

#: Where Turtle likes to stand, as fractions of its own high-kick reach: close
#: enough to punish, not so close that it gets thrown.
PREFERRED_RANGE = (0.7, 0.95)

#: A bonus, in damage points, for the moves Turtle likes to punish with.
MOVE_TASTE = {"uppercut": 1.5, "kick_high": 0.5}

#: Below this stamina Turtle stops starting new attacks and catches its breath.
STAMINA_FLOOR = 15.0

#: Ticks with nobody hurt before Turtle loses patience and starts the fight
#: itself (10 ticks a second, so 8 seconds).
PATIENCE_TICKS = 80

#: From this fraction of the round on, Turtle attacks if it is losing on the
#: round's time-out tiebreak.
CLOCK_START = 0.6

#: The five attacks every body has.
ATTACKS = ("punch_high", "uppercut", "kick_high", "kick_low", "throw")


class Turtle(BigBadBotsBot):
    """Blocks, waits for a mistake, punishes it hard."""

    def __init__(self) -> None:
        super().__init__()
        # Bookkeeping for patience: the last HP we saw on both sides, and how
        # many ticks in a row nobody was hurt. Not annotated on purpose: this
        # file is compiled with Cython for the runtime image, and there an
        # attribute annotation is a C type declaration, not a hint.
        self.last_seen = None
        self.quiet_ticks = 0

    # -----------------------------------------------------------------------
    # Reading the rule set
    # -----------------------------------------------------------------------

    def rule_section(self, name):
        """One section of ``game_config.rule_set``, or ``{}`` if it is missing."""
        if self.game_config is None:
            return {}
        section = self.game_config.rule_set.get(name)
        return section if isinstance(section, dict) else {}

    def move_data(self, move_id):
        """A move's frame data from the rule set (``moves``, then ``specials``)."""
        return self.rule_section("moves").get(move_id) or self.rule_section("specials").get(
            move_id, {}
        )

    def reach_of(self, move_id, fighter):
        """How far a move reaches for this fighter.

        A move either has a fixed ``range`` (the throw does) or reaches a
        multiple of the fighter's ``strike_range``. With no frame data we
        assume plain strike range.
        """
        data = self.move_data(move_id)
        if data.get("range") is not None:
            return float(data["range"])
        return float(data.get("reach", 1.0)) * fighter.strike_range

    def startup_of(self, move_id):
        """Ticks from starting a move to its first hit. Unknown counts as slow."""
        return int(self.move_data(move_id).get("startup", 5))

    def damage_of(self, move_id):
        """A move's listed damage before attributes. Unknown counts as small."""
        return float(self.move_data(move_id).get("damage", 5))

    def can_afford(self, move_id, fighter):
        return fighter.stamina >= float(self.move_data(move_id).get("stamina", 0))

    def lands(self, move_id, fighter, distance):
        return distance <= self.reach_of(move_id, fighter)

    # -----------------------------------------------------------------------
    # Gear-up: body shop, auction, fitting
    # -----------------------------------------------------------------------

    def part_value(self, part):
        """What one part adds, in attribute points, by Turtle's weights."""
        value = sum(ATTRIBUTE_WEIGHTS.get(k, 0.0) * v for k, v in part.bonus.items())
        value += sum(EFFECT_VALUES.get(k, 0.0) * v for k, v in part.effects.items())
        return value

    def best_fitting(self, slots, parts):
        """The best part for each slot: for each slot type, the most valuable
        parts of that type, one per slot. Parts worth nothing stay out."""
        fitting = {}
        ranked = sorted(parts, key=self.part_value, reverse=True)
        used = set()
        for slot_index, slot_type in enumerate(slots):
            for i, part in enumerate(ranked):
                if i not in used and part.slot == slot_type and self.part_value(part) > 0:
                    fitting[slot_index] = part
                    used.add(i)
                    break
        return fitting

    def choose_body(self, state: BodyShopState) -> Action:
        credits = state.me.credits
        best_id, best_score = None, None
        for body in state.offer:
            if body.cost > credits:
                continue
            score = sum(ATTRIBUTE_WEIGHTS.get(k, 0.0) * v for k, v in body.stats.items())
            score += BODY_TASTE.get(body.id, 0.0)
            # More slots are worth more, but only as many as the leftover
            # credits can plausibly fill.
            fillable = min(len(body.slots), (credits - body.cost) / PART_PRICE_GUESS)
            score += SLOT_VALUE * fillable
            if best_score is None or score > best_score:
                best_id, best_score = body.id, score
        if best_id is None:  # nothing affordable: the cheapest is the least bad
            best_id = min(state.offer, key=lambda b: b.cost).id
        return Action.choose_body(best_id)

    def bid(self, state: AuctionState) -> Action:
        credits = state.me.credits
        if credits <= 0:
            return Action.bid(0)
        # What does this lot add to the best fitting of the parts already won?
        slots = self.my_slots(state.me.body)
        owned = [self.known_part(pid) for pid in state.me.owned]
        owned = [p for p in owned if p is not None]
        before = sum(self.part_value(p) for p in self.best_fitting(slots, owned).values())
        after = sum(
            self.part_value(p) for p in self.best_fitting(slots, owned + [state.lot]).values()
        )
        gain = after - before
        if gain <= 0:
            return Action.bid(0)
        # Never bid more than the opponent could possibly beat.
        cap = min(credits, state.opp.credits + 1)
        lots_left = state.lots_total - state.lot_index
        if lots_left <= 1:
            return Action.bid(cap)  # last lot: credits are worth nothing after it
        fair_share = credits / lots_left
        amount = fair_share * (gain / TYPICAL_PART_VALUE) * BID_TEMPER
        return Action.bid(int(max(0, min(cap, amount))))

    def fit(self, state: FittingState) -> Action:
        fitting = self.best_fitting(state.slots, state.owned)
        return Action.fit({slot: part.id for slot, part in fitting.items()})

    def my_slots(self, body_id):
        """A body's slot types, from the rule set's body table."""
        body = self.rule_section("bodies").get(body_id or "", {})
        return list(body.get("slots", []))

    def known_part(self, part_id):
        """A part this seat owns, rebuilt from the rule set (the auction only
        names owned parts by id)."""
        data = self.rule_section("parts").get(part_id)
        if not isinstance(data, dict):
            return None
        return Part(
            id=part_id,
            slot=str(data.get("slot", "")),
            bonus=dict(data.get("bonus") or {}),
            effects=dict(data.get("effects") or {}),
        )

    # -----------------------------------------------------------------------
    # The fight: one decision per tick
    # -----------------------------------------------------------------------

    def act(self, state: FightState) -> Action:
        me, opp, distance = state.me, state.opp, state.distance
        self.count_quiet_ticks(state)

        # Busy with a move or stunned: the server ignores any answer.
        if me.phase is not None:
            return Action.act("idle")

        # 1. The opponent is stuck (recovering, stunned, dazed, or mid-swing):
        #    this is the mistake Turtle waits for. Punish with the hardest hit
        #    that lands in time.
        if opp.phase in ("recovery", "hitstun", "dazed", "active") and opp.frames_left:
            window = opp.frames_left
            if opp.phase == "active":
                window += int(self.move_data(opp.action or "").get("recovery", 0))
            punish = self.best_punish(me, distance, window)
            if punish is not None:
                return Action.act(punish)
            if opp.phase == "active":
                return Action.act("block")  # mid-swing, it may still hit us
            if self.can_close_in_time(me, distance, window):
                return Action.act("move_in")
            return Action.act("idle")

        # 2. The opponent is winding up a hidden move. If it could reach us,
        #    guard: a counter stance if our body has one, a block otherwise.
        #    (A block loses to a throw. That is Turtle's weak spot.)
        if opp.phase == "startup":
            if distance <= self.threat_range(opp):
                if self.has_stance_ready(me):
                    return Action.act("special")
                return Action.act("block")
            return Action.act("idle")

        # 3. Both free. A power-up special (armour, say) is worth using once
        #    the opponent is close.
        if self.has_power_up_ready(me) and distance <= opp.strike_range * 1.2:
            return Action.act("special")

        # Normally Turtle waits. It starts the fight itself only when nobody
        # has been hurt for a while, or when it is losing as time runs out.
        if self.quiet_ticks >= PATIENCE_TICKS or self.behind_on_the_clock(state):
            if me.stamina < STAMINA_FLOOR:
                return Action.act("idle")  # get the breath back first
            attack = self.best_attack(me, distance)
            return Action.act(attack or "move_in")

        # Otherwise hold a comfortable distance and let stamina come back.
        high_kick = self.reach_of("kick_high", me)
        near, far = PREFERRED_RANGE[0] * high_kick, PREFERRED_RANGE[1] * high_kick
        if distance > far:
            return Action.act("move_in")
        if distance < near and self.room_behind(state) >= me.move_rate:
            return Action.act("move_out")
        return Action.act("idle")

    def best_attack(self, me, distance):
        """The attack that lands from here with the most damage plus taste."""
        best, best_score = None, None
        for move in ATTACKS:
            if not (self.can_afford(move, me) and self.lands(move, me, distance)):
                continue
            score = self.damage_of(move) + MOVE_TASTE.get(move, 0.0)
            if best_score is None or score > best_score:
                best, best_score = move, score
        return best

    def best_punish(self, me, distance, window):
        """The most damaging move that hits before ``window`` runs out."""
        options = list(ATTACKS)
        if self.move_data(me.special).get("kind") == "strike" and not me.special_used:
            options.append(me.special)
        best, best_damage = None, 0.0
        for move in options:
            if not (self.can_afford(move, me) and self.lands(move, me, distance)):
                continue
            if self.startup_of(move) >= window:
                continue
            damage = self.damage_of(move) + MOVE_TASTE.get(move, 0.0)
            if damage > best_damage:
                best, best_damage = move, damage
        if best is not None and best == me.special:
            return "special"
        return best

    def has_stance_ready(self, me):
        """A stance is a utility special that stays up for several ticks (a
        counter or a shield): exactly what to raise against a swing."""
        data = self.move_data(me.special)
        return (
            not me.special_used
            and data.get("kind") == "utility"
            and int(data.get("active", 1)) >= 5
            and self.can_afford(me.special, me)
        )

    def has_power_up_ready(self, me):
        """Any other utility special: a one-tick power-up such as armour."""
        data = self.move_data(me.special)
        return (
            not me.special_used
            and data.get("kind") == "utility"
            and int(data.get("active", 1)) < 5
            and self.can_afford(me.special, me)
        )

    def count_quiet_ticks(self, state):
        """Count ticks in a row in which nobody lost HP. A new round resets it."""
        seen = (state.round, state.me.hp, state.opp.hp)
        if self.last_seen is not None and seen == self.last_seen:
            self.quiet_ticks += 1
        else:
            self.quiet_ticks = 0
        self.last_seen = seen

    def behind_on_the_clock(self, state):
        """Late in the round and losing the time-out tiebreak? The rule set
        says how a time-out is decided: by damage dealt, or by HP left."""
        round_ticks = int(self.rule_section("rounds").get("round_ticks", 600))
        if state.tick < CLOCK_START * round_ticks:
            return False
        me, opp = state.me, state.opp
        if self.rule_section("rounds").get("timeout_tiebreak") == "damage":
            return (me.max_hp - me.hp) > (opp.max_hp - opp.hp)
        return me.hp / me.max_hp < opp.hp / opp.max_hp

    def can_close_in_time(self, me, distance, window):
        """Can we walk into kick range and still land a kick before ``window``
        runs out?"""
        if me.move_rate <= 0:
            return False
        gap = distance - self.reach_of("kick_low", me)
        steps = math.ceil(max(0.0, gap) / me.move_rate)
        return steps + self.startup_of("kick_low") < window

    def threat_range(self, opp):
        """How far the opponent's longest attack reaches."""
        return max(self.reach_of(move, opp) for move in ATTACKS)

    def room_behind(self, state):
        """Distance to the wall behind us. ``facing`` is ``1`` when the
        opponent is to our right, so our back is to the left wall."""
        left, right = state.walls[0], state.walls[-1]
        return state.me.x - left if state.facing > 0 else right - state.me.x


def main() -> None:
    """Entry point, called by the Dockerfile ENTRYPOINT.

    The Chipzen platform injects ``CHIPZEN_WS_URL`` and ``CHIPZEN_TOKEN`` (or
    ``CHIPZEN_TICKET``) when it starts your container. To run this bot from
    your own machine instead, use ``chipzen run-external bot.py`` (see the
    README), which picks prod or staging for you.
    """
    url = os.environ.get("CHIPZEN_WS_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not url:
        print(
            "error: CHIPZEN_WS_URL not set and no URL passed on the command line",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(
        run_bot(
            url,
            Turtle(),
            token=os.environ.get("CHIPZEN_TOKEN"),
            ticket=os.environ.get("CHIPZEN_TICKET"),
        )
    )


if __name__ == "__main__":
    main()
