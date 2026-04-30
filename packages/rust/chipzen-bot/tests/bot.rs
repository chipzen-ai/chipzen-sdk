use chipzen_bot::{Action, Bot, GameState};
use serde_json::Value;

/// Counts how many times each lifecycle hook fires. Lets us verify
/// the Bot trait has working default impls and that user code can
/// override only the hooks it cares about.
#[derive(Default)]
struct CountingBot {
    decide_calls: u32,
    match_start_calls: u32,
    round_start_calls: u32,
    round_result_calls: u32,
    match_end_calls: u32,
}

impl Bot for CountingBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        self.decide_calls += 1;
        Action::Fold
    }

    fn on_match_start(&mut self, _msg: &Value) {
        self.match_start_calls += 1;
    }

    fn on_round_start(&mut self, _msg: &Value) {
        self.round_start_calls += 1;
    }

    fn on_round_result(&mut self, _msg: &Value) {
        self.round_result_calls += 1;
    }

    fn on_match_end(&mut self, _results: &Value) {
        self.match_end_calls += 1;
    }
}

#[test]
fn bot_decide_is_invocable_directly() {
    let mut bot = CountingBot::default();
    let state = make_state();
    let action = bot.decide(&state);
    assert!(matches!(action, Action::Fold));
    assert_eq!(bot.decide_calls, 1);
}

#[test]
fn bot_lifecycle_hooks_are_invocable_directly() {
    let mut bot = CountingBot::default();
    let msg = serde_json::json!({});
    bot.on_match_start(&msg);
    bot.on_round_start(&msg);
    bot.on_round_result(&msg);
    bot.on_match_end(&msg);
    assert_eq!(bot.match_start_calls, 1);
    assert_eq!(bot.round_start_calls, 1);
    assert_eq!(bot.round_result_calls, 1);
    assert_eq!(bot.match_end_calls, 1);
}

#[test]
fn bot_default_hooks_are_no_ops() {
    // A bot that only implements `decide` should compile and the
    // hook calls should be no-ops without side effects.
    struct MinimalBot;
    impl Bot for MinimalBot {
        fn decide(&mut self, _state: &GameState) -> Action {
            Action::Check
        }
    }
    let mut bot = MinimalBot;
    let msg = serde_json::json!({});
    bot.on_match_start(&msg); // no-op
    bot.on_round_start(&msg); // no-op
    bot.on_phase_change(&msg); // no-op
    bot.on_turn_result(&msg); // no-op
    bot.on_round_result(&msg); // no-op
    bot.on_match_end(&msg); // no-op
    assert!(matches!(bot.decide(&make_state()), Action::Check));
}

fn make_state() -> GameState {
    GameState {
        hand_number: 1,
        phase: "preflop".to_string(),
        hole_cards: vec![],
        board: vec![],
        pot: 0,
        your_stack: 0,
        opponent_stacks: vec![],
        your_seat: 0,
        dealer_seat: 0,
        to_call: 0,
        min_raise: 0,
        max_raise: 0,
        valid_actions: vec!["fold".into(), "check".into()],
        action_history: vec![],
        round_id: String::new(),
        request_id: String::new(),
    }
}
