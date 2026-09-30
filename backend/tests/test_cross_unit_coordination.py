import asyncio
from copy import deepcopy

import pytest

from app.agents.providers import LLMProviderError
from app.models.review import (
    AgentAction, ChangedFile, CrossUnitCoordinationPlan, ExecutionBudget,
    IssueVerification, IssueStatus, PullRequestInfo, PullRequestRef, ReviewIssueInput,
    ReviewUnit, ReviewUnitResult, UnitReviewRecord, UnitReviewResponse,
)
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.cross_unit_risk import CrossUnitRiskService
from app.services.unit_review_summary import build_record_input, validate_record
from app.services.review_rebuild import rebuild_task_from_state
from app.services.report_service import ReportService


def state(tmp_path=None):
    class Git:
        def get_file_content_at_revision(self, *args):
            return "return {'value': 1}"

        def __deepcopy__(self, memo):
            return self
    files = [ChangedFile.model_validate({"file_path": path, "change_type": "modified",
        "additions": 1, "deletions": 1, "hunks": [{"old_start": 1, "old_length": 1,
        "new_start": 1, "new_length": 1,
        "added_lines": [{"line_no": 1, "content": "return None"}]}]})
        for path in ("caller.py", "callee.py")]
    units = [ReviewUnit(id=f"u{i}", primary_files=[item.file_path], estimated_tokens=10,
        changed_symbols=["load"], complexity="small", fingerprint=str(i), grouping_reason="single")
        for i, item in enumerate(files)]
    results = []
    for unit, file in zip(units, files):
        catalog = build_record_input([file], [], None, "h", "b")
        summary = validate_record(UnitReviewRecord(change_summary="修改返回值"), catalog, {file.file_path})
        results.append(ReviewUnitResult(review_unit_id=unit.id, status="completed",
            terminal_reason="no_issue", review_summary=summary))
    pr = PullRequestInfo(owner="local", repo="sample", number=1, title="PR",
        html_url="https://github.com/local/sample/pull/1", clone_url="https://github.com/local/sample.git",
        base=PullRequestRef(ref="main", sha="b", repo_clone_url="https://github.com/local/sample.git"),
        head=PullRequestRef(ref="feature", sha="h", repo_clone_url="https://github.com/local/sample.git"))
    value = {"task_id": "task", "base_sha": "b", "head_sha": "h", "pr_info": pr.model_dump(mode="json"),
        "_git_tool": Git(),
        "review_units": [item.model_dump(mode="json") for item in units],
        "review_unit_results": [item.model_dump(mode="json") for item in results],
        "changed_files": [item.model_dump(mode="json") for item in files],
        "file_index": [{"path": item.file_path, "analysis_level": 2} for item in files],
        "repository_graph": {"files": [{"path": item.file_path} for item in files],
            "edges": [{"source_kind": "symbol", "source": "caller.py::load", "target_kind": "symbol",
                "target": "callee.py::load", "type": "calls", "confidence": .92,
                "parser_id": "tree-sitter.python.v1"}], "metadata": {"file_count": 2, "edge_count": 1}}}
    value["cross_unit_risk"] = CrossUnitRiskService().assess(value).model_dump(mode="json")
    if tmp_path is not None:
        value["repo_path"] = str(tmp_path)
        for file in files:
            (tmp_path / file.file_path).write_text("return None\n", encoding="utf-8")
    return value


def plan(payload):
    return CrossUnitCoordinationPlan(decision="required", reason="核验跨模块空值契约", followups=[{
        "id": "f1", "question": "返回 None 时调用方是否处理空值？", "unit_ids": ["u0", "u1"],
        "primary_files": ["callee.py"], "evidence_ids": [payload["evidence"][0]["id"]],
        "counterevidence_goal": "寻找调用方空值防护", "stop_condition": "完成契约路径检查或预算耗尽",
    }])


