/**
 * Rusher: a Big Bad Bots starter bot, in JavaScript.
 *
 * Fast bodies, fast hands. Rusher buys a quick body, bids hard for the parts
 * it wants, then walks straight in, jabs, throws anyone who blocks, and spends
 * its special the first time it can.
 *
 * It is one of the Chipzen house bots, rewritten as a short, readable starter
 * on the SDK's `BigBadBotsBot`. It plays exactly like the Python starter in
 * `packages/python/starters/bigbadbots/rusher/bot.py`. Change the constants at
 * the top to change its taste; change the methods to change how it thinks.
 *
 * How a match runs (`docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`):
 *
 * 1. `chooseBody`: buy one body from your offer.
 * 2. `bid`: one sealed bid per auction lot.
 * 3. `fit`: put the parts you won into your body's slots.
 * 4. `act`: one fight action per tick, ten ticks a second, until the match ends.
 *
 * Every number this bot plays by (move speeds, ranges, stamina costs) is read
 * from `match_start.game_config.rule_set`, so it keeps working when a new rule
 * set changes them. The SDK handles the connection, the timing, and the safe
 * default answer if a decision ever runs late or throws.
 */

import { Action, BigBadBotsBot, runBot } from "@chipzen-ai/bot";

// ---------------------------------------------------------------------------
// Rusher's taste. These are the knobs: change them and the bot plays differently.
// ---------------------------------------------------------------------------

/** How much Rusher values one point of each attribute when it shops. Speed and power matter most; reach and guard least. */
export const ATTRIBUTE_WEIGHTS = {
  health: 0.8,
  power: 1.2,
  speed: 1.6,
  reach: 0.8,
  guard: 0.6,
  stamina: 1.0,
};

/**
 * Value per unit of a part's rule effect (`part.effects`). A stabilizer's
 * `recovery: -1` is one tick less recovery after every move, so it is worth
 * twelve attribute points; the other effects are fractions.
 */
export const EFFECT_VALUES = { regen_mult: 40.0, block_chip: 40.0, recovery: -12.0 };

/** A bonus, in the same points, for the bodies Rusher likes: the quick ones. */
export const BODY_TASTE = { wisp: 25.0, brawler: 25.0, viper: 15.0, scrapper: 8.0 };

/** Rough value of one filled slot, used to compare bodies before any part is won. */
export const SLOT_VALUE = 20.0;

/** Rough price of a part, to guess how many slots the leftover credits can fill. */
export const PART_PRICE_GUESS = 120;

/** How hard it bids: 1.0 spends its fair share of credits on an average part. Rusher overpays. */
export const BID_TEMPER = 1.25;

/** What an average part is worth, in attribute points. */
export const TYPICAL_PART_VALUE = 20.0;

/** Where Rusher likes to stand, as fractions of its own high-kick reach. 0.0 to 0.6 means "in your face". */
export const PREFERRED_RANGE = [0.0, 0.6];

/** A bonus, in damage points, for the moves Rusher likes. */
export const MOVE_TASTE = { punch_high: 1.0, throw: 1.5, uppercut: 1.0 };

/** Below this stamina Rusher stops starting new attacks and catches its breath. */
export const STAMINA_FLOOR = 15.0;

/** The five attacks every body has. */
export const ATTACKS = ["punch_high", "uppercut", "kick_high", "kick_low", "throw"];

/** A plain object (a rule-set section or entry), as opposed to null, an array or a number. */
function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** A plain object with at least one key. (An empty entry counts as missing, as in the Python starter.) */
function isFilled(value) {
  return isObject(value) && Object.keys(value).length > 0;
}

/** `weights[key] * value`, summed over a map of numbers. Keys without a weight count 0. */
function weighted(weights, values) {
  let total = 0;
  for (const [key, value] of Object.entries(values)) total += (weights[key] ?? 0) * value;
  return total;
}

/** Closes in, jabs, throws blockers, spends its special early. */
export class Rusher extends BigBadBotsBot {
  // -------------------------------------------------------------------------
  // Reading the rule set
  // -------------------------------------------------------------------------

  /** One section of `game_config.rule_set`, or `{}` if it is missing. */
  ruleSection(name) {
    if (this.gameConfig === null) return {};
    const section = this.gameConfig.ruleSet[name];
    return isObject(section) ? section : {};
  }

  /** A move's frame data from the rule set (`moves`, then `specials`). */
  moveData(moveId) {
    const move = this.ruleSection("moves")[moveId];
    if (isFilled(move)) return move;
    const special = this.ruleSection("specials")[moveId];
    return special === undefined ? {} : special;
  }

  /**
   * How far a move reaches for this fighter.
   *
   * A move either has a fixed `range` (the throw does) or reaches a multiple
   * of the fighter's `strikeRange`. With no frame data we assume plain strike
   * range.
   */
  reachOf(moveId, fighter) {
    const data = this.moveData(moveId);
    if (data.range !== undefined && data.range !== null) return Number(data.range);
    return Number(data.reach ?? 1.0) * fighter.strikeRange;
  }

