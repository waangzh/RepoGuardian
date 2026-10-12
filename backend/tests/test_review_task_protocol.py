from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem, WorkItemKind
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_protocol import TaskReviewV1Protocol
from runtime_v1_fixtures import evidence_catalog


def test_prepare_uses_new_protocol_and_freezes_identity_without_legacy_fields():
    spec = ReviewRunSpec(run_id="run-1", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
                         diff_manifest_hash="m", model_provider="stub", model_name="model",
                         policy_version="p", max_tokens=100, max_calls=2)
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind=WorkItemKind.inspect, target_ref="src/a.py")
    work = ReviewWorkItem(work_item_id="w1", kind=WorkItemKind.inspect, goal_ids=["g1"],
                          scope_files=["src/a.py"])
    request = TaskReviewV1Protocol(spec).prepare(work, [goal], active_evidence_ids={"e2", "e1"},
                                               evidence_catalog=evidence_catalog("e1", "e2"))
    assert request.operation == "task_review_v1"
    assert "task-review-protocol-v1" in request.prompt
    assert '"active_evidence_ids":["e1","e2"]' in request.prompt
    assert "question_updates" not in request.prompt and "review_record" not in request.prompt


def test_prepare_send_parse_stub_chain_projects_result():
    spec = ReviewRunSpec(run_id="run-1", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
                         diff_manifest_hash="m", model_provider="stub", model_name="model",
                         policy_version="p", max_tokens=100, max_calls=2)
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind=WorkItemKind.inspect, target_ref="src/a.py")
    work = ReviewWorkItem(work_item_id="w1", kind=WorkItemKind.inspect, goal_ids=["g1"],
                          scope_files=["src/a.py"])
    executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids={"e1"}, request_id="req",
                                  evidence_catalog=evidence_catalog("e1"))
    protocol = TaskReviewV1Protocol(spec)
    request = protocol.prepare(work, [goal], active_evidence_ids={"e1"}, evidence_catalog=executor.evidence_catalog)
    raw = {"kind": "submit", "examinations": [{"target_id": "g1", "disposition": "examined",
        "evidence_ids": ["e1"], "reason": "已检查"}], "candidates": [], "leads": []}
    parsed = protocol.parse(executor, raw)
    assert request.model == "model" and not parsed.errors
    assert executor.projection.examinations["g1"].snapshot_head_sha == "a" * 40


async def test_prepare_send_parse_uses_sdk_stub_without_legacy_review_step():
    spec = ReviewRunSpec(run_id="run-1", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
                         diff_manifest_hash="m", model_provider="stub", model_name="model",
                         policy_version="p", max_tokens=100, max_calls=2)
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind=WorkItemKind.inspect, target_ref="src/a.py")
    work = ReviewWorkItem(work_item_id="w1", kind=WorkItemKind.inspect, goal_ids=["g1"],
                          scope_files=["src/a.py"])
    executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids={"e1"}, request_id="req",
                                  evidence_catalog=evidence_catalog("e1"))
    calls = []

    async def sender(request):
        calls.append(request)
        return {"kind": "submit", "examinations": [{"target_id": "g1", "disposition": "examined",
            "evidence_ids": ["e1"], "reason": "stub"}], "candidates": [], "leads": []}

    parsed = await TaskReviewV1Protocol(spec).send_and_parse(
        executor, work, [goal], active_evidence_ids={"e1"}, sender=sender,
    )
    assert len(calls) == 1 and calls[0].operation == "task_review_v1"
    assert not parsed.errors and "g1" in executor.projection.accepted_target_ids