class Provider:
    def __init__(self, *, candidate=True, verifier_fail=False):
        self.candidate = candidate
        self.verifier_fail = verifier_fail
        self.calls = []
        self.decisions = []

    async def coordinate_cross_units(self, payload, model):
        self.calls.append("coordinate")
        return plan(payload)

    async def decide(self, value, model):
        self.calls.append("decide")
        self.decisions.append(value)
        return AgentAction(action="task_done" if value["issue_round_completed"] else "report_issue", reason="定向检查")

    async def review_unit(self, pr, files, diff, model, catalog):
        self.calls.append("review")
        assert "寻找调用方空值防护" in diff
        issue = ReviewIssueInput(title="空值返回导致调用方失败", category="correctness", severity="high",
            confidence=.95, affected_behavior="调用方无法读取返回值", failure_scenario="返回 None 后调用方读取字段失败",
            recommendation="处理空值返回", primary_evidence={"file_path": "callee.py", "existing_code": "return None"}).to_issue()
        return UnitReviewResponse(issues=[issue] if self.candidate else [])

    async def verify_issue(self, request, model):
        self.calls.append("verify")
        if self.verifier_fail:
            raise LLMProviderError("verifier unavailable")
        return IssueVerification(issue_id=request.issue.id, decision="keep", reason="检查后确认契约缺陷")


@pytest.mark.asyncio
async def test_followup_batch_is_verified_without_mutating_original_coverage(tmp_path):
    value = state(tmp_path)
    before = deepcopy(value)
    provider = Provider()
    result = await CrossUnitCoordinationService(provider).run(value)
    assert value == before
    assert result["coordination_plan"]["status"] == "completed", result
    assert provider.calls == ["coordinate", "decide", "review", "decide", "verify"]
    assert result["review_issues"][0]["status"] == "confirmed"
    assert result["issue_metrics"]["candidate_issue_count"] == 1
    assert result["issue_metrics"]["verifier_call_count"] == 1
    assert len(result["issue_verifications"]) == 1
    assert "review_units" not in result and "review_unit_results" not in result
    assert result["followup_results"][0]["unit_result"]["issues"] == result["review_issues"]
    scope = provider.decisions[0]["review_tool_scope"]
    assert set(scope["readable_files"]) == {"caller.py", "callee.py"}
    assert not scope["repository_discovery_enabled"]
    assert result["coordination_plan"]["execution_budget"]["model_calls"] == 5
    rebuilt = rebuild_task_from_state({**value, **result})
    assert rebuilt.coordination_plan.execution_budget.model_calls == 5
    assert "跨 Unit 协调与定向补查" in ReportService().generate(rebuilt)
    assert await CrossUnitCoordinationService(provider).run({**value, **result}) == {}


@pytest.mark.asyncio
async def test_zero_candidates_stays_unresolved_and_verifier_failure_needs_human(tmp_path):
    result = await CrossUnitCoordinationService(Provider(candidate=False)).run(state())
    assert result["followup_results"][0]["outcome"] == "unresolved"
    assert result["coordination_plan"]["status"] == "unresolved"
    failed = await CrossUnitCoordinationService(Provider(verifier_fail=True)).run(state(tmp_path))
    assert failed["review_issues"][0]["status"] == "needs_human"
    assert failed["coordination_plan"]["status"] == "unresolved"


@pytest.mark.parametrize("change", ["unit", "path", "reference", "duplicate", "downgrade"])
def test_untrusted_plan_is_rejected(change):
    payload = CrossUnitCoordinationService.catalog(state())
    proposed = plan(payload).model_dump(mode="json")
    if change == "unit":
        proposed["followups"][0]["unit_ids"] = ["u0", "unknown"]
    elif change == "path":
        proposed["followups"][0]["primary_files"] = ["outside.py"]
    elif change == "reference":
        proposed["followups"][0]["evidence_ids"] = ["stale"]
    elif change == "duplicate":
        proposed["followups"].append({**proposed["followups"][0], "id": "f2"})
    else:
        proposed.update(decision="skip", followups=[])
    with pytest.raises(ValueError):
        CrossUnitCoordinationService.validate_plan(CrossUnitCoordinationPlan.model_validate(proposed), payload)


