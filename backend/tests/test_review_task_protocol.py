from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem, WorkItemKind
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_protocol import TaskReviewV1Protocol
from runtime_v1_fixtures import evidence_catalog
import json
import hashlib
import pytest


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


def test_v2_keeps_validation_contract_but_separates_review_from_empty_findings_example():
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="m", model_provider="stub", model_name="model", policy_version="p",
        max_tokens=100, max_calls=2)
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind="inspect", target_ref="src/a.py")
    item = ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g1"], scope_files=["src/a.py"])
    def prepare(version):
        return TaskReviewV1Protocol(spec.model_copy(update={"prompt_version": version})).prepare(item, [goal],
            active_evidence_ids={"e1"}, evidence_catalog=evidence_catalog("e1"))
    old, new = prepare("task-review-prompt-v1"), prepare("task-review-prompt-v2")
    old_payload, payload = json.loads(old.prompt), json.loads(new.prompt)
    assert payload["response"] == TaskReviewV1Protocol.compact_schema(old_payload["response"])
    assert "Examinations and candidates are independent" in payload["review_contract"]
    assert "submit_example" not in payload
    newest = prepare("task-review-prompt-v3")
    newest_payload = json.loads(newest.prompt)
    assert newest_payload["submit_shape_example"]["kind"] == "submit"
    assert newest_payload["submit_shape_example"]["candidates"][0]["primary_anchor"] == "g1"
    assert "never a path, line number or code fragment" in newest.system
    assert "submit_shape_example" not in payload
    for schema in newest_payload["response"]["one_of"]:
        assert "kind" in schema["required"]
    assert newest_payload["response"]["one_of"][1]["$defs"]["CandidateSubmission"]["properties"]["primary_anchor"]["enum"] == ["g1"]
    fourth = json.loads(prepare("task-review-prompt-v4").prompt)
    examination_schema = fourth["response"]["one_of"][1]["$defs"]["ExaminationSubmission"]
    assert "evidence_ids" in examination_schema["required"]
    assert examination_schema["properties"]["evidence_ids"]["items"]["enum"] == ["e1"]
    assert "entire goal range" in fourth["evidence_requirements"]["examinations"]
    assert "actual old and new operations" in new.system
    assert payload["response"]["one_of"][1]["additionalProperties"] is False
    assert payload["response"]["one_of"][1]["properties"]["candidates"]["maxItems"] == 20
    raw = {"kind": "submit", "examinations": [{"target_id": "g1", "disposition": "examined",
        "evidence_ids": ["e1"], "reason": "存在具体行为缺陷"}], "candidates": [
        {"claim": "候选", "primary_anchor": "g1", "evidence_ids": ["e1"], "failure_scenario": "触发条件"}]}
    for version in ("task-review-prompt-v1", "task-review-prompt-v2"):
        executor = ReviewTaskExecutor(spec.model_copy(update={"prompt_version": version}), item, [goal],
            active_evidence_ids={"e1"}, evidence_catalog=evidence_catalog("e1"), request_id="r")
        assert not executor.parse_and_apply(raw).errors
        assert executor.projection.examinations and executor.projection.candidates
    with pytest.raises(ValueError):
        ReviewRunSpec.model_validate({**spec.model_dump(), "prompt_version": "unrecognized"})


def test_v5_compacts_diff_without_losing_removed_code_or_active_source():
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="manifest", model_provider="stub", model_name="model", policy_version="p",
        max_tokens=100_000, max_calls=5, prompt_version="task-review-prompt-v4")
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind="inspect", target_ref="src/a.py", end_line=3)
    item = ReviewWorkItem(work_item_id="w1", kind="inspect", goal_ids=["g1"], scope_files=["src/a.py"])
    content = "def answer():\n    return value\n# end"
    catalog = evidence_catalog("e1")
    catalog["e1"].update(content=content, end_line=3, content_hash=hashlib.sha256(content.encode()).hexdigest(),
        canonical_change={"goal_id": "g1", "hunk_id": "h1", "hunk_hash": "frozen-hunk",
            "new_start": 1, "new_length": 3, "old_start": 1, "old_length": 4, "lines": [
                {"kind": "context", "content": "def answer():", "new_line_no": 1, "old_line_no": 1},
                {"kind": "deleted", "content": "    return original", "new_line_no": None, "old_line_no": 2},
                {"kind": "added", "content": "    return value", "new_line_no": 2, "old_line_no": None},
                {"kind": "context", "content": "# end", "new_line_no": 3, "old_line_no": 3},
                {"kind": "deleted", "content": "# removed tail", "new_line_no": None, "old_line_no": 4},
            ]})
    def prepare(version, selected_goal=goal):
        return json.loads(TaskReviewV1Protocol(spec.model_copy(update={"prompt_version": version})).prepare(
            item, [selected_goal], active_evidence_ids={"e1"}, evidence_catalog=catalog).prompt)
    old, new = prepare("task-review-prompt-v4"), prepare("task-review-prompt-v5")
    assert old["active_evidence"] == new["active_evidence"]
    assert old["response"] == new["response"]
    change = new["canonical_changes"][0]
    assert change["lines"] == [line for line in old["canonical_changes"][0]["lines"] if line["kind"] != "context"]
    assert change["hunk_hash"] == "frozen-hunk"
    assert change["visible_head_start"] == 1 and change["visible_head_end"] == 3
    assert len(old["canonical_changes"][0]["lines"]) == 5
    assert new["active_evidence"][0]["content"] == content
    second_page = prepare("task-review-prompt-v5", goal.model_copy(update={"start_line": 2, "end_line": 2}))
    assert [line["content"] for line in second_page["canonical_changes"][0]["lines"]] == [
        "    return original", "    return value"]
    tail_page = prepare("task-review-prompt-v5", goal.model_copy(update={"start_line": 3, "end_line": 3}))
    assert tail_page["canonical_changes"][0]["lines"][0]["content"] == "# removed tail"
