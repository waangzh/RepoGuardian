import hashlib
import json
from copy import deepcopy

import pytest

from app.agents.providers import LLMProviderError
from app.models.review_runtime import ReviewRunSpec, WorkItemKind
from app.services.publication_planner import PublicationPlanner
from app.services.model_request_budgeter import ModelRequestProfile
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_review_outcome import completed_task
from test_task_review_sdk_slice import make_slice


def investigation_task():
    task = completed_task(initial_scope_coverage=[{"goal_id": "g", "target_ref": "src/stats.py",
        "target_kind": "source_range", "start_line": 390, "end_line": 464, "status": "examined",
        "reason": "已检查区间及边界分支"}], admitted_investigations=[
            *[{"status": "answered", "answer": "supported"} for _ in range(27)],
            {"work_item_id": "i1", "proposition": "嵌套调用返回值消费是否兼容", "status": "insufficient_evidence",
             "answer": "insufficient_evidence", "reason": "缺少外层 weighted_posterior 实现",
             "limitations": ["未读取 weighted_posterior 如何消费 wilson 返回值"]}],
        work_items=[{"work_item_id": "i1", "evidence_requests": [
            {"file_path": "src/stats.py", "start_line": 129, "end_line": 158}]}])
    task.status = "completed_with_warnings"
    return task


def test_user_report_explains_actual_gap_and_action_without_embedding_summary():
    plan = PublicationPlanner().build(investigation_task())
    summary, user = plan.summary_markdown, plan.user_report_markdown
    assert summary not in user
    assert "已检查区间及边界分支" in user and "已检查区间及边界分支" not in summary
    assert "已记录调查 28 项；有明确答案 27 项；未得到充分答案 1 项" in user
    assert "缺少外层 weighted\\_posterior 实现" in user
    assert "129–158" in user and "请求范围不代表已读取或已核验" in user
    assert "补读上述缺失实现" in user and "实际运行记录：completed / plan\\_exhausted" in user
    assert "investigation_incomplete" in summary and "termination_incomplete" not in summary
    assert "运行可靠性：可靠" in summary
    assert "缺少外层" not in summary
    assert plan.outcome.kind == "inconclusive" and plan.outcome.incomplete_investigations == 1


def test_gap_text_is_escaped_and_missing_runtime_stays_unknown():
    task = investigation_task()
    item = task.task_runtime_report["admitted_investigations"][-1]
    item.update(reason="<script>@everyone ![x](https://evil)", limitations=["<b>gap</b>"])
    user = PublicationPlanner().build(task).user_report_markdown
    assert "<script>" not in user and "@everyone" not in user and "![x]" not in user
    assert "&lt;script&gt;" in user and "&#64;everyone" in user
    task.task_runtime_report = None
    user = PublicationPlanner().build(task).user_report_markdown
    assert "调查状态未知" in user and "有明确答案 0 项" not in user


def test_unknown_investigation_answer_stays_incomplete_without_render_failure():
    task = investigation_task()
    task.task_runtime_report["admitted_investigations"][-1].update(status="answered", answer=["future"])
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "inconclusive" and plan.outcome.incomplete_investigations == 1
    assert "未得到充分答案 1 项" in plan.user_report_markdown


