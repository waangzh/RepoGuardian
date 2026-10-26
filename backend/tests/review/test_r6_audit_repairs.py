from app.evaluation.runner import final_audit


def event(stage, identity, unit="u", status="candidate", **kwargs):
    return {"stage": stage, "issue_id": identity, "review_unit_id": unit, "status": status,
            "reason": None, "canonical_id": None, **kwargs}


def test_schema_accepted_without_real_candidate_is_explicitly_unclosed():
    events = [event("schema_validation", "schema", canonical_id="lost", reason="schema_accepted")]
    final = final_audit(events, {}, "failed")[-1]
    assert final["status"] == "not_published"
    assert final["reason"] == "canonical_lifecycle_not_observed"
    assert final["canonical_id"] == "lost"


def test_followup_identity_inherits_dismissed_target_across_units():
    events = [event("schema_validation", "schema", canonical_id="old"),
              event("generated", "old"),
              event("identity", "old", canonical_id="new", canonical_review_unit_id="followup"),
              event("identity_assigned", "new", "followup"),
              event("verifier", "new", "followup", "dismissed", reason="counterevidence")]
    finals = {item["issue_id"]: item for item in final_audit(events, {}, "completed") if item["stage"] == "final"}
    for identity in ("schema", "old", "new"):
        assert finals[identity]["status"] == "dismissed"
        assert finals[identity]["reason"] == "counterevidence"
    assert finals["old"]["canonical_review_unit_id"] == "followup"


def test_alias_cannot_inherit_a_synthetic_audit_gap_as_success():
    events = [event("schema_validation", "schema", canonical_id="lost")]
    state = {"review_issues": [{"id": "lost", "review_unit_id": "u", "status": "confirmed"}]}
    final = next(item for item in final_audit(events, state, "completed")
                 if item["stage"] == "final" and item["issue_id"] == "schema")
    assert final["status"] == "not_published"
    assert final["canonical_status"] is None
