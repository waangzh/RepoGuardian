import pytest

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import ExecutionBudget, ModelCallResult, ModelUsage, ReviewUnit, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_review_summary import domain_output_errors, validate_record
from test_review_step_flow_repairs import StepProvider
from test_unit_review_summary import _state
from test_phase6a_persistence import persistence as persistence


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


def test_local_question_catalog_is_not_duplicated_without_parent_obligations():
    import json
    from app.models.review import ChangedFile, PullRequestInfo
    from app.services.unit_review_summary import build_record_input
    value = _state()
    files = [ChangedFile.model_validate(item) for item in value["changed_files"]]
    record_input = build_record_input(files, [], None, "h", "b")
    record_input["working_memory"] = {"unresolved_questions": [{"id": "local-question", "question": "调用方是否处理空值",
        "affected_files": ["a.py"], "evidence_ids": []}]}
    payload = OpenAICompatibleProvider._unit_evidence_payload(files, record_input)
    assert "update_catalog" not in payload
    assert payload["working_memory"] == record_input["working_memory"]
    prompt = OpenAICompatibleProvider._build_unit_review_prompt(
        PullRequestInfo.model_validate(value["pr_info"]), files, "", record_input)
    schema = json.JSONDecoder().raw_decode(prompt.split("review_record schema (replace values; empty lists are allowed): ", 1)[1])[0]
    assert schema["obligation_updates"] == []
    assert "Otherwise obligation_updates must be []" in prompt
    assert "working_memory" in prompt
    record_input["origin_obligations"] = [parent_question()]
    catalog = OpenAICompatibleProvider._unit_evidence_payload(files, record_input)["update_catalog"]
    assert catalog["question_updates"][0]["question_id"] == "local-question"
    assert catalog["obligation_updates"][0]["obligation_id"] == "obligation-parent"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid,uncovered,interrupt", [(True, True, False), (False, False, False),
    (False, True, False), (False, False, True)])
async def test_execution_updates_bound_planning_questions_and_completion_report(invalid, uncovered, interrupt, persistence):
    from app.models.review import CrossUnitCoordinationPlan, UnitReviewRecord
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from app.services.coordination_questions import refresh_plan_questions
    from app.services.review_rebuild import rebuild_task_from_state
    from app.services.report_service import ReportService
    from test_cross_unit_coordination import state, plan

    class Provider(StepProvider):
        async def coordinate_cross_units(self, payload, model):
            proposed = plan(payload)
            first = proposed.followups[0]
            first.planning_question_indices = [0]
            second = first.model_copy(update={"id": "f2", "question": "文档是否引用本地指引？", "planning_question_indices": [1]})
            proposed.followups.append(second)
            proposed.unresolved_questions = [first.question, second.question] + (["没有补查覆盖的构建机制"] if uncovered else [])
            return proposed

        async def review_step(self, pr, files, diff, model, record_input):
            self.inputs.append(record_input)
            if invalid and record_input["verification_scope"]["id"] == "f1":
                return UnitReviewResponse(record_error="invalid_review_record: question update requires a visible prior question")
            evidence = [item["id"] for item in record_input["evidence"]]
            return UnitReviewResponse(review_record=UnitReviewRecord(change_summary="限定问题已核验", target_checks=[{
                "target": target, "status": "checked", "reason": "核对当前证据", "evidence_ids": evidence}
                for target in record_input["targets"]], hypothesis_checks=[{
                    "hypothesis_id": item["id"], "status": "refuted", "reason": "存在限定反证", "evidence_ids": evidence}
                    for item in record_input["hypotheses"]]))

    value = state()
    # 将两个文件的已保存正文送入补查，确保旧范围规则也具有两侧证据。
    from app.models.review import ContextSnippet
    from app.services.unit_review_summary import build_record_input
    for raw in value["review_unit_results"]:
        path = next(item["primary_files"][0] for item in value["review_units"] if item["id"] == raw["review_unit_id"])
        context = ContextSnippet(file=path, start_line=1, end_line=1, content="return None", relevance="direct")
        raw["context_snippets"] = [context.model_dump(mode="json")]
        file = next(item for item in value["changed_files"] if item["file_path"] == path)
        from app.models.review import ChangedFile
        catalog = build_record_input([ChangedFile.model_validate(file)], raw["context_snippets"], None, "h", "b")
        raw["review_summary"] = validate_record(UnitReviewRecord(change_summary="当前证据"), catalog, {path}).model_dump(mode="json")
    provider = Provider()
    observed = []
    stopped = False
    repository = persistence[0] if interrupt else None
    if repository:
        from app.models.review import ReviewTask
        repository.create_task(ReviewTask(id=value["task_id"], pr_url=value["pr_info"]["html_url"]))

    def progress(snapshot):
        nonlocal stopped
        observed.append(snapshot)
        if interrupt and not stopped and any(question["question"] == "文档是否引用本地指引？"
                and question["status"] == "resolved" for question in snapshot["coordination_plan"]["question_states"]):
            import asyncio
            stopped = True
            raise asyncio.CancelledError

    service = CrossUnitCoordinationService(provider, repository=repository, progress=progress)
    if interrupt:
        import asyncio
        with pytest.raises(asyncio.CancelledError):
            await service.run(value)
        service = CrossUnitCoordinationService(provider, repository=repository, progress=progress)
    result = await service.run(value)
    saved = result["coordination_plan"]
    assert saved["status"] == ("unresolved" if invalid or uncovered else "completed"), saved
    states = {item["question"]: item for item in saved["question_states"]}
    assert states["文档是否引用本地指引？"]["status"] == "resolved", result["followup_results"]
    assert "文档是否引用本地指引？" not in saved["unresolved_questions"]
    if invalid:
        assert states[saved["followups"][0]["question"]]["status"] == "invalid"
    if uncovered:
        assert "没有补查覆盖的构建机制" in saved["unresolved_questions"]
    restored = CrossUnitCoordinationPlan.model_validate(saved)
    refreshed = refresh_plan_questions(restored, result["followup_results"], value)
    assert refreshed.model_dump(mode="json") == saved
    assert any(snapshot["coordination_plan"]["status"] == "validated"
        and any(question["question"] == "文档是否引用本地指引？" and question["status"] == "resolved"
                for question in snapshot["coordination_plan"]["question_states"])
        for snapshot in observed), "问题关闭必须在运行进度落盘时可见，不只出现在最终输出"
    from copy import deepcopy
    stale = deepcopy(result["followup_results"])
    stale[-1]["unit_result"]["review_summary"]["last_valid_snapshot"]["head_sha"] = "other-head"
    assert next(item.status for item in refresh_plan_questions(restored, stale, value).question_states
                if item.question == "文档是否引用本地指引？") == "unresolved"
    report = ReportService().generate(rebuild_task_from_state({**value, **result}))
    assert "- 未决：文档是否引用本地指引？" not in report
    assert "规划问题[已解决]：文档是否引用本地指引？" in report
    assert await CrossUnitCoordinationService(provider).run({**value, **result}) == {}
    assert len(provider.inputs) == 2


