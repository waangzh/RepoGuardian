from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.providers import LLMProvider
from app.models.review import (
    AgentAction, ChangedFile, ChangedLine, DiffHunk, PullRequestInfo, PullRequestRef,
    ReviewUnit, ReviewUnitResult, UnitReviewPlan, UnitReviewRecord, UnitReviewResponse,
    ReviewIssue,
)
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_review_summary import build_record_input, merge_review_summaries, validate_record


def _file() -> ChangedFile:
    return ChangedFile(file_path="a.py", change_type="modified", additions=1, deletions=0,
                       hunks=[DiffHunk(old_start=1, old_length=1, new_start=1, new_length=1,
                                       added_lines=[ChangedLine(line_no=1, content="return value")])])


def _input() -> dict[str, Any]:
    plan = UnitReviewPlan(change_summary="变更返回值", review_objectives=["检查返回值"],
                          coverage_targets=["检查空值"], risk_hypotheses=[{
                              "id": "h1", "category": "correctness", "priority": "high",
                              "description": "空值可能失败", "completion_criteria": "检查调用方",
                          }], initial_action=AgentAction(action="report_issue", reason="审查"))
    return build_record_input([_file()], [], plan, "head-a")


def test_record_checks_bind_to_snapshot_and_missing_targets_stay_unchecked() -> None:
    request = _input()
    evidence_id = request["evidence"][0]["id"]
    record = UnitReviewRecord(change_summary="变更返回值", target_checks=[{
        "target": "检查返回值", "status": "checked", "evidence_ids": [evidence_id], "reason": "已检查",
    }])
    summary = validate_record(record, request, {"a.py"})
    assert summary.status == "reported"
    assert summary.record is not None
    assert [item.status for item in summary.record.target_checks] == ["checked", "not_checked"]
    assert summary.record.hypothesis_checks[0].status == "unresolved"
    assert summary.evidence[0].head_sha == "head-a"
    changed_snapshot = build_record_input([_file()], [], None, "head-b")
    assert changed_snapshot["evidence"][0]["id"] != evidence_id
    changed_base = build_record_input([_file()], [], None, "head-a", "new-base")
    assert changed_base["evidence"][0]["id"] != evidence_id


@pytest.mark.parametrize("field,value", [
    ("target_checks", [{"target": "不存在的目标", "status": "unresolved", "reason": "未知"}]),
    ("target_checks", [{"target": "检查返回值", "status": "checked", "evidence_ids": ["stale"], "reason": "已检查"}]),
    ("hypothesis_checks", [{"hypothesis_id": "missing", "status": "unresolved", "reason": "未知"}]),
    ("contract_dependencies", [{"file_path": "outside.py", "status": "unresolved", "assumption": "非空"}]),
    ("unresolved_questions", [{"question": "未知", "affected_files": ["outside.py"]}]),
])
def test_invalid_references_degrade_to_unknown(field: str, value: Any) -> None:
    record = UnitReviewRecord.model_validate({"change_summary": "修改返回值", field: value})
    summary = validate_record(record, _input(), {"a.py"})
    assert summary.status == "unknown" and summary.record is None
    assert summary.reason.startswith("invalid_review_record")


def test_missing_and_legacy_records_are_unknown_not_completed_checks() -> None:
    summary = validate_record(None, _input(), {"a.py"})
    assert summary.status == "unknown"
    assert ReviewUnitResult(review_unit_id="old").review_summary.status == "unknown"
    with pytest.raises(ValidationError):
        UnitReviewRecord(change_summary="修改", target_checks=[{
            "target": "检查返回值", "status": "checked", "reason": "无证据",
        }])
    with pytest.raises(ValidationError):
        UnitReviewRecord(change_summary="修改", contract_dependencies=[{
            "file_path": "../secret", "status": "unresolved", "assumption": "未知",
        }])


class RecordingProvider(LLMProvider):
    def __init__(self, invalid: bool = False) -> None:
        self.calls = 0
        self.invalid = invalid

    async def decide(self, state, model):
        return AgentAction(action="task_done" if state["issue_round_completed"] else "report_issue",
                           reason="完成" if state["issue_round_completed"] else "检查")

    async def review(self, pr, changed_files, diff_text, model):
        self.calls += 1
        return []

    async def review_unit(self, pr, changed_files, diff_text, model, record_input):
        self.calls += 1
        evidence_id = "stale" if self.invalid else record_input["evidence"][0]["id"]
        return UnitReviewResponse(review_record=UnitReviewRecord(
            change_summary="修改返回值", target_checks=[{
                "target": record_input["targets"][0], "status": "checked",
                "evidence_ids": [evidence_id], "reason": "已检查变更",
            }], unresolved_questions=[{"question": "调用方是否处理空值？", "affected_files": ["b.py"]}],
        ))

    async def generate_patch(self, state, model):
        raise AssertionError("只读阶段不应生成补丁")


