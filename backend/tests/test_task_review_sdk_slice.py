import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider, LLMProviderError
from app.models.review import ReviewToolScope
from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem
from app.services.review_run_budget import ReviewRunBudget
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_step import InspectTaskStep
from runtime_v1_fixtures import evidence_catalog


def make_slice(tmp_path, responses, *, max_calls=5):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("return value\n", encoding="utf-8")
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="manifest", model_provider="openai-compatible", model_name="fixture",
        policy_version="p1", max_tokens=100_000, max_calls=max_calls)
    goal = ReviewGoal(goal_id="t1", unit_id="u1", kind="inspect", target_ref="src/a.py")
    work = ReviewWorkItem(work_item_id="w1", kind="inspect", goal_ids=["t1"], scope_files=["src/a.py"])
    executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids={"e1"},
        evidence_catalog=evidence_catalog("e1"), request_id="initial")
    provider = OpenAICompatibleProvider("stub-key", "https://example.test/v1", "fixture",
                                       request_attempts=1)
    messages = []

    class SDKChat:
        async def ainvoke(self, payload):
            messages.append(payload)
            response = responses.pop(0)
            if callable(response):
                response = response(json.loads(payload[1].content.split("\n")[0]))
            return AIMessage(content=response if isinstance(response, str) else json.dumps(response),
                             usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    class SnapshotGit:
        _git = "git"

        def _run(self, argv):
            return "a" * 40

        def get_file_content_at_revision(self, *args):
            return "return value\n"

    provider._build_chat_model = lambda *args: SDKChat()
    scope = ReviewToolScope(review_unit_id="w1", commentable_files={"src/a.py"},
        readable_files={"src/a.py"}, repository_root=str(tmp_path),
        max_lines_per_read=240, max_search_results=12)
    budget = ReviewRunBudget("run", max_tokens=spec.max_tokens, max_calls=max_calls)
    step = InspectTaskStep(executor, provider, budget, scope, git_tool=SnapshotGit())
    return step, messages


async def test_sdk_stub_read_tool_submit_uses_real_provider_budget_and_store(tmp_path):
    from app.services.review_runtime import TaskReviewRuntime
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


async def test_malformed_output_is_billed_and_correction_is_once_for_logical_work_item(tmp_path):
    step, messages = make_slice(tmp_path, ["not json", "not json", "not json"])
    assert (await step.advance())["correction_pending"]
    assert not (await step.advance())["correction_pending"]
    assert step.executor.work_item.format_corrections == 1
    assert step.budget.settled_actual == 30 and len(messages) == 2
    assert not step.executor.projection.examinations


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
