from types import SimpleNamespace

from app.models.review_runtime import IntentAssessment
from app.services.publication_planner import PublicationPlanner
from app.services.review_prose import concise_text
from app.services.review_task_executor import ReviewTaskExecutor
from test_intent_response_recovery import payload_for, slice_with_intent
from test_intent_context_completeness import spec_for
from test_publication_planner import publishable_task
from test_review_outcome import completed_task


def test_bound_goal_summary_and_separate_test_observations():
    task = publishable_task()
    title = "fix(stats): the point estimate sat outside its own interval"
    task.task_runtime_report["pr_intent_context"] = title
    goal = task.task_runtime_report["initial_scope_coverage"][0]
    goal.update(target_ref="src/stats.py", target_kind="source_range", status="examined",
        result_id="result-1", snapshot_head_sha=task.run_manifest.head_sha,
        snapshot_base_sha=task.run_manifest.effective_base_sha, evidence_ids=["e1"],
        intent_assessments=[IntentAssessment(author_quote=title, actual_behavior="调整区间上下界。",
            author_goal_summary="修复点估计落在报告区间之外的问题。",
            alignment="supported", evidence_ids=["e1"]).model_dump(mode="json")])
    test_goal = {**goal, "goal_id": "test-goal", "target_ref": "tests/test_stats.py",
        "intent_assessments": [IntentAssessment(author_quote=title, actual_behavior="断言区间包含点估计。",
            alignment="supported", evidence_ids=["e1"]).model_dump(mode="json")]}
    task.task_runtime_report["initial_scope_coverage"].append(test_goal)
    plan = PublicationPlanner().build(task)
    text = plan.review_comment_markdown
    assert "**PR 目标：** 修复点估计落在报告区间之外的问题。" in text
    assert "**实现变化：** 调整区间上下界。" in text
    assert "**测试核对：** 断言区间包含点估计。" in text
    assert "本次 Diff 共" not in text and "。；" not in text and "。。" not in text
    assert "未执行目标仓库代码、测试或构建" in text
    goal["snapshot_head_sha"] = "d" * 40
    assert "修复点估计落在报告区间之外的问题" not in PublicationPlanner().build(task).review_comment_markdown


def test_legacy_summary_serialization_and_contract_rejection():
    assessment = IntentAssessment(author_quote="修复空值。", actual_behavior="尚未核实",
        alignment="insufficient")
    assert "author_goal_summary" not in assessment.model_dump(mode="json")
    assessment.author_goal_summary = "修复空值处理问题。"
    executor = object.__new__(ReviewTaskExecutor)
    executor.spec = SimpleNamespace(intent_assessment_contract="structured-intent-v3")
    assert executor._intent_assessment_error(None, None, assessment, []) == "intent_summary_contract_not_enabled"
    executor.spec = SimpleNamespace(intent_assessment_contract="structured-intent-v4")
    assert executor._intent_assessment_error(None, SimpleNamespace(evidence_ids=[]),
        assessment, ["修复空值。"]) is None


def test_summary_schema_is_only_exposed_to_new_frozen_contract(tmp_path):
    for contract in ("structured-intent-v2", "structured-intent-v3", "structured-intent-v4"):
        step, _ = slice_with_intent(tmp_path / contract, [], contract=contract)
        schema = payload_for(step)["response"]["one_of"][1]["$defs"]["IntentAssessment"]
        assert ("author_goal_summary" in schema["properties"]) is (contract == "structured-intent-v4")
    assert spec_for("修复空值。", "pr-intent-assessment-v5").intent_assessment_contract == "structured-intent-v4"
    assert spec_for("修复空值。", "pr-intent-assessment-v4").intent_assessment_contract == "structured-intent-v3"


def test_comment_sentence_boundaries_and_single_scope_notice():
    assert concise_text("实现变化。；测试核对。。") == "实现变化。"
    assert "weight" not in concise_text("`" + "weighted_posterior" * 30, 80)
    assert "…" not in concise_text("word " * 100, 80)
    plan = PublicationPlanner().build(completed_task())
    assert plan.review_comment_markdown.count("不等于代码无缺陷") == 1
    assert plan.review_comment_markdown.count("不替代 CI 与人工审批") == 1