@pytest.mark.asyncio
async def test_shared_budget_covers_diagnosis_and_verifier_and_counts_failure():
    provider = Provider()
    result = await CrossUnitCoordinationService(provider, budget=ExecutionBudget(
        max_model_calls=2, max_token_usage=120000)).run(state())
    assert provider.calls == ["coordinate"]
    assert result["coordination_plan"]["status"] == "failed"
    assert result["coordination_plan"]["execution_budget"]["model_calls"] == 1
    result = await CrossUnitCoordinationService(Provider(), budget=ExecutionBudget(
        max_model_calls=0)).run(state())
    assert result["coordination_plan"]["status"] == "failed"


@pytest.mark.asyncio
async def test_uncertain_with_missing_coverage_cannot_skip_and_cancellation_propagates():
    class Skip(Provider):
        async def coordinate_cross_units(self, payload, model):
            return CrossUnitCoordinationPlan(decision="skip", reason="假定独立")
    value = state()
    value["cross_unit_risk"].update(decision="uncertain", index_status="unknown")
    result = await CrossUnitCoordinationService(Skip()).run(value)
    assert result["coordination_plan"]["status"] == "failed"
    class Cancel(Provider):
        async def coordinate_cross_units(self, payload, model):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await CrossUnitCoordinationService(Cancel()).run(state())


def test_catalog_rejects_evidence_from_another_snapshot():
    value = state()
    value["head_sha"] = "new-head"
    assert not CrossUnitCoordinationService.catalog(value)["evidence"]


@pytest.mark.asyncio
async def test_uncertain_can_promote_to_bounded_followup(tmp_path):
    value = state(tmp_path)
    value["cross_unit_risk"]["decision"] = "uncertain"
    result = await CrossUnitCoordinationService(Provider()).run(value)
    assert result["coordination_plan"]["decision"] == "required"
    assert result["review_issues"][0]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_original_issue_and_metrics_are_not_reverified(tmp_path):
    value = state(tmp_path)
    original = ReviewIssueInput(title="原有问题", category="correctness", severity="medium", confidence=.9,
        affected_behavior="原有返回路径异常", failure_scenario="原有调用路径读取失败", recommendation="修复原有路径",
        primary_evidence={"file_path": "caller.py", "existing_code": "return None"}).to_issue("u0")
    original.status = IssueStatus.confirmed
    value["review_issues"] = [original.model_dump(mode="json")]
    value["issue_metrics"] = {"candidate_issue_count": 1, "verifier_call_count": 1}
    provider = Provider()
    result = await CrossUnitCoordinationService(provider).run(value)
    assert result["review_issues"][0] == value["review_issues"][0]
    assert provider.calls.count("verify") == 1
    assert result["issue_metrics"]["candidate_issue_count"] == 2
    assert result["issue_metrics"]["verifier_call_count"] == 2


@pytest.mark.asyncio
async def test_uncertain_relationship_skip_requires_both_end_evidence():
    class Skip(Provider):
        async def coordinate_cross_units(self, payload, model):
            return CrossUnitCoordinationPlan(decision="skip", reason="已检查两侧的契约，静态边与此次变更无关",
                relationship_ids=[item["id"] for item in payload["risk"]["relationships"]],
                evidence_ids=[item["id"] for item in payload["evidence"]])
    value = state()
    value["cross_unit_risk"].update(decision="uncertain", index_status="available", reasons=[{
        "code": "relationship_unknown", "detail": "弱关联需要判别",
    }])
    result = await CrossUnitCoordinationService(Skip()).run(value)
    assert result["coordination_plan"]["status"] == "completed"
    assert result["followup_results"] == []


@pytest.mark.asyncio
async def test_token_budget_and_timeout_are_explicit_failures():
    provider = Provider()
    result = await CrossUnitCoordinationService(provider, budget=ExecutionBudget(
        max_token_usage=1)).run(state())
    assert not provider.calls
    assert result["coordination_plan"]["status"] == "failed"
    class Hang(Provider):
        async def coordinate_cross_units(self, payload, model):
            await asyncio.Event().wait()
    result = await CrossUnitCoordinationService(Hang(), timeout_seconds=.01).run(state())
    assert "TimeoutError" in result["coordination_plan"]["reason"]