def test_planning_question_binding_rejects_unknown_index_and_survives_deduplication():
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from test_cross_unit_coordination import state, plan
    payload = CrossUnitCoordinationService.catalog(state())
    proposed = plan(payload)
    first = proposed.followups[0]
    first.planning_question_indices = [0]
    proposed.unresolved_questions = ["问题一", "问题二", "未覆盖"]
    proposed.followups.append(first.model_copy(update={"id": "duplicate", "planning_question_indices": [1]}))
    normalized, _ = CrossUnitCoordinationService.normalize_plan(proposed, payload)
    assert len(normalized.followups) == 1
    assert [item.followup_ids for item in normalized.question_states] == [["f1"], ["f1"], []]
    first.planning_question_indices = [3]
    with pytest.raises(ValueError, match="unknown planning question index"):
        CrossUnitCoordinationService.normalize_plan(proposed, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [True, False])
async def test_catalog_question_bindings_follow_rebased_ids_and_preserve_unadmitted_work(monkeypatch, duplicate):
    from app.models.review import CrossUnitCatalogBatch
    from app.services.coordination_runtime import CoordinationRuntime
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from test_cross_unit_coordination import Provider as BaseProvider, state, plan
    payload = CrossUnitCoordinationService.catalog(state())
    batches = [(CrossUnitCatalogBatch(id=f"catalog-{index}", input_hash=f"hash-{index}", input_chars=100,
        unit_ids=["u0", "u1"]), {**payload, "batch_id": f"catalog-{index}"}) for index in range(2)]
    monkeypatch.setattr("app.services.cross_unit_coordination.catalog_batches", lambda *args: batches)

    class Provider(BaseProvider):
        async def coordinate_cross_units(self, batch, model):
            index = len(self.calls)
            self.calls.append(index)
            proposed = plan(batch)
            original = proposed.followups[0]
            proposed.followups = [original.model_copy(update={"id": f"local-{offset}",
                "question": original.question if duplicate else f"独立检查 {index}-{offset}",
                "planning_question_indices": [offset]}) for offset in range(1 if duplicate else 2)]
            proposed.unresolved_questions = [f"批次 {index} 规划问题 {offset}" for offset in range(len(proposed.followups))]
            return proposed

    service = CrossUnitCoordinationService(Provider(), budget=ExecutionBudget(max_model_calls=64, max_token_usage=1000000))
    service.runtime = CoordinationRuntime("test", "fingerprint", service.provider.budget)
    service.provider.runtime = service.runtime
    result = await service.plan_catalog(payload, None, [])
    assert len(result.followups) == (1 if duplicate else 3)
    ids = {item.id for item in result.followups}
    linked = [identity for item in result.question_states for identity in item.followup_ids]
    assert all(identity in ids or identity.startswith("not-admitted-") for identity in linked)
    if duplicate:
        assert len(result.question_states) == 2
        assert set(linked) == ids
    else:
        assert any(identity.startswith("not-admitted-") for identity in linked)
        assert any("补查未执行" in question for question in result.unresolved_questions)
    assert all(not item.planning_question_indices for item in result.followups)


def test_legacy_question_binding_does_not_guess_from_similar_wording():
    from app.services.coordination_questions import bind_plan_questions
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from test_cross_unit_coordination import state, plan
    proposed = plan(CrossUnitCoordinationService.catalog(state()))
    question = proposed.followups[0].question
    proposed.unresolved_questions = [question, question + "以及完整构建机制是否受影响？"]
    bound = bind_plan_questions(proposed)
    assert bound.question_states[0].followup_ids == ["f1"]
    assert bound.question_states[1].followup_ids == []
