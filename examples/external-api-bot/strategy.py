"""Trivial poker strategy for the External-API reference bot.

Kept as a pure, side-effect-free module so it is unit-testable without any
WebSocket plumbing. :func:`decide` takes a Layer-2 ``turn_request.state`` dict
plus the ``valid_actions`` list and returns ``{"action": ..., "params": ...}``.

The strategy is deliberately minimal — call when cheap, otherwise check, else
fold — because this reference client exists to demonstrate the *protocol*, not
to play well. See ``docs/EXTERNAL-API-BOT-PROTOCOL.md`` §6 for the protocol the
returned action is wrapped into.
"""

from __future__ import annotations


def decide(state: dict, valid_actions: list[str]) -> dict:
    """Pick an action from a Layer-2 ``turn_request`` state.

    Strategy (trivial, by design):

    * If checking is free, check.
    * Otherwise, call when the amount to call is cheap relative to the pot
      (``to_call <= half the pot``), else fold.
    * Fall back to whatever legal action remains (the server always offers
      ``check`` or ``fold``, so this is just belt-and-suspenders).

    Args:
        state: the ``state`` object from a ``turn_request`` frame. Reads
            ``to_call`` and ``pot`` (both integers; treated as ``0`` when
            absent so a sparse/partial state never raises).
        valid_actions: the ``valid_actions`` list from the same frame — the
            set of legal action strings for this turn.

    Returns:
        ``{"action": <str>, "params": <dict>}``. ``params`` is always present
        (empty for non-``raise`` actions) so the caller can splat it into a
        ``turn_action`` frame unconditionally.
    """

    def has(action: str) -> bool:
        return action in valid_actions

    to_call = _as_int(state.get("to_call"))
    pot = _as_int(state.get("pot"))

    # Free to check -> check.
    if to_call <= 0 and has("check"):
        return {"action": "check", "params": {}}

    # Cheap to call -> call. "Cheap" = at most half the current pot. Guard the
    # pot==0 edge so a 0-pot, nonzero-to_call spot doesn't auto-call.
    if has("call") and to_call > 0 and to_call <= pot // 2:
        return {"action": "call", "params": {}}

    # Otherwise fold if we can.
    if has("fold"):
        return {"action": "fold", "params": {}}

    # Belt-and-suspenders: the server's auto-action policy guarantees one of
    # check/fold is always legal, but if we somehow get here, prefer the
    # cheapest legal action.
    if has("check"):
        return {"action": "check", "params": {}}
    if has("call"):
        return {"action": "call", "params": {}}
    # Last resort: echo the first legal action with empty params.
    return {"action": valid_actions[0] if valid_actions else "fold", "params": {}}


def _as_int(value: object) -> int:
    """Coerce a possibly-missing/possibly-stringy numeric field to int.

    Layer-2 state fields are integers, but a defensive coercion keeps the
    strategy robust against a sparse or unexpectedly-typed ``state`` payload
    (returns ``0`` for ``None`` / unparseable values rather than raising).
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
