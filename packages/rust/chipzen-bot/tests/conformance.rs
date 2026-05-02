use chipzen_bot::{
    run_conformance_checks, Action, Bot, ConformanceSeverity, GameState, RunConformanceOptions,
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
    assert_eq!(results.len(), 1);
    let check = &results[0];
    assert_eq!(check.severity, ConformanceSeverity::Pass);
    assert_eq!(check.name, "connectivity_full_match");
    assert!(
        check.message.contains("handshake + 1 hand + match_end"),
        "unexpected message: {}",
        check.message
    );
    assert!(
        check.message.contains("turn_action: action=\"call\""),
        "unexpected message: {}",
        check.message
    );
}

#[tokio::test]
async fn passes_when_bot_returns_invalid_action_thanks_to_safe_fallback() {
    // The session loop substitutes a safe fallback when decide() picks
    // an action that's not in valid_actions, so the protocol exchange
    // still completes cleanly. Conformance verifies SDK plumbing, not
    // user-code happiness — this is expected to pass.
    let results = run_conformance_checks(ThrowingBot, RunConformanceOptions::default()).await;
    let check = &results[0];
    assert_eq!(check.severity, ConformanceSeverity::Pass);
    assert!(
        check.message.contains("turn_action: action="),
        "expected a turn_action diagnostic, got: {}",
        check.message
    );
}