  /** Ticks from starting a move to its first hit. Unknown counts as slow. */
  startupOf(moveId) {
    return Math.trunc(Number(this.moveData(moveId).startup ?? 5));
  }

  /** A move's listed damage before attributes. Unknown counts as small. */
  damageOf(moveId) {
    return Number(this.moveData(moveId).damage ?? 5);
  }

  canAfford(moveId, fighter) {
    return fighter.stamina >= Number(this.moveData(moveId).stamina ?? 0);
  }

  lands(moveId, fighter, distance) {
    return distance <= this.reachOf(moveId, fighter);
  }

  // -------------------------------------------------------------------------
  // Gear-up: body shop, auction, fitting
  // -------------------------------------------------------------------------

  /** What one part adds, in attribute points, by Rusher's weights. */
  partValue(part) {
    return weighted(ATTRIBUTE_WEIGHTS, part.bonus) + weighted(EFFECT_VALUES, part.effects);
  }

  /**
   * The best part for each slot: for each slot type, the most valuable parts
   * of that type, one per slot. Parts worth nothing stay out. Returns a map
   * of slot index to part.
   */
  bestFitting(slots, parts) {
    const fitting = new Map();
    // Array.prototype.sort is stable, so equal values keep their order.
    const ranked = [...parts].sort((a, b) => this.partValue(b) - this.partValue(a));
    const used = new Set();
    slots.forEach((slotType, slotIndex) => {
      for (let i = 0; i < ranked.length; i++) {
        const part = ranked[i];
        if (!used.has(i) && part.slot === slotType && this.partValue(part) > 0) {
          fitting.set(slotIndex, part);
          used.add(i);
          break;
        }
      }
    });
    return fitting;
  }

  /** The total value of a fitting from {@link bestFitting}. */
  fittingValue(fitting) {
    let total = 0;
    for (const part of fitting.values()) total += this.partValue(part);
    return total;
  }

  chooseBody(state) {
    const credits = state.me.credits;
    let bestId = null;
    let bestScore = null;
    for (const body of state.offer) {
      if (body.cost > credits) continue;
      let score = weighted(ATTRIBUTE_WEIGHTS, body.stats);
      score += BODY_TASTE[body.id] ?? 0;
      // More slots are worth more, but only as many as the leftover credits
      // can plausibly fill.
      const fillable = Math.min(body.slots.length, (credits - body.cost) / PART_PRICE_GUESS);
      score += SLOT_VALUE * fillable;
      if (bestScore === null || score > bestScore) {
        bestId = body.id;
        bestScore = score;
      }
    }
    if (bestId === null) {
      // Nothing affordable: the cheapest is the least bad.
      bestId = state.offer.reduce((cheapest, b) => (b.cost < cheapest.cost ? b : cheapest)).id;
    }
    return Action.chooseBody(bestId);
  }

  bid(state) {
    const credits = state.me.credits;
    if (credits <= 0) return Action.bid(0);
    // What does this lot add to the best fitting of the parts already won?
    const slots = this.mySlots(state.me.body);
    const owned = state.me.owned.map((id) => this.knownPart(id)).filter((p) => p !== null);
    const before = this.fittingValue(this.bestFitting(slots, owned));
    const after = this.fittingValue(this.bestFitting(slots, [...owned, state.lot]));
    const gain = after - before;
    if (gain <= 0) return Action.bid(0);
    // Never bid more than the opponent could possibly beat.
    const cap = Math.min(credits, state.opp.credits + 1);
    const lotsLeft = state.lotsTotal - state.lotIndex;
    if (lotsLeft <= 1) return Action.bid(cap); // last lot: credits are worth nothing after it
    const fairShare = credits / lotsLeft;
    const amount = fairShare * (gain / TYPICAL_PART_VALUE) * BID_TEMPER;
    return Action.bid(Math.trunc(Math.max(0, Math.min(cap, amount))));
  }

  fit(state) {
    const fitting = this.bestFitting(state.slots, state.owned);
    const answer = {};
    for (const [slot, part] of fitting) answer[slot] = part.id;
    return Action.fit(answer);
  }

  /** A body's slot types, from the rule set's body table. */
  mySlots(bodyId) {
    const body = this.ruleSection("bodies")[bodyId ?? ""] ?? {};
    return Array.isArray(body.slots) ? [...body.slots] : [];
  }

  /** A part this seat owns, rebuilt from the rule set (the auction only names owned parts by id). */
  knownPart(partId) {
    const data = this.ruleSection("parts")[partId];
    if (!isObject(data)) return null;
    return {
      id: partId,
      slot: String(data.slot ?? ""),
      bonus: { ...(data.bonus ?? {}) },
      effects: { ...(data.effects ?? {}) },
    };
  }

  // -------------------------------------------------------------------------
  // The fight: one decision per tick
  // -------------------------------------------------------------------------

