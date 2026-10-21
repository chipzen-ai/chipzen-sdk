"""Big Bad Bots rule sets: ``state.rules``, and the published rule sets the SDK bundles.

Every match is played under exactly one **rule set**, the named set of numbers
the game runs on: the budget, the bodies and parts, the move frame data, the
arena, the round length, the per-tick deadline. ``match_start.game_config``
names it (``rule_set_id``, e.g. ``"bigbadbots-1.2"``) and carries its full
parameter table (``rule_set``). A balance patch is a new rule set with new
numbers, so a bot that reads its numbers from the rule set keeps working
unchanged when the rule set changes.

- :class:`RuleSet` is that table, typed and read-only. A
  :class:`chipzen.BigBadBotsBot` builds it at ``match_start`` and hands it to
  every call as ``state.rules`` (and keeps it as ``bot.rules``).
- :func:`published_rule_set` looks up a released rule set's published numbers
  offline, from the files bundled with the SDK (``chipzen/rulesets/``).

**The server's numbers always win.** The bundled files are a reference; the
SDK never plays a bundled value in place of one ``match_start`` sent. A
``rule_set_id`` this release does not know, or a bundled value that differs
from the server's, is reported by :func:`rule_set_warnings` (which
:class:`chipzen.BigBadBotsBot` logs), and the bot plays on.

The id scheme, what a minor or major version may change, the lifecycle
states and the changelog are in ``docs/RULESETS.md``:
https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from chipzen.bigbadbots import (
    BigBadBotsGameConfig,
    StateParseError,
    _bool,
    _int,
    _number,
    _number_list,
    _object,
    _str,
)

#: Where the rule-set spec lives: the id scheme, the lifecycle and the changelog.
RULESETS_URL = "https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md"

#: The bundled published rule sets, one JSON file per rule set.
RULESET_DIR = Path(__file__).parent / "rulesets" / "bigbadbots"

#: ``rounds.timeout_tiebreak`` when a rule set leaves it out (``bigbadbots-1.0``):
#: at the tick limit the higher share of max HP wins (protocol section 6.2).
DEFAULT_TIMEOUT_TIEBREAK = "hp_pct"


def _freeze(value: Any) -> Any:
    """A read-only deep copy: objects become mappings, arrays become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """The JSON form of a :func:`_freeze` result."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _opt(obj: Mapping[str, Any], key: str, where: str, reader: Any) -> Any:
    """An optional rule-set key: ``None`` when the rule set leaves it out."""
    return reader(obj, key, where) if key in obj else None


# ---------------------------------------------------------------------------
# The typed sections (docs/RULESETS.md section 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GearUpRules:
    """``rule_set.gearup``: the budget and the size of the body shop and the auction.

    Attributes:
        budget: Credits each seat starts with, for the body and the auction.
        body_offer_size: How many bodies each seat is offered.
        auction_lots: How many parts the auction sells.
    """

    budget: int
    body_offer_size: int
    auction_lots: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "rule_set.gearup") -> GearUpRules:
        return cls(
            budget=_int(data, "budget", where),
            body_offer_size=_int(data, "body_offer_size", where),
            auction_lots=_int(data, "auction_lots", where),
        )


@dataclass(frozen=True, slots=True)
class ArenaRules:
    """``rule_set.arena``: the one-dimensional arena.

    Attributes:
        width: Distance between the walls, in arena units.
        start_x: Each seat's start position, by seat.
        start_jitter: The most the start positions shift each round (both by
            the same seeded amount).
        min_separation: The fighters never stand closer than this.
    """

    width: float
    start_x: tuple[float, ...]
    start_jitter: float
    min_separation: float

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "rule_set.arena") -> ArenaRules:
        return cls(
            width=_number(data, "width", where),
            start_x=tuple(_number_list(data, "start_x", where)),
            start_jitter=_number(data, "start_jitter", where),
            min_separation=_number(data, "min_separation", where),
        )


@dataclass(frozen=True, slots=True)
class RoundRules:
    """``rule_set.rounds``: ticks, round length and how a match is won.

    Attributes:
        ticks_per_second: Fight ticks per second.
        round_ticks: The most ticks a round lasts.
        rounds_to_win: Round wins that take the match.
        max_rounds: The most rounds a match plays, drawn rounds included.
        timeout_tiebreak: Who wins a round that reaches ``round_ticks``:
            ``"hp_pct"`` (the higher share of max HP; also what a rule set
            that leaves the key out plays) or ``"damage"`` (more damage dealt
            this round).
        stall_close_after: Closing walls (optional; ``None`` when the rule set
            has none): ticks with no damage before the walls start to close.
        stall_close_rate: How far each wall moves in per tick while closing.
        stall_min_width: The walls stop closing this far apart.
    """

    ticks_per_second: int
    round_ticks: int
    rounds_to_win: int
    max_rounds: int
    timeout_tiebreak: str
    stall_close_after: int | None
    stall_close_rate: float | None
    stall_min_width: float | None

    @property
    def closing_walls(self) -> bool:
        """Whether the walls close in on a stalled round (protocol section 6.2)."""
        return self.stall_close_after is not None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "rule_set.rounds") -> RoundRules:
        tiebreak = _opt(data, "timeout_tiebreak", where, _str)
        return cls(
            ticks_per_second=_int(data, "ticks_per_second", where),
            round_ticks=_int(data, "round_ticks", where),
            rounds_to_win=_int(data, "rounds_to_win", where),
            max_rounds=_int(data, "max_rounds", where),
            timeout_tiebreak=DEFAULT_TIMEOUT_TIEBREAK if tiebreak is None else tiebreak,
            stall_close_after=_opt(data, "stall_close_after", where, _int),
            stall_close_rate=_opt(data, "stall_close_rate", where, _number),
            stall_min_width=_opt(data, "stall_min_width", where, _number),
        )


@dataclass(frozen=True, slots=True)
class ForfeitRules:
    """``rule_set.forfeit``: disconnects and forfeits (protocol section 3.9).

    Attributes:
        grace_seconds: Seconds of missed ticks before a seat forfeits the round.
        rounds_for_match: Forfeited rounds that forfeit the match.
    """

    grace_seconds: float
    rounds_for_match: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "rule_set.forfeit") -> ForfeitRules:
        return cls(
            grace_seconds=_number(data, "grace_seconds", where),
            rounds_for_match=_int(data, "rounds_for_match", where),
        )


@dataclass(frozen=True, slots=True)
class DeadlineRules:
    """``rule_set.deadline``: the per-tick deadline and the time bank (sections 3.4, 3.5).

    Attributes:
        deadline_ms: The per-tick deadline. Answer inside it and the time bank
            is never touched.
        time_bank_ms: Each seat's time bank per round, reset every round.
        default_action_policy: What the server plays on a missed tick.
        late_answer: What happens to an answer that arrives too late.
        miss_counts_toward_forfeit: Whether missed ticks count toward the
            disconnect forfeit (``forfeit.grace_seconds``).
    """

    deadline_ms: int
    time_bank_ms: int
    default_action_policy: str
    late_answer: str
    miss_counts_toward_forfeit: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "rule_set.deadline") -> DeadlineRules:
        return cls(
            deadline_ms=_int(data, "deadline_ms", where),
            time_bank_ms=_int(data, "time_bank_ms", where),
            default_action_policy=_str(data, "default_action_policy", where),
            late_answer=_str(data, "late_answer", where),
            miss_counts_toward_forfeit=_bool(data, "miss_counts_toward_forfeit", where),
        )


# ---------------------------------------------------------------------------
# The rule set a match is played under
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class RuleSet:
    """The rule set this match is played under, as ``match_start`` sent it. Read-only.

    The sections every bot needs are typed (:attr:`gearup`, :attr:`arena`,
    :attr:`rounds`, :attr:`forfeit`, :attr:`deadline`). Every section,
    including the bodies, parts, moves and specials, is in :attr:`params`,
    and ``rules["moves"]`` is short for ``rules.params["moves"]``.

    Read every number from here and never hard-code one: a new rule set
    changes numbers, not code, so a bot that does keeps working unchanged.
    The rule-set spec: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md

    Attributes:
        id: The rule set's id, ``game_config.rule_set_id`` (e.g.
            ``"bigbadbots-1.2"``). Fixed for the whole match.
        params: The full parameter table, ``game_config.rule_set``, as a
            read-only copy: objects are read-only mappings and arrays are
            tuples. ``game_config.rule_set`` keeps the wire form.
        gearup: ``params["gearup"]``, typed.
        arena: ``params["arena"]``, typed.
        rounds: ``params["rounds"]``, typed.
        forfeit: ``params["forfeit"]``, typed.
        deadline: ``params["deadline"]``, typed.
    """

    id: str
    params: Mapping[str, Any]
    gearup: GearUpRules
    arena: ArenaRules
    rounds: RoundRules
    forfeit: ForfeitRules
    deadline: DeadlineRules

    @classmethod
    def from_rule_set(cls, rule_set_id: str, rule_set: Mapping[str, Any]) -> RuleSet:
        """Build from a ``rule_set_id`` and its parameter table.

        Raises:
            StateParseError: one of the typed sections is missing, or one of
                its keys is missing or mistyped.
        """
        where = "rule_set"
        return cls(
            id=rule_set_id,
            params=_freeze(rule_set),
            gearup=GearUpRules.from_dict(_object(rule_set, "gearup", where)),
            arena=ArenaRules.from_dict(_object(rule_set, "arena", where)),
            rounds=RoundRules.from_dict(_object(rule_set, "rounds", where)),
            forfeit=ForfeitRules.from_dict(_object(rule_set, "forfeit", where)),
            deadline=DeadlineRules.from_dict(_object(rule_set, "deadline", where)),
        )

    @classmethod
    def from_game_config(cls, config: BigBadBotsGameConfig) -> RuleSet:
        """Build from a parsed ``match_start.game_config``."""
        return cls.from_rule_set(config.rule_set_id, config.rule_set)

    def __getitem__(self, section: str) -> Any:
        return self.params[section]

    def __contains__(self, section: object) -> bool:
        return section in self.params

    @property
    def published(self) -> PublishedRuleSet | None:
        """This rule set's bundled published entry, or ``None`` for an id this release lacks."""
        return published_rule_set(self.id)

    def to_dict(self) -> dict[str, Any]:
        """The parameter table in its wire form, ``game_config.rule_set``."""
        return _thaw(self.params)


