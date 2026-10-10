from app.review.review_policy import (
    REVIEW_STEP_OUTPUT_TOKENS,
    REVIEW_STEP_PROTOCOL_VERSION,
    input_composition,
)


def test_review_step_policy_has_single_output_default_and_input_metrics():
    metrics = input_composition({
        "diff_evidence": [{"content": "diff"}],
        "readonly_context": [{"content": "evidence"}],
        "working_memory": {"target_checks": []},
        "pr_intent": "intent",
    })
    assert REVIEW_STEP_PROTOCOL_VERSION == "review-step-v1"
    assert REVIEW_STEP_OUTPUT_TOKENS == 4096
    assert metrics["diff_chars"] > 0 and metrics["evidence_chars"] > 0
