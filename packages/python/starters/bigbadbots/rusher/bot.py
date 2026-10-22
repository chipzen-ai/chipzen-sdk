# cython: language_level=3str
"""Rusher: a Big Bad Bots starter bot.

Fast bodies, fast hands. Rusher buys a quick body, bids hard for the parts it
wants, then walks straight in, jabs, throws anyone who blocks, and spends its
special the first time it can.

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
import os
import sys

from chipzen import Action, BigBadBotsBot
from chipzen.bigbadbots import AuctionState, BodyShopState, FightState, FittingState, Part
from chipzen.client import run_bot

# ---------------------------------------------------------------------------
# Rusher's taste. These are the knobs: change them and the bot plays differently.
# ---------------------------------------------------------------------------

#: How much Rusher values one point of each attribute when it shops. Speed and
#: power matter most; reach and guard least.
ATTRIBUTE_WEIGHTS = {
    "health": 0.8,
    "power": 1.2,
    "speed": 1.6,
    "reach": 0.8,
    "guard": 0.6,
    "stamina": 1.0,
}

#: Value per unit of a part's rule effect (``Part.effects``). A stabilizer's
#: ``recovery: -1`` is one tick less recovery after every move, so it is worth
#: twelve attribute points; the other effects are fractions.
EFFECT_VALUES = {"regen_mult": 40.0, "block_chip": 40.0, "recovery": -12.0}

#: A bonus, in the same points, for the bodies Rusher likes: the quick ones.
BODY_TASTE = {"wisp": 25.0, "brawler": 25.0, "viper": 15.0, "scrapper": 8.0}

#: Rough value of one filled slot, used to compare bodies before any part is won.
SLOT_VALUE = 20.0

#: Rough price of a part, to guess how many slots the leftover credits can fill.
PART_PRICE_GUESS = 120

#: How hard it bids: 1.0 spends its fair share of credits on an average part.
#: Rusher overpays.
BID_TEMPER = 1.25

#: What an average part is worth, in attribute points.
TYPICAL_PART_VALUE = 20.0

#: Where Rusher likes to stand, as fractions of its own high-kick reach. 0.0 to
#: 0.6 means "in your face".
PREFERRED_RANGE = (0.0, 0.6)

#: A bonus, in damage points, for the moves Rusher likes.
MOVE_TASTE = {"punch_high": 1.0, "throw": 1.5, "uppercut": 1.0}

#: Below this stamina Rusher stops starting new attacks and catches its breath.
STAMINA_FLOOR = 15.0

#: The five attacks every body has.
ATTACKS = ("punch_high", "uppercut", "kick_high", "kick_low", "throw")


class Rusher(BigBadBotsBot):
    """Closes in, jabs, throws blockers, spends its special early."""

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
        """What one part adds, in attribute points, by Rusher's weights."""
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

        # Busy with a move or stunned: the server ignores any answer.
        if me.phase is not None:
            return Action.act("idle")

        # 1. The opponent is stuck (recovering, stunned, dazed, or mid-swing):
        #    hit it with the best move that lands before it can act again.
        if opp.phase in ("recovery", "hitstun", "dazed", "active") and opp.frames_left:
            window = opp.frames_left
            if opp.phase == "active":
                window += int(self.move_data(opp.action or "").get("recovery", 0))
            punish = self.best_punish(me, distance, window)
            if punish is not None:
                return Action.act(punish)
            # Nothing reaches in time. Mid-swing it may still hit us: guard.
            # Otherwise walk in to be closer for the next chance.
            return Action.act("block" if opp.phase == "active" else "move_in")

        # 2. The opponent is winding up a hidden move. Rusher does not wait to
        #    find out what it is: if its fastest move reaches, it strikes first.
        if opp.phase == "startup":
            jab = self.fastest_landing_attack(me, distance)
            if jab is not None:
                return Action.act(jab)
            return Action.act("block")

        # 3. Both free. Spend the special as soon as it can do something.
        if self.special_is_useful(me, distance):
            return Action.act("special")

        # Out of breath: step back and recover instead of swinging.
        if me.stamina < STAMINA_FLOOR:
            return Action.act("move_out" if distance < opp.strike_range else "idle")

        attack = self.best_attack(me, opp, distance)
        if attack is not None:
            return Action.act(attack)

        # Nothing lands from here: walk in until we are in Rusher's range.
        high_kick = self.reach_of("kick_high", me)
        if distance > PREFERRED_RANGE[1] * high_kick:
            return Action.act("move_in")
        return Action.act("idle")

    def best_attack(self, me, opp, distance):
        """Rusher's favourite attack from here: the most damage per tick of
        wind-up (fast hands), plus its taste, and a throw against a blocker."""
        best, best_score = None, None
        for move in ATTACKS:
            if not (self.can_afford(move, me) and self.lands(move, me, distance)):
                continue
            score = self.damage_of(move) / max(1, self.startup_of(move))
            score += MOVE_TASTE.get(move, 0.0)
            if move == "throw" and opp.action == "block":
                score += 5.0  # throws go through a block
            if best_score is None or score > best_score:
                best, best_score = move, score
        return best

    def fastest_landing_attack(self, me, distance):
        moves = [m for m in ATTACKS if self.can_afford(m, me) and self.lands(m, me, distance)]
        return min(moves, key=self.startup_of) if moves else None

    def best_punish(self, me, distance, window):
        """The most damaging move that hits before ``window`` runs out."""
        options = list(ATTACKS)
        if self.special_kind(me) == "strike" and not me.special_used:
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

    def special_kind(self, me):
        return self.move_data(me.special).get("kind")

    def special_is_useful(self, me, distance):
        """Rusher fires its special early: a striking special as soon as it
        reaches, a power-up as soon as it is close. A stance (a long-lasting
        utility special, like a counter) is only worth it against a swing, so
        it is left alone here."""
        if me.special_used or not self.can_afford(me.special, me):
            return False
        data = self.move_data(me.special)
        if not data:
            return False
        if data.get("kind") == "strike":
            return self.lands(me.special, me, distance)
        is_stance = int(data.get("active", 1)) >= 5
        return not is_stance and distance <= me.strike_range * 1.2


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
            Rusher(),
            token=os.environ.get("CHIPZEN_TOKEN"),
            ticket=os.environ.get("CHIPZEN_TICKET"),
        )
    )


if __name__ == "__main__":
    main()