# ---------------------------------------------------------------------------
# The published rule sets bundled with the SDK
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class PublishedRuleSet:
    """A released rule set as published in ``docs/RULESETS.md``, bundled with the SDK.

    See :func:`published_rule_set`. The spec:
    https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md

    Attributes:
        id: The rule set's id.
        state: Its lifecycle state when this SDK release was cut:
            ``"preview"``, ``"live"``, ``"deprecated"`` or ``"retired"``.
            The platform's current state can be newer.
        engine: The engine release it was frozen with.
        frozen_on: The date it was frozen, ``YYYY-MM-DD`` (UTC).
        sha256: The sha256 of its canonical content, as published.
        sections: The sections of its parameter table whose numbers are
            published, read-only (``gearup``, ``arena``, ``rounds``,
            ``forfeit``, ``deadline`` so far). The rest are added as their
            tables are published; the full table always comes with
            ``match_start``.
    """

    id: str
    state: str
    engine: str
    frozen_on: str
    sha256: str
    sections: Mapping[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "rule set file") -> PublishedRuleSet:
        return cls(
            id=_str(data, "rule_set_id", where),
            state=_str(data, "state", where),
            engine=_str(data, "engine", where),
            frozen_on=_str(data, "frozen_on", where),
            sha256=_str(data, "sha256", where),
            sections=_freeze(_object(data, "rule_set", where)),
        )


