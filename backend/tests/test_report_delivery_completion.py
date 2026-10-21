import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.api import reviews
from app.main import app
from app.models.review import TestRunResult as RunResult, ValidationSnapshot, ValidationResult
from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import render_review_comment
from app.services.review_runtime import TaskReviewRuntime
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.services.task_context_pages import build_context_pages
from app.services.task_review_projection import project_task_runtime
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_phase6a_persistence import persistence as persistence
from test_publication_planner import publishable_task
from test_review_outcome import completed_task
from test_task_review_sdk_slice import make_slice


def intent_submit(payload):
    return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
        "evidence_ids": payload["active_evidence_ids"], "reason": "核对当前页返回路径",
        "intent_assessments": [{"author_quote": payload["author_targets"][0],
            "actual_behavior": "当前源码保留返回值", "alignment": "supported",
            "evidence_ids": payload["active_evidence_ids"], "limitations": ["仅当前页，异常路径未核验"]}]}],
        "candidates": [], "leads": []}


def enable_intent(step):
    step.executor.spec = step.executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v6",
        "pr_intent_context": "保留返回值。\n拒绝空值。", "intent_assessment_contract": "structured-intent-v1"})
    step.protocol = TaskReviewV1Protocol(step.executor.spec)


@pytest.mark.parametrize("truncated", [None, False, True])
async def test_structured_intent_survives_pages_and_checkpoint_with_same_budget(tmp_path, persistence, truncated):
    step, messages = make_slice(tmp_path, [intent_submit, intent_submit])
    enable_intent(step)
    step.executor.spec = step.executor.spec.model_copy(update={"pr_intent_context_truncated": truncated})
    step.protocol = TaskReviewV1Protocol(step.executor.spec)
    executor = step.executor
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"end_line": 2})
    content = "first\nsecond"
    executor.evidence_catalog["e1"].update(content=content, end_line=2,
        content_hash=hashlib.sha256(content.encode()).hexdigest())
    executor.bind_pages(build_context_pages(executor.work_item, list(executor.goals.values()),
        executor.evidence_catalog, capacity_chars=6))
    repository = persistence[0]
    repository.create_task(completed_task())
    runtime = TaskReviewRuntime([executor.work_item])
    checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
    await checkpoint.restore()
    await runtime.advance_one({"w1": step})
    assert not executor.projection.examinations and executor.page_index == 1
    executor.page_index = 0
    executor.page_examinations = {}
    await TaskRuntimeCheckpoint(runtime, {"w1": step}, repository).restore()
    assert executor.page_index == 1
    assert checkpoint.spec.get("pr_intent_context_truncated") is truncated
    await runtime.advance_one({"w1": step})
    result = executor.projection.examinations["t1"]
    assert len(result.intent_assessments) == 2 and len(messages) == step.budget.calls_consumed == 2
    assert step.budget.settled_actual == 30
    report = project_task_runtime(runtime, {"w1": step}, list(executor.goals.values()), termination_reason="plan_exhausted")
    assert report.pr_intent_context_truncated is truncated
    task = completed_task(**report.model_dump(mode="json"))
    task.run_manifest.effective_base_sha = executor.spec.snapshot_base_sha
    plan = PublicationPlanner().build(task)
    assert [item.status for item in plan.digest.intent_assessments] == ["observed", "not_checked"]
    assert len(plan.digest.intent_assessments[0].observations) == 2
    # 逐项观察的限制只留在详细报告；评论仅保留影响合并判断的缺口。
    assert "异常路径未核验" in plan.user_report_markdown
    assert "异常路径未核验" not in plan.review_comment_markdown
    # 未逐项核验的作者声明是辅助信息，不再单独把结论降为证据缺口情景。
    expected = "conditional_merge" if truncated is False else "resolve_unknowns"
    assert plan.digest.merge_decision.status == expected
    assert plan.digest.author_context_status == ("unknown" if truncated is None else "truncated" if truncated else "complete")
    task.task_runtime_report["initial_scope_coverage"][0]["snapshot_base_sha"] = "d" * 40
    assert PublicationPlanner().build(task).digest.intent_assessments[0].status == "insufficient"


@pytest.mark.parametrize("bad", ["quote", "evidence", "missing"])
async def test_intent_rejects_invalid_bindings_without_promoting_inspection(tmp_path, bad):
    def invalid(payload):
        response = intent_submit(payload)
        item = response["examinations"][0]
        if bad == "quote":
            item["intent_assessments"][0]["author_quote"] = "作者没有声明的目标"
        elif bad == "evidence":
            item["intent_assessments"][0]["evidence_ids"] = ["unknown"]
        elif bad == "missing":
            item["intent_assessments"] = []
        return response
    step, messages = make_slice(tmp_path, [invalid])
    enable_intent(step)
    response = await step.advance()
    assert response["errors"] and not step.executor.projection.examinations
    assert len(messages) == step.budget.calls_consumed == 1


