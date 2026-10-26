from app.models.review_runtime import (
    ReviewGoal, ReviewRunSpec, ReviewWorkItem, RuntimeSubmitResponse, WorkItemKind,
)
from app.services.review.runtime.review_task_executor import ReviewTaskExecutor, RuntimeResponseParser
from tests.support.runtime_v1_fixtures import evidence_catalog


HEAD = "a" * 40
BASE = "b" * 40


def _executor(kind=WorkItemKind.inspect):
    spec = ReviewRunSpec(run_id="run-1", snapshot_head_sha=HEAD, snapshot_base_sha=BASE,
                         diff_manifest_hash="manifest-1", model_provider="stub",
                         model_name="model", policy_version="policy", max_tokens=1000, max_calls=4)
    goals = [ReviewGoal(goal_id="goal-1", unit_id="unit-1", kind=kind,
                        target_ref="src/a.py", required=True)]
    item = ReviewWorkItem(work_item_id="work-1", kind=kind, goal_ids=["goal-1"],
                          scope_files=["src/a.py"])
    return ReviewTaskExecutor(spec, item, goals, active_evidence_ids={"e1"}, request_id="request-1",
                              evidence_catalog=evidence_catalog("e1"))


def test_submit_preserves_valid_sibling_when_one_item_is_malformed():
    parsed = RuntimeResponseParser().parse({
        "kind": "submit",
        "examinations": [
            {"target_id": "goal-1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "已查"},
            {"target_id": "goal-2", "disposition": "examined", "evidence_ids": [], "reason": ""},
        ],
        "candidates": [], "leads": [],
    })
    assert isinstance(parsed.response, RuntimeSubmitResponse)
    assert len(parsed.response.examinations) == 1
    assert parsed.errors and parsed.errors[0].item_type == "examinations"
    executor = _executor()
    applied = executor.parse_and_apply({
        "kind": "submit",
        "examinations": [
            {"target_id": "goal-1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "已查"},
            {"target_id": "unknown", "disposition": "examined", "evidence_ids": ["e1"], "reason": "无效"},
        ],
        "candidates": [], "leads": [],
    })
    assert "goal-1" in executor.projection.accepted_target_ids
    assert "goal-1" not in executor.projection.pending_target_ids
    assert any(error.reason == "unknown_target_id" for error in applied.errors)


def test_unknown_id_stale_evidence_conflict_and_scope_are_rejected():
    executor = _executor()
    parsed = executor.parse_and_apply({
        "kind": "submit",
        "examinations": [
            {"target_id": "goal-1", "disposition": "examined", "evidence_ids": ["stale"], "reason": "旧证据"},
            {"target_id": "goal-1", "disposition": "blocked", "evidence_ids": ["e1"], "reason": "阻塞"},
            {"target_id": "unknown", "disposition": "examined", "evidence_ids": ["e1"], "reason": "未知"},
        ],
        "candidates": [{"claim": "问题", "primary_anchor": "unknown", "evidence_ids": ["e1"]}],
        "leads": [],
    })
    assert not executor.projection.examinations
    reasons = {error.reason for error in parsed.errors}
    assert {"conflicting_duplicate_target", "unknown_target_id"} <= reasons
    conflict = _executor().parse_and_apply({
        "kind": "submit", "examinations": [
            {"target_id": "goal-1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "一"},
            {"target_id": "goal-1", "disposition": "blocked", "evidence_ids": ["e1"], "reason": "二"},
        ], "candidates": [], "leads": [],
    })
    assert any(error.reason == "conflicting_duplicate_target" for error in conflict.errors)
    read = executor.parse_and_apply({"kind": "read", "tool": "file_read",
        "arguments": {"file_path": "src/other.py", "start_line": 1, "end_line": 1},
        "purpose": "越界"})
    assert any(error.reason == "read_path_outside_work_item_scope" for error in read.errors)


def test_read_and_submit_are_mutually_exclusive_and_snapshot_is_frozen():
    parser = RuntimeResponseParser()
    response = parser.parse({"kind": "read", "tool": "file_read",
        "arguments": {"file_path": "src/a.py", "start_line": 1, "end_line": 2},
        "purpose": "核对"})
    assert response.errors == [] and response.response.kind == "read"
    malformed = parser.parse({"kind": "submit", "tool": "file_read", "examinations": []})
    assert malformed.response is None and malformed.errors
    executor = _executor()
    applied = executor.parse_and_apply({"kind": "submit", "examinations": [{
        "target_id": "goal-1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "完成"}],
        "candidates": [], "leads": []})
    result = executor.projection.examinations["goal-1"]
    assert applied.errors == [] and result.snapshot_head_sha == HEAD


def test_inspect_submit_cannot_silently_accept_investigation_or_verification_fields():
    executor = _executor()
    parsed = executor.parse_and_apply({
        "kind": "submit", "examinations": [], "candidates": [], "leads": [],
        "investigation": {"answer": "supported", "evidence_ids": ["e1"], "limitations": []},
        "verification": [{"candidate_id": "c1", "decision": "supported",
                           "evidence_ids": ["e1"], "reason": "核验"}],
    })
    assert parsed.response is None
    assert {error.reason for error in parsed.errors} == {"unknown_submit_field"}
