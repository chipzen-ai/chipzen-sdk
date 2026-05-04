use chipzen_bot::{
    run_conformance_checks, Action, Bot, ConformanceSeverity, GameState, RunConformanceOptions,
    SCENARIO_NAMES,
};

struct CallBot;
impl Bot for CallBot {
    fn decide(&mut self, state: &GameState) -> Action {
        if state.valid_actions.iter().any(|a| a == "call") {
            Action::Call
        } else if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

struct ThrowingBot;
impl Bot for ThrowingBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        // Returning a non-legal action is the closest analog to "decide
        // panicked" that exercises _run_session's fallback path
        // without requiring panic catching.
        Action::Raise(99_999_999)
    }
}

#[tokio::test]
async fn passes_when_bot_completes_canned_full_match() {
    let results = run_conformance_checks(CallBot, RunConformanceOptions::default()).await;
    // 4 scenarios now: full_match, multi_turn, action_rejected, retry_storm.
    assert_eq!(results.len(), 4);

    let full_match = results
        .iter()
        .find(|r| r.name == "connectivity_full_match")
        .expect("full_match scenario missing");
    assert_eq!(full_match.severity, ConformanceSeverity::Pass);
    assert!(
        full_match.message.contains("handshake + 1 hand + match_end"),
        "unexpected message: {}",
        full_match.message
    );
    assert!(
        full_match.message.contains("turn_action: action=\"call\""),
        "unexpected message: {}",
        full_match.message
    );

    // None of the four scenarios should fail for a well-behaved bot.
    let fails: Vec<_> = results
        .iter()
        .filter(|r| r.severity == ConformanceSeverity::Fail)
        .collect();
    assert!(
        fails.is_empty(),
        "Unexpected failures: {:?}",
        fails
            .iter()
            .map(|r| (&r.name, &r.message))
            .collect::<Vec<_>>()
    );
}

#[tokio::test]
async fn passes_when_bot_returns_invalid_action_thanks_to_safe_fallback() {
    // The session loop substitutes a safe fallback when decide() picks
    // an action that's not in valid_actions, so the protocol exchange
    // still completes cleanly. Conformance verifies SDK plumbing, not
    // user-code happiness — this is expected to pass on every scenario.
    let results = run_conformance_checks(ThrowingBot, RunConformanceOptions::default()).await;
    assert_eq!(results.len(), 4);
    let fails: Vec<_> = results
        .iter()
        .filter(|r| r.severity == ConformanceSeverity::Fail)
        .collect();
    assert!(
        fails.is_empty(),
        "Expected SDK fallback to keep the wire exchange green even when decide returns \
         invalid actions; got fails: {:?}",
        fails
            .iter()
            .map(|r| (&r.name, &r.message))
            .collect::<Vec<_>>()
    );
}

#[tokio::test]
async fn registers_all_four_scenarios() {
    assert_eq!(
        SCENARIO_NAMES,
        &[
            "connectivity_full_match",
            "multi_turn_request_id_echo",
            "action_rejected_recovery",
            "retry_storm_bounded",
        ]
    );
}

#[tokio::test]
async fn multi_turn_scenario_passes_for_well_behaved_bot() {
    let results = run_conformance_checks(CallBot, RunConformanceOptions::default()).await;
    let multi_turn = results
        .iter()
        .find(|r| r.name == "multi_turn_request_id_echo")
        .expect("multi_turn scenario missing");
    assert_eq!(
        multi_turn.severity,
        ConformanceSeverity::Pass,
        "multi_turn message: {}",
        multi_turn.message
    );
    assert!(
        multi_turn.message.contains("3 turn_actions"),
        "expected scenario summary to mention 3 turn_actions, got: {}",
        multi_turn.message
    );
}

#[tokio::test]
async fn action_rejected_scenario_verifies_safe_fallback_with_original_request_id() {
    let results = run_conformance_checks(CallBot, RunConformanceOptions::default()).await;
    let recovery = results
        .iter()
        .find(|r| r.name == "action_rejected_recovery")
        .expect("action_rejected_recovery scenario missing");
    assert_eq!(
        recovery.severity,
        ConformanceSeverity::Pass,
        "action_rejected_recovery message: {}",
        recovery.message
    );
    // Pass message should name the safe action used (check or fold).
    assert!(
        recovery.message.contains("\"check\"") || recovery.message.contains("\"fold\""),
        "expected pass message to identify the safe-fallback action; got: {}",
        recovery.message
    );
    assert!(
        recovery.message.contains("original request_id"),
        "expected pass message to confirm request_id was preserved; got: {}",
        recovery.message
    );
}

#[tokio::test]
async fn retry_storm_scenario_confirms_four_turn_actions_total() {
    let results = run_conformance_checks(CallBot, RunConformanceOptions::default()).await;
    let storm = results
        .iter()
        .find(|r| r.name == "retry_storm_bounded")
        .expect("retry_storm_bounded scenario missing");
    assert_eq!(
        storm.severity,
        ConformanceSeverity::Pass,
        "retry_storm message: {}",
        storm.message
    );
    // 1 initial + 3 retries = 4 turn_actions total.
    assert!(
        storm.message.contains("4 turn_actions"),
        "expected pass message to confirm 4-turn-action total; got: {}",
        storm.message
    );
    assert!(
        storm.message.contains("exited cleanly on match_end"),
        "expected pass message to confirm clean exit; got: {}",
        storm.message
    );
}