async def test_structured_intent_keeps_zero_budget_and_stale_evidence_rejection(tmp_path):
    from app.agents.providers import LLMProviderError
    step, messages = make_slice(tmp_path, [], max_calls=0)
    enable_intent(step)
    with pytest.raises(LLMProviderError, match="run_budget_admission_rejected"):
        await step.advance()
    assert not messages and not step.budget.attempts
    step.executor.evidence_catalog["e1"]["head_sha"] = "d" * 40
    assert step.executor.evidence_error(["e1"]) == "stale_evidence_snapshot"


@pytest.mark.parametrize("stage,sha,passed,expected", [("head", "a", True, "passed"),
    ("head", "a", False, "failed"), ("head", "d", True, "unknown"), ("base", "c", True, "unknown")])
def test_validation_stage_identity_and_failure_remain_separate(stage, sha, passed, expected):
    task = completed_task()
    task.validation_snapshots = [ValidationSnapshot(stage=stage, sha=sha * 40, passed=passed,
        command_results=[RunResult(tool="test_runner", command="pytest", exit_code=0 if passed else 1, passed=passed)])]
    plan = PublicationPlanner().build(task)
    assert plan.digest.validation_state == plan.outcome.dynamic_validation == expected
    if expected in {"failed", "unknown"}:
        assert plan.digest.merge_decision.status == "resolve_unknowns"


@pytest.mark.parametrize("trusted,bound,expected", [(True, True, "independent_results"),
    (False, True, "unknown"), (True, False, "unknown")])
def test_patch_success_never_becomes_head_success(trusted, bound, expected):
    from app.services.review_validation_digest import validation_digest
    result = ValidationResult(id="v", backend="user_runner", status="passed", trusted=trusted,
        trust_source="user_runner", head_sha="a" * 40, patch_sha="hash", patch_id="p")
    task = SimpleNamespace(run_manifest=SimpleNamespace(head_sha="a" * 40, effective_base_sha="c" * 40),
        validation_snapshots=[], validation=[result], patches=[SimpleNamespace(id="p", head_sha="a" * 40,
            patch_sha="hash" if bound else "other", validation_result_id="v")])
    state, records = validation_digest(task)
    assert state == expected and state != "passed" and records[0].stage == "patched"


def test_blockers_withheld_and_human_resolved_do_not_disappear():
    task = publishable_task(severity="high")
    record = task.task_runtime_report["findings"][0]
    record.pop("publication")
    plan = PublicationPlanner().build(task, dispositions={"key-a": {"current": {"status": "resolved", "reason": "已修改"}}})
    assert plan.digest.merge_decision.status == "fix_blockers"
    assert plan.digest.merge_decision.blocking_finding_keys == ["key-a"]
    assert "confirmed_findings_withheld" in plan.digest.merge_decision.unresolved_reasons


async def test_copy_selection_is_explicit_keeps_blockers_and_rechecks_head(monkeypatch):
    task = publishable_task()
    saved = {"key-a": {"current": {"status": "resolved", "reason": "人工已处理"}}}
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(get_task=lambda _: task, _dispositions={task.id: saved}))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        return SimpleNamespace(head=SimpleNamespace(sha="a" * 40))
    monkeypatch.setattr(reviews.GitHubTool, "fetch_pr", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        options = {"detail": "full", "exclude_resolved": True}
        preview = (await client.post(f"/api/reviews/{task.id}/review-comment-preview", json=options)).json()
        assert not calls and preview["omitted_finding_keys"] == ["key-a"]
        assert "本次未包含的事项" in preview["review_comment_markdown"]
        assert "**问题与影响：**" not in preview["review_comment_markdown"]
        copied = (await client.post(f"/api/reviews/{task.id}/review-comment-check", json=options)).json()
        assert calls == [task.pr_url] and copied["status"] == "current"
        assert copied["detail"] == "full" and copied["omitted_finding_keys"] == ["key-a"]
        blocking = publishable_task(claim="核心功能鉴权失败", severity="high")
        blocking.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.9
        task.task_runtime_report = blocking.task_runtime_report
        retained = (await client.post(f"/api/reviews/{task.id}/review-comment-preview", json=options)).json()
        assert retained["retained_blocking_keys"] == ["key-a"] and not retained["omitted_finding_keys"]
        assert "核心功能鉴权失败" in retained["review_comment_markdown"]
        assert (await client.post(f"/api/reviews/{task.id}/review-comment-check", json={"finding_keys": []})).status_code == 422


def test_compact_is_publishable_and_full_adds_evidence_without_duplicates():
    task = publishable_task()
    plan = PublicationPlanner().build(task)
    original = deepcopy(plan)
    finding = plan.inline_drafts[0]
    compact = render_review_comment(plan, detail="compact")
    full = render_review_comment(plan, detail="full")
    for markdown in (compact, full):
        assert finding.title in markdown and finding.recommendation in markdown
        assert markdown.count("本结论基于提交") == 1
        assert "独立核验支持此结论" not in markdown
    assert "代码证据" not in compact and "代码证据" in full
    assert len(full) > len(compact)
    assert plan == original
    assert json.loads(plan.model_dump_json())["digest"]["merge_decision"]["status"] == "conditional_merge"
