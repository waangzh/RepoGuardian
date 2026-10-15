import pytest

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import ExecutionBudget, ModelCallResult, ModelUsage, ReviewUnit, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_review_summary import domain_output_errors, validate_record
from test_review_step_flow_repairs import StepProvider
from test_unit_review_summary import _state


def parent_question():
    return {"id": "obligation-parent", "kind": "question", "check_id": "parent-question",
            "check": {"id": "parent-question", "question": "显式引用是否存在", "affected_files": ["a.py"]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [True, False])
async def test_parent_question_wrong_channel_is_rejected_and_correctable(repair):
    class Provider(StepProvider):
        async def review_step(self, pr, files, diff, model, record_input):
            response = await super().review_step(pr, files, diff, model, record_input)
            evidence = record_input["evidence"][0]["id"]
            update = {"status": "resolved", "evidence_ids": [evidence], "reason": "当前正文已核验"}
            if len(self.inputs) == 1 or not repair:
                record = response.review_record.model_copy(update={"question_updates": []})
                from app.models.review import UnitQuestionUpdate
                record.question_updates = [UnitQuestionUpdate(question_id="parent-question", **update)]
                assert validate_record(record, record_input, {"a.py"}).latest_attempt_status == "invalid"
            else:
                assert record_input["output_repair"]["errors"][0]["loc"] == ["review_record", "question_updates"]
                from app.models.review import UnitObligationUpdate
                record = response.review_record.model_copy(update={"obligation_updates": [
                    UnitObligationUpdate(obligation_id="obligation-parent", **update)]})
            usage = ModelUsage(provider="test", model="test", operation="review_step", latency_ms=1,
                               actual_total_tokens=10, usage_available=True)
            return ModelCallResult(UnitReviewResponse(review_record=record), usage)

    provider = Provider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(
        max_model_calls=3, max_diagnosis_attempts=3, max_token_usage=100000)
    value = _state()
    value["origin_obligations"] = [parent_question()]
    unit = ReviewUnit(id="followup-u", primary_files=["a.py"], fingerprint="f", estimated_tokens=10,
                      complexity="small", grouping_reason="cross_unit_followup")
    result = await runner.execute_unit(unit, value)
    assert len(provider.inputs) == 2
    assert result.output_repair_attempted
    assert result.review_summary.latest_attempt_status == ("reported" if repair else "invalid"), result.error
    if repair:
        assert result.review_summary.record.obligation_updates[0].obligation_id == "obligation-parent"


def test_prompt_lists_both_update_channels_and_disjoint_catalogs():
    from app.services.unit_review_summary import build_record_input
    from app.models.review import ChangedFile, PullRequestInfo
    value = _state()
    files = [ChangedFile.model_validate(item) for item in value["changed_files"]]
    record_input = build_record_input(files, [], None, "h", "b")
    record_input["origin_obligations"] = [parent_question()]
    payload = OpenAICompatibleProvider._unit_evidence_payload(files, record_input)
    assert payload["update_catalog"] == {"question_updates": [], "obligation_updates": [{
        "obligation_id": "obligation-parent", "kind": "question", "status": "resolved", "required_files": ["a.py"]}]}
    prompt = OpenAICompatibleProvider._build_unit_review_prompt(
        PullRequestInfo.model_validate(value["pr_info"]), files, "", record_input)
    schema = prompt.split("review_record schema", 1)[1].split("At every level", 1)[0]
    assert '"obligation_updates"' in schema
    assert '"question_updates"' in schema
    assert domain_output_errors("invalid_review_record: unknown origin obligation")[0]["loc"] == [
        "review_record", "obligation_updates"]


@pytest.mark.parametrize("change,expected", [("valid", "refuted"), ("invalid", "unresolved"),
    ("missing", "unresolved"), ("dependency", "unresolved"), ("parent", "unresolved"),
    ("evidence", "unresolved"), ("legacy", "unresolved")])
def test_counterevidence_covers_verification_files_without_unrelated_primary_refs(change, expected):
    from app.models.review import ReviewUnitResult, UnitReviewRecord
    from app.services.unit_review_summary import build_record_input
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from test_cross_unit_coordination import state, plan
    value = state()
    payload = CrossUnitCoordinationService.catalog(value)
    request = plan(payload).followups[0].model_copy(update={"verification_files": ["pyproject.toml", "setup.py"]})
    contexts = [{"file": path, "start_line": 1, "end_line": 1, "content": content}
        for path, content in [("pyproject.toml", '[tool.setuptools]\n'), ("setup.py", "setup()")]]
    record_input = build_record_input([], contexts, None, "h", "b")
    evidence = [item["id"] for item in record_input["evidence"]]
    record = UnitReviewRecord(change_summary="两个配置正文没有显式引用本地文件名", target_checks=[{
        "target": target, "status": "checked", "reason": "核对完整正文", "evidence_ids": evidence}
        for target in record_input["targets"] or ["核验显式引用"]], hypothesis_checks=[])
    record_input["targets"] = ["核验显式引用"]
    record_input["hypotheses"] = [{"id": "scope"}]
    from app.models.review import UnitHypothesisCheck
    record.hypothesis_checks = [UnitHypothesisCheck(hypothesis_id="scope", status="refuted", reason="仅限定正文", evidence_ids=evidence)]
    summary = validate_record(record, record_input, {"pyproject.toml", "setup.py"})
    assert summary.status == "reported", summary.reason
    if change == "invalid":
        summary.latest_attempt_status = "invalid"
        summary.latest_attempt_reason = "invalid_review_record: question update requires a visible prior question"
    elif change == "missing":
        summary.record = None
        summary.status = "unknown"
        summary.latest_attempt_status = "missing"
    elif change == "dependency":
        from app.models.review import UnitContractDependency
        summary.record.contract_dependencies = [UnitContractDependency(file_path="setup.py", assumption="全局机制未知", status="unresolved")]
    elif change == "parent":
        request.origin_obligation_ids = ["unclosed-parent"]
    elif change == "evidence":
        summary.record.hypothesis_checks[0].evidence_ids = evidence[:1]
    elif change == "legacy":
        request.verification_files = []
    result = ReviewUnitResult(review_unit_id="followup", status="completed", review_summary=summary)
    selected = [ReviewUnit.model_validate(raw) for raw in value["review_units"]]
    outcome, reason = CrossUnitCoordinationService.followup_conclusion(request, result, selected)
    assert outcome == expected
    if change == "invalid":
        assert "question update requires a visible prior question" in reason


def test_verification_scope_cannot_authorize_unknown_files():
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from test_cross_unit_coordination import state, plan
    payload = CrossUnitCoordinationService.catalog(state())
    proposed = plan(payload)
    proposed.followups[0].verification_files = ["unknown.py"]
    with pytest.raises(ValueError, match="supporting files outside"):
        CrossUnitCoordinationService.normalize_plan(proposed, payload)