def _state() -> dict[str, Any]:
    ref = PullRequestRef(ref="main", sha="head-a", repo_clone_url="")
    pr = PullRequestInfo(owner="local", repo="repo", number=1, title="修改返回值",
                         html_url="", clone_url="", base=ref, head=ref)
    return {"task_id": "test", "head_sha": "head-a", "pr_info": pr.model_dump(mode="json"),
            "changed_files": [_file().model_dump(mode="json")],
            "file_index": [{"path": "a.py"}, {"path": "b.py"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_executor_records_checks_without_an_extra_model_call(invalid: bool) -> None:
    unit = ReviewUnit(id="u1", primary_files=["a.py"], fingerprint="f", estimated_tokens=10,
                      complexity="small", grouping_reason="single")
    provider = RecordingProvider(invalid)
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(unit, _state())
    assert result.status == "completed" and result.terminal_reason == "no_issue"
    assert provider.calls == 1
    assert result.review_summary.status == ("unknown" if invalid else "reported")
    if not invalid:
        assert result.review_summary.record.unresolved_questions[0].affected_files == ["b.py"]


def test_multi_round_records_retain_checked_targets_and_history() -> None:
    request = _input()
    identity = request["evidence"][0]["id"]
    first = UnitReviewRecord(change_summary="检查返回值", target_checks=[{
        "target": "检查返回值", "status": "checked", "evidence_ids": [identity], "reason": "第一轮已检查",
    }])
    second = UnitReviewRecord(change_summary="检查空值", target_checks=[{
        "target": "检查空值", "status": "checked", "evidence_ids": [identity], "reason": "第二轮已检查",
    }])
    merged = merge_review_summaries(validate_record(first, request, {"a.py"}),
                                    validate_record(second, request, {"a.py"}), second)
    assert [item.status for item in merged.record.target_checks] == ["checked", "checked"]
    assert len(merged.record_history) == 2
    invalid = merge_review_summaries(merged, validate_record(None, request, {"a.py"}), None)
    assert invalid.status == "unknown" and invalid.record is None
    assert len(invalid.record_history) == 2


def test_overlong_round_record_is_unknown_instead_of_breaking_snapshot_validation() -> None:
    record = UnitReviewRecord(change_summary="修改", unresolved_questions=[
        {"question": f"问题 {index}"} for index in range(21)
    ])
    assert validate_record(record, _input(), {"a.py"}).status == "unknown"


@pytest.mark.asyncio
async def test_invalid_summary_does_not_discard_valid_issue() -> None:
    class IssueProvider(RecordingProvider):
        async def review_unit(self, *args):
            response = await super().review_unit(*args)
            response.issues = [ReviewIssue(id="valid", review_unit_id="u1", severity="high", category="correctness",
                title="空值问题", affected_behavior="空值失败", failure_scenario="输入为空",
                recommendation="处理空值", confidence=0.9,
                primary_evidence={"file_path": "a.py", "existing_code": "return value"})]
            return response

    unit = ReviewUnit(id="u1", primary_files=["a.py"], fingerprint="f", estimated_tokens=10,
                      complexity="small", grouping_reason="single")
    result = await ReviewUnitExecutor(IssueProvider(True), concurrency=1, timeout_seconds=10).execute_unit(unit, _state())
    assert result.error is None, result.error
    assert result.review_summary.status == "unknown"
    assert [item.id for item in result.issues] == ["valid"], result.model_dump()


@pytest.mark.asyncio
async def test_legacy_provider_adapter_keeps_unknown_summary() -> None:
    class LegacyProvider(RecordingProvider):
        review_unit = LLMProvider.review_unit

    unit = ReviewUnit(id="u1", primary_files=["a.py"], fingerprint="f", estimated_tokens=10,
                      complexity="small", grouping_reason="single")
    provider = LegacyProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(unit, _state())
    assert result.review_summary.status == "unknown"
    assert provider.calls == 1
