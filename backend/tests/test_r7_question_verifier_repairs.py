import pytest

from app.models.review import (IssueMetrics, IssueVerification, ModelCallResult, ModelUsage,
    UnitReviewRecord)
from app.services.issue_verifier import IssueVerifierService
from app.services.review_input_context import build_working_memory
from app.services.unit_review_summary import validate_record, merge_review_summaries
from test_issue_validation import _issue, _unit, _state
from test_unit_review_summary import _input


def closed_question_fixture(active_question=False):
    request = _input()
    evidence = request["evidence"][0]["id"]
    first = validate_record(UnitReviewRecord(change_summary="发现问题", unresolved_questions=[{
        "question": "调用方是否检查空值", "affected_files": ["a.py"], "evidence_ids": [evidence]}]), request, {"a.py"})
    memory, _ = build_working_memory(first, request, "u")
    request["working_memory"] = memory
    identity = first.record.unresolved_questions[0].id
    transition = {"question_id": identity, "status": "resolved", "reason": "核验调用方",
                  "evidence_ids": [evidence], "replacement_id": None}
    active = [{"question": "另一个契约仍待核验", "affected_files": ["a.py"], "evidence_ids": [evidence]}] if active_question else []
    declared = UnitReviewRecord(change_summary="检查完成", question_updates=[transition], unresolved_questions=active)
    second = validate_record(declared, request, {"a.py"})
    assert second.status == "reported"
    merged = merge_review_summaries(first, second, declared)
    request["working_memory"], _ = build_working_memory(merged, request, "u")
    assert request["working_memory"]["closed_question_updates"] == [transition]
    return request, transition


def test_exact_closed_transition_is_an_idempotent_noop():
    request, transition = closed_question_fixture()
    result = validate_record(UnitReviewRecord(change_summary="检查完成", question_updates=[transition]), request, {"a.py"})
    assert result.status == "reported"
    assert result.record.question_updates == [] and result.record.unresolved_questions == []
    assert transition["question_id"] in request["working_memory"]["allowed_question_update_ids"]


def test_closed_noop_does_not_retire_a_different_active_question():
    request, transition = closed_question_fixture(active_question=True)
    active = request["working_memory"]["unresolved_questions"]
    result = validate_record(UnitReviewRecord(change_summary="检查", question_updates=[transition],
        unresolved_questions=active), request, {"a.py"})
    assert result.status == "reported" and result.record.question_updates == []
    assert [question.id for question in result.record.unresolved_questions] == [active[0]["id"]]


@pytest.mark.parametrize("change", ["reason", "snapshot", "evidence", "catalog", "reuse_identity"])
def test_closed_transition_cannot_change_evidence_snapshot_or_question(change):
    request, transition = closed_question_fixture()
    question = []
    if change == "reason":
        transition["reason"] = "不同主张"
    elif change == "snapshot":
        request["snapshot"]["head_sha"] = "new-head"
    elif change == "evidence":
        transition["evidence_ids"] = ["not-shown"]
    elif change == "catalog":
        request["working_memory"].pop("closed_question_updates")
    else:
        question = [{"id": transition["question_id"], "question": "另一个问题", "affected_files": ["a.py"]}]
    result = validate_record(UnitReviewRecord(change_summary="检查", question_updates=[transition],
                                             unresolved_questions=question), request, {"a.py"})
    assert result.latest_attempt_status == "invalid" and result.record is None


class VerifiedProvider:
    def __init__(self, decision="keep", failure=False, usage_available=True):
        self.calls = 0
        self.decision = decision
        self.failure = failure
        self.usage_available = usage_available

    async def verify_issue(self, request, model):
        self.calls += 1
        if self.failure and self.calls == 1:
            raise RuntimeError("offline failure")
        return ModelCallResult(IssueVerification(issue_id=request.issue.id, decision=self.decision, reason="独立核验"),
            ModelUsage(provider="fixture", model="fixture", operation="issue_verifier", latency_ms=1,
                       usage_available=self.usage_available, actual_input_tokens=10, actual_output_tokens=5, actual_total_tokens=15))


async def test_identical_verification_inputs_reuse_decision_with_independent_ids_and_single_usage():
    provider = VerifiedProvider()
    unit = _unit()
    first = _issue("one")
    second = first.model_copy(update={"id": "two", "source_issue_ids": ["two"]}, deep=True)
    service = IssueVerifierService(provider, enabled=True, fail_mode="candidate", max_calls_per_unit=1)
    from app.review.issue_audit import capture_issue_audit
    events = []
    with capture_issue_audit(events.append):
        result = await service.verify_issues([first, second], [unit], _state(unit), IssueMetrics())
    assert provider.calls == 1 and result.metrics.verifier_call_count == 1
    assert [decision.issue_id for decision in result.verifications] == ["one", "two"]
    assert [issue.status.value for issue in result.issues] == ["confirmed", "confirmed"]
    assert len(result.model_usages) == 1
    assert events[0]["stage"] == "verifier_reuse" and events[0]["issue_id"] == "two"
    assert events[0]["reason"] == "identical_verified_input:one"
    await service.verify_issues([first], [unit], _state(unit), IssueMetrics())
    assert provider.calls == 2  # 另一次批次不共享缓存。


@pytest.mark.parametrize("decision,failure,usage_available", [("unresolved", False, True),
    ("keep", True, True), ("keep", False, False)])
async def test_unresolved_failed_or_unknown_usage_verification_is_never_reused(decision, failure, usage_available):
    provider = VerifiedProvider(decision, failure, usage_available)
    unit, first = _unit(), _issue("one")
    second = first.model_copy(update={"id": "two", "source_issue_ids": ["two"]}, deep=True)
    service = IssueVerifierService(provider, enabled=True, fail_mode="candidate", max_calls_per_unit=5)
    await service.verify_issues([first, second], [unit], _state(unit), IssueMetrics())
    assert provider.calls == 2


@pytest.mark.parametrize("field", ["failure_scenario", "supporting_evidence", "rules", "head", "scope", "complete"])
def test_verification_key_binds_claim_full_evidence_rules_scope_and_snapshot(field):
    unit, issue = _unit(), _issue("one")
    service = IssueVerifierService(VerifiedProvider(), enabled=True, fail_mode="candidate", max_calls_per_unit=5)
    state = _state(unit)
    request = service._build_request(issue, unit, state, 0)
    original = service._verification_input_key(request, unit, state)
    altered = request.model_copy(deep=True)
    if field == "failure_scenario":
        altered.issue.failure_scenario = "不同的触发条件"
    elif field == "supporting_evidence":
        altered.supporting_evidence = [issue.primary_evidence.model_copy(update={"existing_code": "different"})]
    elif field == "rules":
        altered.applicable_rules = ["another-rule"]
    elif field == "head":
        state = {**state, "head_sha": "new-head"}
    elif field == "scope":
        unit = unit.model_copy(update={"related_files": ["related.py"]})
    else:
        altered.evidence_complete = False
    assert service._verification_input_key(altered, unit, state) != original
