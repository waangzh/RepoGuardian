from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.api import reviews
from app.main import app
from app.models.review import TestRunResult as RunResult, ValidationSnapshot
from app.services.publication.publication_planner import PublicationPlanner
from app.services.review.review_findings import finding_views
from app.services.review.runtime.review_runtime import TaskReviewRuntime
from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
from app.services.review.task_review_projection import project_task_runtime
from app.tools.github.github_tool import GitHubToolError
from tests.support.review_tasks import publishable_task as publishable_task
from tests.support.review_tasks import completed_task as completed_task
from tests.support.runtime_harness import make_slice


@pytest.mark.parametrize("strong,expected", [(False, "medium"), (True, "high")])
async def test_effective_severity_agrees_across_api_digest_comment_and_merge(monkeypatch, strong, expected):
    task = publishable_task(claim="核心功能鉴权失败", severity="high")
    if strong:
        task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.9
    original = deepcopy(task.task_runtime_report)
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(get_task=lambda _: task, finding_dispositions=lambda _: {}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        views = (await client.get(f"/api/reviews/{task.id}/findings")).json()["items"]
        plan = (await client.get(f"/api/reviews/{task.id}/publication-plan")).json()
    assert views[0]["severity"] == plan["digest"]["findings"][0]["severity"] == plan["inline_drafts"][0]["severity"] == expected
    assert f"**[{expected.title()}" in plan["review_comment_markdown"]
    decision = plan["digest"]["merge_decision"]
    assert decision["status"] == ("fix_blockers" if strong else "conditional_merge")
    assert decision["blocking_finding_keys"] == (["key-a"] if strong else [])
    assert task.task_runtime_report == original
    assert finding_views(task, {"key-a": {"current": {"status": "resolved", "reason": "人工标记"}}})[0].severity == expected


@pytest.mark.parametrize("kind,passed,expected", [("code_regression", False, "failed"),
    ("dependency_missing", False, "unknown"), ("test_collection_error", False, "unknown"),
    ("timeout", False, "unknown"), ("infrastructure", False, "unknown"), ("unknown", False, "unknown"),
    (None, False, "failed"), (None, True, "passed"), ("code_regression", True, "unknown")])
def test_failure_classification_keeps_known_failure_distinct_from_unknown(kind, passed, expected):
    task = completed_task()
    task.validation_snapshots = [ValidationSnapshot(stage="head", sha=task.run_manifest.head_sha,
        passed=passed, failure_kind=kind, command_results=[RunResult(tool="test_runner", command="pytest",
            exit_code=0 if passed else 1, passed=passed)])]
    plan = PublicationPlanner().build(task)
    assert plan.outcome.dynamic_validation == plan.digest.validation_state == plan.digest.validations[0].status == expected
    for text in (plan.review_comment_markdown, plan.summary_markdown):
        assert ("已有与当前 Head 绑定的验证失败记录" in text) is (expected == "failed")
        if expected == "unknown":
            assert text.count("动态验证状态未知") == 1
    if expected == "failed":
        assert "已有与当前 Head 绑定的验证失败记录" in plan.review_comment_markdown
        assert "head_validation_failed" in plan.digest.merge_decision.unresolved_reasons
    elif expected == "unknown":
        assert "head_validation_unknown" in plan.digest.merge_decision.unresolved_reasons


@pytest.mark.parametrize("bad", ["sha", "empty", "inconsistent"])
def test_known_regression_does_not_bypass_identity_or_command_checks(bad):
    task = completed_task()
    snapshot = ValidationSnapshot(stage="head", sha=task.run_manifest.head_sha, passed=False,
        failure_kind="code_regression", command_results=[RunResult(tool="test_runner", command="pytest", exit_code=1, passed=False)])
    if bad == "sha":
        snapshot.sha = "d" * 40
    elif bad == "empty":
        snapshot.command_results = []
    else:
        snapshot.command_results[0].exit_code = 0
    task.validation_snapshots = [snapshot]
    plan = PublicationPlanner().build(task)
    expected = "not_executed" if bad == "sha" else "unknown"
    assert plan.outcome.dynamic_validation == expected
    for text in (plan.review_comment_markdown, plan.summary_markdown):
        assert "已有与当前 Head 绑定的验证失败记录" not in text
        assert ("动态验证状态未知" in text) is (expected == "unknown")


@pytest.mark.parametrize("disposition", ["examined", "blocked"])
async def test_accepted_not_applicable_without_evidence_is_preserved(tmp_path, disposition):
    def response(payload):
        return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": disposition,
            "evidence_ids": ["e1"], "reason": "当前范围与目标无关", "intent_assessments": [{
                "author_quote": payload["author_targets"][0], "actual_behavior": "本目标仅涉及文档，当前范围为实现代码",
                "alignment": "not_applicable", "evidence_ids": [], "limitations": []}]}], "candidates": [], "leads": []}
    step, _ = make_slice(tmp_path, [response], max_calls=1)
    step.executor.spec = step.executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v6",
        "pr_intent_context": "更新文档。", "intent_assessment_contract": "structured-intent-v1"})
    step.protocol = TaskReviewV1Protocol(step.executor.spec)
    runtime = TaskReviewRuntime([step.executor.work_item])
    result = await runtime.advance_one({"w1": step})
    assert not result["errors"]
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()), termination_reason="plan_exhausted")
    task = completed_task(**report.model_dump(mode="json"))
    task.run_manifest.effective_base_sha = step.executor.spec.snapshot_base_sha
    plan = PublicationPlanner().build(task)
    assert plan.digest.intent_assessments[0].status == "not_applicable"
    # 逐目标对照属于详细报告；评论只保留变更概述与其他审查结果。
    assert "当前检查不适用" in plan.user_report_markdown
    assert "author_intent_unresolved" not in plan.digest.merge_decision.unresolved_reasons
    goal = task.task_runtime_report["initial_scope_coverage"][0]
    original = deepcopy(goal)
    for key, value in (("snapshot_head_sha", "d" * 40), ("snapshot_base_sha", "d" * 40), ("result_id", None)):
        goal.update(original)
        goal[key] = value
        assert PublicationPlanner().build(task).digest.intent_assessments[0].status == "insufficient"
    for alignment, identities in (("not_applicable", ["foreign"]), ("supported", []), ("contradicted", [])):
        goal.update(deepcopy(original))
        goal["intent_assessments"][0].update(alignment=alignment, evidence_ids=identities)
        assert PublicationPlanner().build(task).digest.intent_assessments[0].status == "insufficient"


