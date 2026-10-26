import json
from copy import deepcopy

import pytest

from app.llm.providers import LLMProviderError
from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
from tests.support.runtime_harness import make_slice




async def test_sdk_stub_read_tool_submit_uses_real_provider_budget_and_store(tmp_path):
    from app.services.review.runtime.review_runtime import TaskReviewRuntime
    def submit(payload):
        # Current Prompt actually contains the canonical stored tool evidence text.
        evidence = next(item for item in payload["active_evidence"] if item["id"] != "e1")
        assert evidence["content"] == "return value"
        return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
            "evidence_ids": [evidence["id"]], "reason": "已核对完整固定范围"}], "candidates": [], "leads": []}

    step, messages = make_slice(tmp_path, [
        {"kind": "read", "tool": "file_read", "arguments": {
            "file_path": "src/a.py", "start_line": 1, "end_line": 1}, "purpose": "读取实现"}, submit])
    runtime = TaskReviewRuntime([step.executor.work_item])
    steps = {"w1": step}
    assert (await runtime.advance_one(steps))["action"] == "model"
    assert step.budget.calls_consumed == 1
    tool_result = await runtime.advance_one(steps)
    assert tool_result["action"] == "tool" and tool_result["status"] == "completed"
    assert step.store.entries and step.budget.calls_consumed == 1
    result = await runtime.advance_one(steps)
    assert not result["errors"] and not result["pending_goals"]
    assert len(messages) == 2 and step.budget.settled_actual == 30
    examination = step.executor.projection.examinations["t1"]
    assert examination.request_id in {attempt.request_id for attempt in step.budget.attempts.values()}
    assert {attempt.operation for attempt in step.budget.attempts.values()} == {"task_review_v1"}
    assert runtime.snapshot()[0]["state"] == "completed"
    assert await runtime.advance_one(steps) is None


async def test_task_prompt_separates_transport_and_business_shape(tmp_path):
    step, messages = make_slice(tmp_path, [{"kind": "submit", "examinations": [
        {"target_id": "t1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "核对固定目标"}]}])
    await step.advance()
    payload = json.loads(messages[0][1].content)
    assert payload["submit_example"]["examinations"][0]["target_id"] == "t1"
    assert "Never emit API transport fields" in messages[0][0].content
    assert "type" not in payload["submit_example"]


async def test_read_more_than_eighty_lines_saves_matching_chunk_bodies_and_hashes(tmp_path):
    import hashlib
    body = "return value\n" * 100
    step, messages = make_slice(tmp_path, [{"kind": "read", "tool": "file_read",
        "arguments": {"file_path": "src/a.py", "start_line": 1, "end_line": 100}, "purpose": "读取实现"}])
    (tmp_path / "src/a.py").write_text(body, encoding="utf-8")
    step.git.get_file_content_at_revision = lambda *args: body
    await step.advance()
    observation = await step.advance()
    assert observation["status"] == "completed" and len(observation["evidence_ids"]) == 2
    assert step.executor.evidence_error(observation["evidence_ids"]) is None
    for identity in observation["evidence_ids"]:
        value = step.executor.evidence_catalog[identity]
        assert value["content_hash"] == hashlib.sha256(value["content"].encode()).hexdigest()
        assert len(value["content"].splitlines()) == value["end_line"] - value["start_line"] + 1
    request = step.protocol.prepare(step.executor.work_item, step.executor.current_goals(),
        active_evidence_ids=step.executor.active_evidence_ids, evidence_catalog=step.executor.evidence_catalog)
    assert len(json.loads(request.prompt)["active_evidence"]) == 3
    assert len(messages) == 1 and step.budget.calls_consumed == 1


async def test_malformed_output_is_billed_and_correction_is_once_for_logical_work_item(tmp_path):
    step, messages = make_slice(tmp_path, ["not json", "not json", "not json"])
    assert (await step.advance())["correction_pending"]
    assert not (await step.advance())["correction_pending"]
    assert step.executor.work_item.format_corrections == 1
    assert step.budget.settled_actual == 30 and len(messages) == 2
    assert not step.executor.projection.examinations


async def test_correction_keeps_rejected_item_and_does_not_advance_page(tmp_path):
    step, messages = make_slice(tmp_path, [
        {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
            "evidence_ids": ["e1"], "reason": "检查"}],
         "candidates": [{"claim": "候选", "primary_anchor": "t1", "evidence_ids": ["e1"],
             "evidence_ids_note": None}]},
        {"kind": "submit", "candidates": [{"claim": "候选", "primary_anchor": "t1",
            "evidence_ids": ["e1"]}], "leads": []},
    ])
    step.executor.spec = step.executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v5"})
    step.protocol = TaskReviewV1Protocol(step.executor.spec)
    first = await step.advance()
    assert first["correction_pending"] and not first["page_advanced"]
    from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
    saved = TaskRuntimeCheckpoint._session(step)
    assert saved["rejected_items"][0]["item"]["evidence_ids_note"] is None
    step.rejected_items = []
    TaskRuntimeCheckpoint._restore_session(step, saved)
    second = await step.advance()
    payload, end = json.JSONDecoder().raw_decode(messages[1][1].content)
    suffix = json.loads(messages[1][1].content[end:])
    assert second["correction_pending"] is False
    assert suffix["rejected_items"][0]["item_type"] == "candidates"
    assert suffix["accepted_target_ids"] == ["t1"]
    assert payload["goals"] == [] and step.executor.projection.candidates
    assert TaskRuntimeCheckpoint._session(step)["rejected_items"] == []


async def test_candidate_correction_stays_on_original_evidence_page(tmp_path):
    import hashlib
    from app.services.review.task_context_pages import build_context_pages
    def submit_page(payload):
        return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
            "evidence_ids": payload["active_evidence_ids"], "reason": "已检查当前页"}],
            "candidates": [{"claim": "候选", "primary_anchor": "t1", "evidence_ids": payload["active_evidence_ids"],
                            "extra": None}]}
    def correct(payload):
        assert payload["goals"] == []
        return {"kind": "submit", "candidates": [{"claim": "候选", "primary_anchor": "t1",
                                                   "evidence_ids": payload["active_evidence_ids"]}]}
    step, _ = make_slice(tmp_path, [submit_page, correct])
    executor = step.executor
    executor.spec = executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v5"})
    step.protocol = TaskReviewV1Protocol(executor.spec)
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"end_line": 2})
    content = "return value\nreturn value"
    executor.evidence_catalog["e1"].update(content=content, end_line=2,
        content_hash=hashlib.sha256(content.encode()).hexdigest())
    pages = build_context_pages(executor.work_item, list(executor.goals.values()), executor.evidence_catalog,
                                capacity_chars=15, chunk_evidence=True)
    assert len(pages) == 2
    executor.bind_pages(pages)
    assert (await step.advance())["correction_pending"] and executor.page_index == 0
    assert (await step.advance())["page_advanced"] and executor.page_index == 1
    candidate = next(iter(executor.projection.candidates.values()))
    assert set(candidate.supporting_evidence) == set(pages[0].evidence_ids)


