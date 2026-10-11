import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.evidence.branch import branch_error
from app.models.review import EvidenceBranchClaim, ChangedFile, DiffHunk, ReviewUnit
from app.review.issue_audit import capture_issue_audit
from app.review.unit_completion import is_review_unit_complete
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.model_request_budgeter import ModelRequestProfile
from test_review_unit_subgraph import _repo


@pytest.mark.parametrize("header", ["directml", "if directml", "if directml:", "elif directml:"])
def test_condition_header_alias_still_requires_actual_branch(header):
    claim = EvidenceBranchClaim(condition_code=header, arm="else")
    source = "if directml:\n    x = 1\nelse:\n    x = 2\n"
    assert branch_error(source, 4, 4, claim) is None
    assert branch_error(source, 2, 2, claim) == "branch_claim_mismatch"


@pytest.mark.parametrize("next_need", [
    {"type": "file_read", "file_path": "module.py", "symbol": "value"},
    {"action": "file_read", "reason": "读取", "tool_args": {"command": "secret-shell-text"}},
])
async def test_invalid_next_need_preserves_valid_candidates_without_completing_unit(tmp_path, next_need):
    repo = _repo(tmp_path)
    changed = ChangedFile(file_path="module.py", change_type="modified", additions=1, deletions=1,
        hunks=[DiffHunk(old_start=4, old_length=1, new_start=4, new_length=1,
                       added_lines=[{"line_no": 4, "content": "    return FEATURE"}])])
    unit = ReviewUnit(id="u", primary_files=["module.py"], fingerprint="fixture", grouping_reason="fixture",
                      estimated_tokens=100, complexity="medium")
    provider = OpenAICompatibleProvider("fixture", "https://example.test", "fixture", request_attempts=1,
                                       request_profile=ModelRequestProfile(context_window=100_000))
    requests = []

    class Chat:
        async def ainvoke(self, messages):
            requests.append(messages)
            catalog = json.loads(messages[-1].content.split("Bounded record input JSON:\n", 1)[1]
                                 .split("\n本次调用", 1)[0])
            ids = [item["id"] for item in catalog["evidence"] if item["source"] == "diff"]
            return AIMessage(content=json.dumps({
                "issues": [{"title": "返回值与调用契约不符", "category": "correctness", "severity": "medium",
                    "confidence": .9, "affected_behavior": "调用方收到布尔值", "failure_scenario": "调用方期待对象",
                    "recommendation": "核对契约", "primary_evidence": {
                        "file_path": "module.py", "existing_code": "    return FEATURE"}}],
                "review_record": {"change_summary": "检查返回值", "target_checks": [
                    {"target": target, "status": "checked", "evidence_ids": ids, "reason": "已检查"}
                    for target in catalog["targets"]]}, "next_need": next_need}),
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    provider._build_chat_model = lambda model, max_tokens: Chat()
    from app.models.review import PullRequestInfo, PullRequestRef
    pr = PullRequestInfo(owner="local", repo="sample", number=1, title="fixture",
        html_url="https://example.test/pr/1", clone_url="https://example.test/r.git",
        base=PullRequestRef(sha="base", ref="main", repo_clone_url="https://example.test/r.git"),
        head=PullRequestRef(sha="head", ref="head", repo_clone_url="https://example.test/r.git"))
    events = []
    with capture_issue_audit(events.append):
        result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(unit, {
            "task_id": "r6-regression", "pr_info": pr.model_dump(mode="json"),
            "changed_files": [changed.model_dump(mode="json")], "repo_path": str(repo),
            "head_sha": "head", "base_sha": "base", "model": "fixture"})
    assert len(requests) == 2, (result.error, result.terminal_reason, result.plan_skip_reason)
    assert len(result.issues) == 1
    assert any(event["stage"] == "generated" for event in events)
    assert result.review_summary.latest_attempt_status == "invalid"
    assert "invalid_next_need_schema" in result.review_summary.latest_attempt_reason
    assert "secret-shell-text" not in result.review_summary.latest_attempt_reason
    assert not is_review_unit_complete(result)
    assert not result.tool_events