@pytest.mark.parametrize("remote,state", [("a" * 40, "current"), ("d" * 40, "stale"), (None, "unknown")])
async def test_single_finding_copy_uses_latest_server_body_and_head_check(monkeypatch, remote, state):
    task = publishable_task()
    other = publishable_task(candidate_id="candidate-b", claim="另一个问题").task_runtime_report["findings"][0]
    other["key"] = "key-b"
    task.task_runtime_report["findings"].append(other)
    before = deepcopy(task.task_runtime_report)
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(get_task=lambda _: task,
        finding_dispositions=lambda _: {"key-a": {"current": {"status": "resolved", "reason": "最新人工处理说明"}}}))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        if remote is None:
            raise GitHubToolError("github_connection_failed")
        return SimpleNamespace(head=SimpleNamespace(sha=remote))
    monkeypatch.setattr(reviews.GitHubTool, "fetch_pr", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/api/reviews/{task.id}/review-comment-check", json={"finding_key": "key-a"})
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == state and calls == [task.pr_url]
    assert body["included_finding_keys"] == ["key-a"] and body["omitted_finding_keys"] == ["key-b"]
    text = body["review_comment_markdown"]
    assert "空返回值破坏调用方契约" in text and "最新人工处理说明" in text
    if state == "current":
        assert "快照检查" not in text
    else:
        assert "快照检查" in text
    assert "另一个问题" not in text and "另有 1 项" in text and "src/a\\.py" in text
    assert task.task_runtime_report == before


@pytest.mark.parametrize("case,expected", [("missing", 409), ("withheld", 409), ("mixed", 422)])
async def test_single_finding_copy_rejects_unavailable_or_conflicting_selection_before_network(monkeypatch, case, expected):
    task = publishable_task()
    if case == "withheld":
        task.task_runtime_report["findings"][0].pop("publication")
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(
        get_task=lambda _: task, finding_dispositions=lambda _: {}))
    async def fetch(*args):
        pytest.fail("invalid selection must not query GitHub")
    monkeypatch.setattr(reviews.GitHubTool, "fetch_pr", fetch)
    options = {"finding_key": "missing" if case == "missing" else "key-a"}
    if case == "mixed":
        options["exclude_resolved"] = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/api/reviews/{task.id}/review-comment-check", json=options)
    assert result.status_code == expected