async def test_budget_denial_never_sends_sdk_request(tmp_path):
    step, messages = make_slice(tmp_path, [{}], max_calls=0)
    with pytest.raises(LLMProviderError, match="run_budget_admission_rejected"):
        await step.advance()
    assert not messages and not step.budget.attempts


async def test_scope_denial_does_not_execute_tool_or_correct_security_errors(tmp_path):
    step, _ = make_slice(tmp_path, [{"kind": "read", "tool": "file_read", "arguments": {
        "file_path": "src/private.py", "start_line": 1, "end_line": 1}, "purpose": "读取"}])
    result = await step.advance()
    assert result["errors"][0]["reason"] == "read_path_outside_work_item_scope"
    assert step.pending_read is None and not step.store.entries
    assert not result["correction_pending"]


async def test_changed_worktree_is_rejected_without_archiving_tool_evidence(tmp_path):
    step, _ = make_slice(tmp_path, [{"kind": "read", "tool": "file_read", "arguments": {
        "file_path": "src/a.py", "start_line": 1, "end_line": 1}, "purpose": "核对"}])
    await step.advance()
    (tmp_path / "src/a.py").write_text("modified body\n", encoding="utf-8")
    observed = await step.advance()
    assert observed["reason"] == "snapshot_text_mismatch" and not step.store.entries


def test_unrelated_invalid_component_does_not_erase_accepted_history(tmp_path):
    step, _ = make_slice(tmp_path, [])
    executor = step.executor
    legal = {"kind": "submit", "examinations": [{
        "target_id": "t1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "检查"}]}
    executor.parse_and_apply(legal)
    before = deepcopy(executor.projection)
    parsed = executor.parse_and_apply({"kind": "submit", "candidates": [{"claim": []}]})
    assert parsed.errors and executor.projection == before


def test_duplicate_json_keys_and_legacy_state_fields_rejected(tmp_path):
    step, _ = make_slice(tmp_path, [])
    assert step.executor.parse_and_apply('{"kind":"read","kind":"submit"}').response is None
    assert step.executor.parse_and_apply({"kind": "submit", "question_updates": []}).response is None


def test_schema_rejection_preserves_original_candidate_item_index(tmp_path):
    step, _ = make_slice(tmp_path, [])
    executor = step.executor
    candidate = {"claim": "候选", "primary_anchor": "t1", "evidence_ids": ["e1"]}
    result = executor.parse_and_apply({"kind": "submit", "candidates": [{"invalid": True}, candidate]})
    assert result.item_indices["candidates"] == [1]
    assert list(executor.projection.candidates) == [executor._item_identity("candidate", "1")]


@pytest.mark.parametrize("mutation,error", [
    ("archived", "unknown_or_inactive_evidence"),
    ("head", "stale_evidence_snapshot"),
    ("hash", "evidence_content_hash_mismatch"),
    ("range", "incomplete_goal_evidence"),
])
def test_submit_rejects_stale_inactive_tampered_and_partial_evidence(tmp_path, mutation, error):
    step, _ = make_slice(tmp_path, [])
    executor = step.executor
    if mutation == "archived":
        executor.active_evidence_ids.clear()
    elif mutation == "head":
        executor.evidence_catalog["e1"]["head_sha"] = "c" * 40
    elif mutation == "hash":
        executor.evidence_catalog["e1"]["content"] = "changed"
    else:
        executor.goals["t1"] = executor.goals["t1"].model_copy(update={"end_line": 2})
    result = executor.parse_and_apply({"kind": "submit", "examinations": [{
        "target_id": "t1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "检查"}]})
    assert result.errors[0].reason == error and not executor.projection.examinations


def test_conflicting_target_all_rejected_and_repeated_attempt_is_idempotent(tmp_path):
    step, _ = make_slice(tmp_path, [])
    executor = step.executor
    legal = {"target_id": "t1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "检查"}
    invalid = {**legal, "evidence_ids": ["archived"], "disposition": "blocked"}
    assert len(executor.parse_and_apply({"kind": "submit", "examinations": [legal, invalid]}).errors) == 2
    assert not executor.projection.examinations
    candidate = {"claim": "候选", "primary_anchor": "t1", "evidence_ids": ["e1"]}
    submit = {"kind": "submit", "examinations": [legal], "candidates": [candidate]}
    executor.parse_and_apply(submit)
    before = deepcopy(executor.projection)
    executor.parse_and_apply(submit)
    assert executor.projection == before
