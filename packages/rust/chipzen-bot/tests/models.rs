use chipzen_bot::{parse_card, parse_game_state, Action, ActionKind, Card};
use serde_json::json;

#[test]
fn card_parses_two_char_strings() {
    let c: Card = "Ah".parse().unwrap();
    assert_eq!(c.rank, 'A');
    assert_eq!(c.suit, 'h');
    assert_eq!(format!("{c}"), "Ah");

    assert_eq!(parse_card("Ts").unwrap().rank, 'T');
}

#[test]
fn card_rejects_malformed_input() {
    assert!("".parse::<Card>().is_err());
    assert!("A".parse::<Card>().is_err());
    assert!("Ahx".parse::<Card>().is_err());
    assert!("Xh".parse::<Card>().is_err()); // bad rank
    assert!("Az".parse::<Card>().is_err()); // bad suit
}

#[test]
fn action_to_wire_emits_canonical_shape() {
    let (kind, params) = Action::Fold.to_wire();
    assert_eq!(kind, "fold");
    assert_eq!(params, json!({}));

    let (kind, params) = Action::Raise(150).to_wire();
    assert_eq!(kind, "raise");
    assert_eq!(params, json!({ "amount": 150 }));

    let (kind, _) = Action::AllIn.to_wire();
    assert_eq!(kind, "all_in");
}

#[test]
fn action_kind_round_trips() {
    for kind in [
        ActionKind::Fold,
        ActionKind::Check,
        ActionKind::Call,
        ActionKind::Raise,
        ActionKind::AllIn,
    ] {
        assert_eq!(ActionKind::parse(kind.as_str()), Some(kind));
    }
    assert_eq!(ActionKind::parse("nonsense"), None);
}

#[test]
fn parse_game_state_translates_wire_format() {
    let msg = json!({
        "type": "turn_request",
        "round_id": "r_42",
        "request_id": "req_1",
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": 7,
            "phase": "flop",
            "your_hole_cards": ["Ah", "Kd"],
            "board": ["2c", "9s", "Tc"],
            "pot": 240,
            "your_stack": 9700,
            "opponent_stacks": [9540],
            "your_seat": 1,
            "dealer_seat": 0,
            "to_call": 60,
            "min_raise": 120,
            "max_raise": 9700,
            "action_history": [
                { "seat": 0, "action": "post_small_blind", "amount": 5 },
                { "seat": 1, "action": "post_big_blind", "amount": 10 },
                { "seat": 0, "action": "raise", "amount": 30 },
                { "seat": 1, "action": "call" },
            ],
        }
    });

    let state = parse_game_state(&msg);
    assert_eq!(state.hand_number, 7);
    assert_eq!(state.phase, "flop");
    assert_eq!(state.hole_cards.len(), 2);
    assert_eq!(state.hole_cards[0].rank, 'A');
    assert_eq!(state.board.len(), 3);
    assert_eq!(state.pot, 240);
    assert_eq!(state.your_stack, 9700);
    assert_eq!(state.opponent_stacks, vec![9540]);
    assert_eq!(state.to_call, 60);
    assert_eq!(state.min_raise, 120);
    assert_eq!(state.max_raise, 9700);
    assert_eq!(state.valid_actions, vec!["fold", "call", "raise"]);
    assert_eq!(state.action_history.len(), 4);
    assert_eq!(state.action_history[3].action, "call");
    assert_eq!(state.action_history[3].amount, None);
    assert_eq!(state.round_id, "r_42");
    assert_eq!(state.request_id, "req_1");
}

#[test]
fn parse_game_state_handles_missing_fields_with_safe_defaults() {
    // Worst-case message: `state` absent entirely. parse should not panic
    // and should produce a usable (if empty) GameState.
    let msg = json!({ "type": "turn_request", "request_id": "req_x" });
    let state = parse_game_state(&msg);
    assert_eq!(state.hand_number, 0);
    assert_eq!(state.phase, "preflop");
    assert!(state.hole_cards.is_empty());
    assert_eq!(state.request_id, "req_x");
    assert_eq!(state.round_id, "");
}

#[test]
fn parse_game_state_skips_unparseable_cards() {
    let msg = json!({
        "state": {
            "your_hole_cards": ["Ah", "garbage", "Kd"],
        }
    });
    let state = parse_game_state(&msg);
    // Only the two valid cards survive; the garbage one is dropped
    // rather than raising — robustness against an upstream wire bug.
    assert_eq!(state.hole_cards.len(), 2);
}