def investigation_slice(tmp_path, responses, *, version="task-review-prompt-v6", max_calls=5):
    step, messages = make_slice(tmp_path, responses, max_calls=max_calls)
    step.provider._profile_override = ModelRequestProfile(context_window=32_768)
    source = ["iv = weighted_posterior([(wilson(passed, total), 1.0)])",
        "assert iv.low <= iv.point <= iv.high", "def weighted_posterior(pairs):", "    return pairs[0][0]",
        "def wilson(passed, total):", "    return Interval(passed, total)"]
    (tmp_path / "src/a.py").write_text("\n".join(source), encoding="utf-8")
    step.git.get_file_content_at_revision = lambda *args: "\n".join(source)
    executor = step.executor
    executor.spec = ReviewRunSpec.model_validate({**executor.spec.model_dump(), "prompt_version": version})
    executor.work_item.kind = WorkItemKind.investigate
    executor.work_item.proposition = "核对嵌套调用及外层返回值消费"
    executor.work_item.required_evidence_ids = ["e1", "e2"]
    executor.work_item.evidence_requests = []
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"kind": WorkItemKind.investigate, "end_line": 2})
    for identity, start, end in [("e1", 1, 2), ("e2", 5, 6), ("archived", 3, 4)]:
        content = "\n".join(source[start-1:end])
        executor.evidence_catalog[identity] = {**deepcopy(executor.evidence_catalog["e1"]),
            "evidence_id": identity, "file_path": "src/a.py", "file": "src/a.py", "start_line": start,
            "end_line": end, "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest()}
    executor.active_evidence_ids = {"e1", "e2"}
    step.protocol = TaskReviewV1Protocol(executor.spec)
    return step, messages


@pytest.mark.parametrize("answer", ["supported", "refuted", "insufficient_evidence"])
async def test_investigation_can_read_outer_consumer_and_resume_without_forcing_answer(tmp_path, answer):
    def read_missing(payload):
        requirements = payload["investigation_evidence_requirements"]
        assert "first request it with kind=read" in requirements["instruction"]
        assert "archived" not in payload["active_evidence_ids"]
        target = requirements["available_source_ranges"][0]
        assert target == {"file_path": "src/a.py", "start_line": 3, "end_line": 4}
        return {"kind": "read", "tool": "file_read", "arguments": target, "purpose": "核对外层返回值消费"}

    def submit(payload):
        assert any("def weighted_posterior" in item["content"] for item in payload["active_evidence"])
        return {"kind": "submit", "investigation": {"answer": answer,
            "evidence_ids": payload["active_evidence_ids"], "reason": "根据补读原文重新判断原命题",
            "limitations": ["仍缺少外部契约"] if answer == "insufficient_evidence" else []}}

    step, messages = investigation_slice(tmp_path, [read_missing, submit])
    assert not (await step.advance())["errors"]
    assert step.executor.projection.investigation is None
    assert (await step.advance())["status"] == "completed"
    saved = TaskRuntimeCheckpoint._session(step)
    step.executor.active_evidence_ids = set()
    TaskRuntimeCheckpoint._restore_session(step, saved)
    assert not (await step.advance())["errors"]
    assert step.executor.projection.investigation.answer.value == answer
    assert step.budget.calls_consumed == 2 and step.budget.settled_actual == 30 and len(messages) == 2


def test_v6_discovery_hints_do_not_expand_scope_or_change_old_prompt(tmp_path):
    step, _ = investigation_slice(tmp_path, [])
    executor = step.executor
    for key, changes in {"outside": {"file_path": "src/private.py"}, "stale": {"head_sha": "f" * 40},
                         "base": {"side": "base"}}.items():
        executor.evidence_catalog[key] = {**executor.evidence_catalog["archived"], **changes}
    def prepare(version):
        spec = executor.spec.model_copy(update={"prompt_version": version})
        return json.loads(TaskReviewV1Protocol(spec).prepare(executor.work_item, executor.current_goals(),
            active_evidence_ids=executor.active_evidence_ids, evidence_catalog=executor.evidence_catalog).prompt)
    old, new = prepare("task-review-prompt-v5"), prepare("task-review-prompt-v6")
    assert "available_source_ranges" not in old["investigation_evidence_requirements"]
    assert new["investigation_evidence_requirements"]["available_source_ranges"] == [
        {"file_path": "src/a.py", "start_line": 3, "end_line": 4}]
    assert old["active_evidence"] == new["active_evidence"] and old["response"] == new["response"]
    assert executor.active_evidence_ids == {"e1", "e2"}


@pytest.mark.parametrize("failure", ["snapshot_changed", "text_changed", "budget"])
async def test_supplemental_read_failure_never_creates_a_supported_result(tmp_path, failure):
    read = {"kind": "read", "tool": "file_read", "arguments": {
        "file_path": "src/a.py", "start_line": 3, "end_line": 4}, "purpose": "补读外层实现"}
    insufficient = {"kind": "submit", "investigation": {"answer": "insufficient_evidence",
        "evidence_ids": ["e1", "e2"], "reason": "补读失败，缺少外层实现", "limitations": ["无法确认调用链"]}}
    step, messages = investigation_slice(tmp_path, [read, insufficient], max_calls=1 if failure == "budget" else 5)
    await step.advance()
    if failure == "snapshot_changed":
        step.git._run = lambda *args: "f" * 40
    elif failure == "text_changed":
        (tmp_path / "src/a.py").write_text("changed\n" * 6, encoding="utf-8")
    observation = await step.advance()
    assert step.executor.projection.investigation is None
    if failure == "budget":
        with pytest.raises(LLMProviderError, match="run_budget_admission_rejected"):
            await step.advance()
        assert len(messages) == 1 and step.executor.projection.investigation is None
    else:
        assert observation["status"] == "rejected" and not step.store.entries
        await step.advance()
        assert step.executor.projection.investigation.answer.value == "insufficient_evidence"
