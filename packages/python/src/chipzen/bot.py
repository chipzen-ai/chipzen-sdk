"""Abstract base class for Chipzen poker bots.

Subclass ``ChipzenBot`` and implement ``decide()`` to create your bot.

The public developer-facing API is stable across protocol revisions.
``decide()`` still receives a :class:`GameState` regardless of whether the
SDK is talking to the old flat wire protocol or the new two-layer protocol.
Only the wire format and the optional lifecycle hooks change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from chipzen.models import Action, Card, GameState


class ChipzenBot(ABC):
    """Base class for all Chipzen poker bots.

    At minimum you must implement :meth:`decide`. The other lifecycle hooks
    are optional but useful for tracking state across hands.

    Example::

        from chipzen import Bot, GameState, Action

        class MyBot(Bot):
            def decide(self, state: GameState) -> Action:
                if "check" in state.valid_actions:
                    return Action.check()
                return Action.call()
    """

    @abstractmethod
    def decide(self, state: GameState) -> Action:
        """Return your action given the current game state.

        This is called every time the server asks your bot to act.
        You must respond quickly -- the server enforces a 5000ms timeout
        by default (announced in the ``match_start.turn_timeout_ms`` field).

        Args:
            state: The current game state including your hole cards,
                   the board, pot size, and valid actions.

        Returns:
            The action to take.
        """
        ...

    # ------------------------------------------------------------------
    # Match-level hooks
    # ------------------------------------------------------------------

    def on_match_start(self, match_info: dict) -> None:
        """Called once when the match begins.

        Override this to initialize any match-level state. ``match_info`` is
        the full ``match_start`` message including seat assignments and the
        nested ``game_config`` (blinds, starting stack, hand count).

        Args:
            match_info: The full ``match_start`` message.
        """

    def on_match_end(self, results: dict) -> None:
        """Called once when the match ends.

        Args:
            results: The full ``match_end`` message with final standings.
        """

    # ------------------------------------------------------------------
    # Round (hand) hooks
    # ------------------------------------------------------------------

    def on_round_start(self, message: dict) -> None:
        """Called at the start of each round (hand) with the raw message.

        Override this if you need access to the full Layer 1 envelope
        (``round_id``, ``round_number``) or the nested Layer 2 ``state``
        payload (``deck_commitment``, per-seat ``stacks``).

        The default implementation delegates to :meth:`on_hand_start` for
        backward compatibility.
        """
        state = message.get("state", {}) or {}
        hand_number = int(state.get("hand_number", 0))
        hole_strs = state.get("your_hole_cards", [])
        hole_cards = [Card.from_str(c) for c in hole_strs]
        self.on_hand_start(hand_number, hole_cards)

    def on_hand_start(self, hand_number: int, hole_cards: list[Card]) -> None:
        """Called at the start of each hand.

        Override this to do per-hand setup, reset trackers, etc.

        Args:
            hand_number: Which hand number in the match.
            hole_cards: Your two private cards.
        """

    def on_round_result(self, message: dict) -> None:
        """Called after each round (hand) with the raw message.

        Override this if you need the full Layer 1 envelope plus the nested
        Layer 2 ``result`` payload (``winner_seats``, ``pot``, ``payouts``,
        ``showdown``, ``action_history``, ``deck_reveal``).

        The default implementation delegates to :meth:`on_hand_result` with
        the flattened result object for backward compatibility.
        """
        result = dict(message.get("result", {}) or {})
        # Hoist the Layer 1 round_id so legacy bots that inspect it still work.
        if "round_id" in message and "round_id" not in result:
            result["round_id"] = message["round_id"]
        self.on_hand_result(result)

    def on_hand_result(self, result: dict) -> None:
        """Called with hand results after each hand completes.

        Override this to track opponent tendencies, win rates, etc.

        Args:
            result: The hand result payload. In the two-layer protocol this
                is the ``round_result.result`` object; in the legacy flat
                protocol (and in the local test harness) it is the full
                ``hand_result`` message.
        """

    # ------------------------------------------------------------------
    # Optional hooks for extra protocol signals
    # ------------------------------------------------------------------

    def on_phase_change(self, message: dict) -> None:
        """Called when a new phase begins within a hand (flop/turn/river).

        Default implementation is a no-op. Override to refresh internal
        board tracking or trigger planning steps.
        """

    def on_turn_result(self, message: dict) -> None:
        """Called after any participant's action is broadcast.

        Default implementation is a no-op. Override to track opponent
        action frequencies, timing patterns, etc.
        """