@functools.lru_cache(maxsize=None)
def _bundled() -> Mapping[str, PublishedRuleSet]:
    entries = {}
    for path in sorted(RULESET_DIR.glob("*.json")):
        entry = PublishedRuleSet.from_dict(
            json.loads(path.read_text(encoding="utf-8")), where=path.name
        )
        entries[entry.id] = entry
    return MappingProxyType(entries)


def published_rule_set_ids() -> tuple[str, ...]:
    """The ids of every published rule set this SDK release bundles, sorted."""
    return tuple(sorted(_bundled()))


def published_rule_set(rule_set_id: str) -> PublishedRuleSet | None:
    """A released rule set's published numbers, from the files bundled with the SDK.

    Works offline. Returns ``None`` for an id this SDK release does not know:
    a rule set released after it, or a local or test id such as
    ``"bigbadbots-1.1-dev"``. Never a substitute for the numbers
    ``match_start`` sends; see https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
    """
    return _bundled().get(rule_set_id)


def _differences(published: Any, played: Any, path: str) -> Iterator[str]:
    if isinstance(published, Mapping):
        for key, value in published.items():
            where = f"{path}.{key}"
            if not isinstance(played, Mapping) or key not in played:
                yield f"{where} is missing (published: {_thaw(value)!r})"
            else:
                yield from _differences(value, played[key], where)
    elif published != played:
        yield f"{path} is {_thaw(played)!r} (published: {_thaw(published)!r})"


def rule_set_warnings(rules: RuleSet) -> list[str]:
    """What a bot should be warned about in the rule set ``match_start`` sent.

    - The ``rule_set_id`` is not one this SDK release bundles (a newer rule
      set, or a local or test id).
    - A published value differs from the one ``match_start`` sent.

    Either way the bot plays on the values ``match_start`` sent: these are
    warnings, never errors, and nothing is substituted. Returns ``[]`` when
    there is nothing to report.
    """
    published = published_rule_set(rules.id)
    if published is None:
        known = ", ".join(published_rule_set_ids()) or "none"
        return [
            f"rule set {rules.id!r} is not one this SDK release knows (it knows: {known}); "
            f"playing on the values match_start sent. Upgrade chipzen-bot for its "
            f"published numbers, and see {RULESETS_URL}"
        ]
    return [
        f"rule set {rules.id!r}: match_start's {difference}; playing on match_start's value"
        for difference in _differences(published.sections, rules.params, "rule_set")
    ]


__all__ = [
    "DEFAULT_TIMEOUT_TIEBREAK",
    "RULESETS_URL",
    "RULESET_DIR",
    "ArenaRules",
    "DeadlineRules",
    "ForfeitRules",
    "GearUpRules",
    "PublishedRuleSet",
    "RoundRules",
    "RuleSet",
    "StateParseError",
    "published_rule_set",
    "published_rule_set_ids",
    "rule_set_warnings",
]