  act(state) {
    const { me, opp, distance } = state;

    // Busy with a move or stunned: the server ignores any answer.
    if (me.phase !== null) return Action.act("idle");

    // 1. The opponent is stuck (recovering, stunned, dazed, or mid-swing):
    //    hit it with the best move that lands before it can act again.
    if (["recovery", "hitstun", "dazed", "active"].includes(opp.phase) && opp.framesLeft) {
      let window = opp.framesLeft;
      if (opp.phase === "active") {
        window += Math.trunc(Number(this.moveData(opp.action ?? "").recovery ?? 0));
      }
      const punish = this.bestPunish(me, distance, window);
      if (punish !== null) return Action.act(punish);
      // Nothing reaches in time. Mid-swing it may still hit us: guard.
      // Otherwise walk in to be closer for the next chance.
      return Action.act(opp.phase === "active" ? "block" : "move_in");
    }

    // 2. The opponent is winding up a hidden move. Rusher does not wait to
    //    find out what it is: if its fastest move reaches, it strikes first.
    if (opp.phase === "startup") {
      const jab = this.fastestLandingAttack(me, distance);
      if (jab !== null) return Action.act(jab);
      return Action.act("block");
    }

    // 3. Both free. Spend the special as soon as it can do something.
    if (this.specialIsUseful(me, distance)) return Action.act("special");

    // Out of breath: step back and recover instead of swinging.
    if (me.stamina < STAMINA_FLOOR) {
      return Action.act(distance < opp.strikeRange ? "move_out" : "idle");
    }

    const attack = this.bestAttack(me, opp, distance);
    if (attack !== null) return Action.act(attack);

    // Nothing lands from here: walk in until we are in Rusher's range.
    const highKick = this.reachOf("kick_high", me);
    if (distance > PREFERRED_RANGE[1] * highKick) return Action.act("move_in");
    return Action.act("idle");
  }

  /**
   * Rusher's favourite attack from here: the most damage per tick of wind-up
   * (fast hands), plus its taste, and a throw against a blocker.
   */
  bestAttack(me, opp, distance) {
    let best = null;
    let bestScore = null;
    for (const move of ATTACKS) {
      if (!(this.canAfford(move, me) && this.lands(move, me, distance))) continue;
      let score = this.damageOf(move) / Math.max(1, this.startupOf(move));
      score += MOVE_TASTE[move] ?? 0;
      if (move === "throw" && opp.action === "block") score += 5.0; // throws go through a block
      if (bestScore === null || score > bestScore) {
        best = move;
        bestScore = score;
      }
    }
    return best;
  }

  fastestLandingAttack(me, distance) {
    const moves = ATTACKS.filter((m) => this.canAfford(m, me) && this.lands(m, me, distance));
    if (moves.length === 0) return null;
    return moves.reduce((fastest, m) => (this.startupOf(m) < this.startupOf(fastest) ? m : fastest));
  }

  /** The most damaging move that hits before `window` runs out. */
  bestPunish(me, distance, window) {
    const options = [...ATTACKS];
    if (this.specialKind(me) === "strike" && !me.specialUsed) options.push(me.special);
    let best = null;
    let bestDamage = 0.0;
    for (const move of options) {
      if (!(this.canAfford(move, me) && this.lands(move, me, distance))) continue;
      if (this.startupOf(move) >= window) continue;
      const damage = this.damageOf(move) + (MOVE_TASTE[move] ?? 0);
      if (damage > bestDamage) {
        best = move;
        bestDamage = damage;
      }
    }
    if (best !== null && best === me.special) return "special";
    return best;
  }

  specialKind(me) {
    return this.moveData(me.special).kind;
  }

  /**
   * Rusher fires its special early: a striking special as soon as it
   * reaches, a power-up as soon as it is close. A stance (a long-lasting
   * utility special, like a counter) is only worth it against a swing, so it
   * is left alone here.
   */
  specialIsUseful(me, distance) {
    if (me.specialUsed || !this.canAfford(me.special, me)) return false;
    const data = this.moveData(me.special);
    if (!isFilled(data)) return false;
    if (data.kind === "strike") return this.lands(me.special, me, distance);
    const isStance = Math.trunc(Number(data.active ?? 1)) >= 5;
    return !isStance && distance <= me.strikeRange * 1.2;
  }
}

/**
 * Entry point, called when the compiled image starts.
 *
 * The Chipzen platform injects `CHIPZEN_WS_URL` and `CHIPZEN_TOKEN` (or
 * `CHIPZEN_TICKET`) when it starts your container. To run this bot from your
 * own machine instead, use `chipzen-sdk run-external bot.js` (see the
 * README), which picks prod or staging for you.
 */
export async function main() {
  const url = process.env.CHIPZEN_WS_URL ?? process.argv[2];
  if (!url) {
    console.error("error: CHIPZEN_WS_URL not set and no URL passed on the command line");
    process.exit(1);
  }
  await runBot(url, new Rusher(), {
    token: process.env.CHIPZEN_TOKEN ?? null,
    ticket: process.env.CHIPZEN_TICKET ?? null,
  });
}

// Run main() when this file is the entry point: `node bot.js` (Node sets
// import.meta.url to the file:// URL of argv[1]) or the `bun build --compile`
// binary (Bun sets import.meta.main on the entry module). Importing this file
// from a test or from `chipzen-sdk validate` / `run-external` runs nothing.
if (import.meta.main || import.meta.url === `file://${process.argv[1]}`) {
  await main();
}
