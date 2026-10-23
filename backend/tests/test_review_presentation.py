from copy import deepcopy

import pytest

from app.models.publication import PublicationPlan
from app.services.fingerprints import stable_hash
from app.services.publication_planner import PublicationPlanner
from app.services.publication_repository import finding_fingerprint
from app.services.publication_worker import compose_summary
from test_publication_planner import publishable_task
from test_phase6a_persistence import persistence as persistence
from test_review_outcome import completed_task


def new_output(task):
    task.task_runtime_configuration = {"output_policy": {"presentation_v1": True}}
    return task


def test_dual_output_uses_same_facts_and_legacy_fingerprint():
    task = publishable_task(severity="high")
    old = PublicationPlanner().build(task)
    plan = PublicationPlanner().build(new_output(task))
    assert plan.presentation.verdict == plan.digest.merge_decision
    assert finding_fingerprint(plan.inline_drafts[0]) == finding_fingerprint(old.inline_drafts[0])
    assert "代码证据" not in plan.inline_drafts[0].body_markdown
    assert "影响／触发" in plan.inline_drafts[0].body_markdown
    assert plan.summary_markdown.index("本次 PR 主要变更") < plan.summary_markdown.index("审查结果")
    assert plan.inline_drafts[0].body_markdown in plan.review_comment_markdown
    assert plan.inline_drafts[0].body_markdown not in plan.summary_markdown
    assert "具备发布资格" not in plan.review_comment_markdown
    assert "未执行目标仓库代码、测试或构建" in plan.summary_markdown


def test_selected_summary_fallback_keeps_full_body_and_global_verdict():
    plan = PublicationPlanner().build(new_output(publishable_task(severity="high")))
    key = plan.inline_drafts[0].finding_key
    empty = compose_summary(plan, {"finding_keys": []}, {}, [], "Prepared")
    assert "本轮已确认 1 项" in empty and "本次选择 0 项" in empty
    assert "未选择的问题仍属于审查结论" in empty
    assert "已检查范围内未发现已确认问题" not in empty
    fallback = compose_summary(plan, {"finding_keys": [key]}, {key: "unmapped"}, [], "Prepared")
    assert "仅在 Summary 中反馈" in fallback
    assert plan.inline_drafts[0].body_markdown in fallback


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_clean_and_interrupted_have_honest_boundaries(status):
    task = new_output(completed_task())
    task.status = status
    plan = PublicationPlanner().build(task)
    if status == "completed":
        assert "已检查范围内未发现已确认问题" in plan.summary_markdown
    else:
        assert "部分审查记录" in plan.summary_markdown
        assert "已检查范围内未发现已确认问题" not in plan.summary_markdown
    assert "测试通过" not in plan.summary_markdown


def test_old_frozen_plan_serialization_and_hash_remain_unchanged():
    saved = PublicationPlanner().build(publishable_task()).model_dump(mode="json")
    before = deepcopy(saved)
    restored = PublicationPlan.model_validate(saved).model_dump(mode="json")
    assert restored == before
    assert stable_hash(restored) == stable_hash(before)
    assert "presentation" not in restored
    assert "legacy_body_markdown" not in restored["inline_drafts"][0]


def test_full_copy_keeps_scope_and_safe_historical_links():
    from app.services.publication_renderer import render_review_comment
    plan = PublicationPlanner().build(new_output(publishable_task()))
    finding = plan.presentation.findings[0]
    finding.default_selection_reason = "historical_duplicate"
    finding.discussion_urls = ["https://github.com/o/r/pull/1#discussion_r42", "https://evil.invalid/"]
    compact = render_review_comment(plan)
    full = render_review_comment(plan, detail="full")
    assert "### 审查范围" in full and "### 审查范围" not in compact
    assert "https://github.com/o/r/pull/1#discussion_r42" in full
    assert "已有讨论不代表已修复" in compact
    assert "evil.invalid" not in compact


@pytest.mark.parametrize("synthesis_status", ["ready", "fallback"])
def test_author_purpose_omits_requested_editorial_labels(synthesis_status):
    from app.models.publication import ChangeUnderstanding
    from app.services.publication_renderer import render_review_comment, render_summary

    goal = "通过放宽区间边界调整点估计。"
    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.digest.change_understanding = ChangeUnderstanding(author_goal=goal, author_goal_source="summary")
    plan.presentation.change_summary = goal
    plan.presentation.purpose_text = goal
    plan.presentation.purpose_source = "author_claim"
    plan.presentation.synthesis_status = synthesis_status
    for markdown in (render_summary(plan), render_review_comment(plan),
                     render_review_comment(plan, detail="full")):
        assert goal in markdown
        assert "作者称：" not in markdown
        assert "（基于作者原文的目标归纳，不代表目标已实现）" not in markdown
        overview = markdown.split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
        assert overview.count(goal) == 1
        assert "**PR 目标：**" not in overview


@pytest.mark.parametrize("synthesis_status", ["ready", "fallback", "unavailable"])
def test_overview_is_one_paragraph_from_presentation_for_all_delivery_modes(synthesis_status):
    from app.models.publication import ChangeUnderstanding
    from app.services.publication_renderer import render_review_comment, render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    implementation = "当点估计位于区间之外时，使用 min(lo, point) 和 max(hi, point) 扩展区间，避免缩窄原有范围。"
    tests = "新增点估计包含性与区间不收窄的相关测试用例。"
    plan.presentation.purpose_text = "放宽区间边界以容纳点估计。"
    plan.presentation.change_summary = implementation
    plan.presentation.test_changes = [tests, tests]
    plan.presentation.synthesis_status = synthesis_status
    # 摘要投影已经选定，渲染不得重新拼接旧 Digest 的完整目标和核对记录。
    plan.digest.change_understanding = ChangeUnderstanding(
        author_goal="不应再输出的旧目标", implementation_changes="不应再输出的旧实现",
        test_observations="不应再输出的旧测试核对")
    overviews = []
    for markdown in (render_summary(plan), render_review_comment(plan),
                     render_review_comment(plan, detail="full")):
        overview = markdown.split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
        overviews.append(overview)
        assert "\n" not in overview
        assert "min" in overview and "max" in overview and "避免缩窄原有范围" in overview
        assert tests not in overview
        assert "不表示测试已运行" not in overview
        assert "测试通过" not in markdown
        assert all(label not in overview for label in (
            "旧目标", "旧实现", "旧测试核对", "**PR 目标", "**实现变化", "**测试核对", "摘要来源"))
    assert len(set(overviews)) == 1


@pytest.mark.parametrize(("diff_summary", "expected", "absent"), [
    ("本次 PR 共变更 2 个文件（+3 / -1），主要集中在 src（2 个文件）。",
     "本次 PR 共变更 2 个文件", "审查阶段观察"),
    (None, "尚缺少可概括实现变化的代码观察", "审查阶段观察"),
])
def test_fallback_uses_deterministic_diff_metadata(diff_summary, expected, absent):
    from app.models.publication import ChangeUnderstanding
    from app.services.publication_renderer import render_summary
    from app.services.review_presentation import build_presentation

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.digest.change_understanding = ChangeUnderstanding(
        author_goal="调整区间边界。", diff_summary=diff_summary,
        implementation_changes="代码中 lo = min(lo, point)，符合作者描述。",
        actual_changes="未保存可归纳的实际变更。",
        test_observations="补充区间包含性断言。")
    plan.presentation = build_presentation(plan)
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert expected in overview
    assert absent not in overview
    # 审查阶段的模型散文不进入对外摘要。
    assert "符合作者描述" not in overview and "补充区间包含性断言" not in overview
    assert "目标已实现" not in overview and "测试通过" not in overview


@pytest.mark.parametrize("tests", [
    "新增边界参数化测试用例。",
    "测试文件新增边界参数化测试用例。",
    "测试文件确认包含边界参数化用例，断言内容与作者描述一致。",
    "未新增边界参数化测试用例；现有断言不能证明覆盖区间不收窄的场景。",
])
def test_ready_summary_preserves_saved_observations_without_keyword_rewriting(tests):
    from app.models.publication import ChangeUnderstanding
    from app.services.publication_renderer import markdown_text, render_summary
    from app.services.review_presentation import build_presentation

    plan = PublicationPlanner().build(new_output(completed_task()))
    implementation = ("代码中 lo = min(lo, point) 与 hi = max(hi, point)"
        "确实只放宽边界，point 保持不变，符合作者描述。")
    plan.digest.change_understanding = ChangeUnderstanding(
        implementation_changes="不应进入对外摘要的审查阶段观察。", test_observations=tests)
    plan.presentation = build_presentation(plan)
    plan.presentation.change_summary = implementation
    plan.presentation.synthesis_status = "ready"
    plan.presentation.summary_source = "model"
    before = plan.model_dump(mode="json")
    markdown = render_summary(plan)
    overview = markdown.split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview == markdown_text(implementation, 1200)
    assert "测试源码变化" not in overview and "不表示测试已运行" not in overview
    assert "动态验证：未执行目标仓库代码、测试或构建" in markdown
    assert plan.digest.change_understanding.test_observations == tests
    assert plan.model_dump(mode="json") == before


def test_overview_does_not_repeat_test_observation_already_in_summary():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    tests = "新增区间包含性测试用例。"
    plan.presentation.change_summary = "调整区间边界。" + tests
    plan.presentation.test_changes = [tests]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview.count(tests) == 1
    assert "测试源码变化：" not in overview


def test_overview_does_not_restore_test_observation_outside_visible_excerpt():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    tests = "新增区间包含性测试用例。"
    plan.presentation.change_summary = "调整区间边界。" + "补充静态实现观察。" * 70 + tests
    plan.presentation.test_changes = [tests]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert tests not in overview
    assert "不表示测试已运行" not in overview


def test_overview_preserves_negation_without_appending_separate_test_observation():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.presentation.change_summary = "未新增区间包含性测试用例。"
    plan.presentation.test_changes = ["新增区间包含性测试用例。"]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview == "未新增区间包含性测试用例。"


def test_missing_purpose_and_observations_does_not_invent_changes():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.presentation.purpose_text = None
    plan.presentation.change_summary = "未保存可归纳的实际变更。"
    plan.presentation.test_changes = ["尚未形成可引用的测试断言核对记录。"]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview == "尚缺少可概括实现变化的代码观察。"


def test_summary_coverage_warns_only_for_diff_that_limits_the_summary():
    from app.models.review_synthesis import ReviewSynthesisArtifact
    from app.services.publication_renderer import render_summary, render_technical_audit
    from app.services.review_presentation import build_presentation

    plan = PublicationPlanner().build(new_output(completed_task()))
    artifact = ReviewSynthesisArtifact(
        review_id=plan.review_id, head_sha=plan.snapshot.head_sha,
        effective_base_sha=plan.snapshot.effective_base_sha, fact_bundle_hash="c" * 64,
        status="fallback", input_truncated=True, truncation_reasons=["author_context_truncated"])
    plan.presentation = build_presentation(plan, artifact)
    markdown = render_summary(plan)
    assert "变更概述仅覆盖已纳入的 Diff 片段" not in markdown
    assert "author_context_truncated" in render_technical_audit(plan, {})

    artifact = artifact.model_copy(update={
        "truncation_reasons": ["author_context_truncated", "diff_hunk_omitted"],
        "omitted_files": 1, "omitted_hunks": 2})
    plan.presentation = build_presentation(plan, artifact)
    markdown = render_summary(plan)
    audit = render_technical_audit(plan, {})
    # 覆盖披露只取决于事实包截断，不因摘要来源是回退而消失。
    assert "变更概述仅覆盖已纳入的 Diff 片段" in markdown
    plan.presentation.synthesis_status = "ready"
    markdown = render_summary(plan)
    assert "变更概述仅覆盖已纳入的 Diff 片段" in markdown
    assert "这不表示静态审查范围不完整" in markdown
    assert "diff_hunk_omitted" in audit and '"omitted_hunks": 2' in audit


def test_investigation_is_one_actionable_item_not_a_finding_or_generic_warning():
    from app.services.publication_renderer import render_review_comment, render_technical_audit
    from app.services.review_digest import execution_record

    task = completed_task(
        initial_scope_coverage=[
            {"goal_id": "g1", "target_ref": "src/stats.py", "target_kind": "source_range",
                "start_line": 390, "end_line": 464, "status": "examined", "reason": "已检查区间及边界分支"},
            {"goal_id": "g2", "target_ref": "tests/test_stats.py", "target_kind": "source_range",
                "start_line": 296, "end_line": 305, "status": "examined", "reason": "已检查回归断言"}],
        admitted_investigations=[{
            "work_item_id": "i1",
            "proposition": ("待源码核对的 calls 命题：调用方 weighted_posterior（定义范围 390-464）"
                "对 wilson（定义范围 129-158）的调用，在本次变更后仍兼容实际参数传递、返回值消费与异常处理路径"),
            "status": "insufficient_evidence", "answer": "insufficient_evidence",
            "reason": "缺少外层 weighted_posterior 实现",
            "limitations": ["未读取 weighted_posterior 如何消费 wilson 返回值"]}],
        work_items=[{"work_item_id": "i1", "evidence_requests": [
            {"file_path": "src/stats.py", "start_line": 129, "end_line": 158}]}])
    task.task_runtime_configuration = {"output_policy": {"presentation_v1": True}}
    plan = PublicationPlanner().build(task)
    markdown = render_review_comment(plan)
    assert plan.outcome.confirmed_count == 0 and plan.presentation.findings == []
    assert "未发现已确认代码问题，但有 1 项关联调查尚未完成" in markdown
    assert "## 待核实事项（1 项）" in markdown
    assert "### weighted\\_posterior → wilson 调用兼容性核验" in markdown
    assert "**疑点：** 尚不能确认调用双方的参数传递、返回值消费与异常处理路径是否兼容。" in markdown
    assert "**缺少的证据：** 未读取 weighted\\_posterior 如何消费 wilson 返回值" in markdown
    assert "**建议：** 补查调用双方实现，核对参数传递、返回值消费与异常处理" in markdown
    assert "仍有未完成或证据不足的调查" not in markdown
    assert "调查未得到充分答案" not in markdown
    assert "未决调查" not in markdown
    assert "129–158" not in markdown and "定义范围 390-464" not in markdown and "calls 命题" not in markdown
    assert "P2" not in markdown and "P3" not in markdown
    assert "静态目标已检查 2/2" in markdown
    assert "动态验证：未执行目标仓库代码、测试或构建。" in markdown
    assert "建议先补齐上述待核实事项所需证据" in markdown
    audit = render_technical_audit(plan, execution_record(task.task_runtime_report))
    assert "calls 命题" in audit and "weighted_posterior" in audit
    assert '"start_line": 129' in audit and '"start_line": 296' in audit


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_pending_investigation_does_not_replace_interruption_or_recovery_advice(status):
    task = new_output(completed_task(admitted_investigations=[{
        "status": "insufficient_evidence", "answer": "insufficient_evidence",
        "proposition": "返回值兼容性是否成立", "reason": "缺少调用方源码"}]))
    task.status = status
    plan = PublicationPlanner().build(task)
    assert "部分审查记录" in plan.summary_markdown
    assert "重新审查" in plan.summary_markdown
    assert "未发现已确认代码问题" not in plan.summary_markdown
    assert "待核实事项（1 项）" in plan.summary_markdown


def test_pending_investigation_never_displaces_blocker_repair_action():
    task = publishable_task(severity="high", claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    task.task_runtime_report["admitted_investigations"] = [{
        "status": "not_executed", "proposition": "检查另一条返回路径"}]
    plan = PublicationPlanner().build(new_output(task))
    assert plan.outcome.confirmed_count == 1 and len(plan.presentation.findings) == 1
    assert "先修复上述高优先级问题，并在新快照重新核验" in plan.summary_markdown
    assert "待核实事项（1 项）" in plan.summary_markdown
    assert "未发现已确认代码问题" not in plan.summary_markdown


def test_old_presentation_roundtrip_does_not_add_summary_coverage_or_change_hash():
    from app.models.publication import ReviewPresentation

    saved = PublicationPlanner().build(new_output(completed_task())).presentation.model_dump(mode="json")
    assert "summary_input_coverage" not in saved
    assert stable_hash(saved) == stable_hash(ReviewPresentation.model_validate(saved).model_dump(mode="json"))


def test_frozen_v1_plan_keeps_test_supplement_body_and_hash_for_publication_recovery():
    from app.services.publication_renderer import render_review_comment, render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.presentation.schema_version = "review-presentation-v1"
    plan.presentation.change_summary = "调整区间边界。"
    tests = "新增区间包含性测试用例。"
    plan.presentation.test_changes = [tests]
    plan.summary_markdown = render_summary(plan)
    plan.review_comment_markdown = render_review_comment(plan)
    saved = plan.model_dump(mode="json")
    restored = PublicationPlan.model_validate(deepcopy(saved))
    assert restored.model_dump(mode="json") == saved
    assert stable_hash(restored.model_dump(mode="json")) == stable_hash(saved)
    expected = "调整区间边界。 测试源码变化（不表示测试已运行）：" + tests
    assert expected in render_summary(restored)
    assert render_summary(restored) == plan.summary_markdown
    assert render_review_comment(restored) == plan.review_comment_markdown
    assert expected in compose_summary(restored, {"finding_keys": []}, {}, [], "Prepared")


def test_test_only_fallback_does_not_use_model_prose_as_the_summary():
    from app.models.publication import ChangeUnderstanding
    from app.services.publication_renderer import render_review_comment, render_user_report
    from app.services.review_presentation import build_presentation

    task = new_output(completed_task())
    plan = PublicationPlanner().build(task)
    observation = "补充空响应处理的回归用例。"
    plan.digest.change_understanding = ChangeUnderstanding(
        actual_changes=observation, implementation_changes=None, test_observations=observation)
    plan.presentation = build_presentation(plan)
    markdown = render_review_comment(plan)
    overview = markdown.split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert plan.presentation.summary_source == "unavailable"
    assert observation not in overview
    assert "尚缺少可概括实现变化的代码观察" in overview
    assert plan.presentation.schema_version == "review-presentation-v2"
    assert plan.presentation.test_changes == []
    # 原始观察仍保留在详细报告中供审计。
    assert observation in render_user_report(task, plan)


def test_unknown_finding_status_is_not_promoted_to_no_confirmed_problem():
    task = new_output(completed_task(findings=[{"status": "future"}]))
    plan = PublicationPlanner().build(task)
    assert plan.outcome.finding_conclusion == "unknown"
    assert "未发现已确认代码问题" not in plan.summary_markdown
    assert "未知的问题状态" in plan.summary_markdown


@pytest.mark.parametrize("synthesis_status", ["ready", "fallback"])
def test_overview_does_not_fill_remaining_sentences_with_test_observations(synthesis_status):
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.presentation.synthesis_status = synthesis_status
    plan.presentation.change_summary = "调整边界计算。保留原始下界。扩展上界以包含点估计。"
    plan.presentation.test_changes = ["新增区间包含性断言。", "增加测试入口。但没有保存对应断言观察。"]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview.count("。") == 3
    assert "新增区间包含性断言。" not in overview
    assert "增加测试入口" not in overview
    assert "不表示测试已运行" not in overview


def test_four_sentence_summary_does_not_grow_with_supplementary_test_observations():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    summary = "调整区间边界。保留下界。扩展上界。增加点估计包含性断言。"
    plan.presentation.change_summary = summary
    plan.presentation.test_changes = ["另有一项测试源码观察。"]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview == summary


def test_long_test_observation_is_not_cut_before_its_limitation():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.presentation.change_summary = "调整区间边界。"
    plan.presentation.test_changes = [
        "增加测试入口。" + "测试夹具记录，" * 80 + "但未保存覆盖区间不收窄的断言。"]
    overview = render_summary(plan).split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
    assert overview == "调整区间边界。"
    assert "增加测试入口" not in overview


def test_default_clean_result_is_one_conclusion_without_delivery_accounting():
    from app.services.publication_renderer import render_review_comment, render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    for markdown in (render_summary(plan), render_review_comment(plan)):
        result = markdown.split("### 审查结果\n\n", 1)[1].split("### 验证与合并建议", 1)[0].strip()
        assert result == "✅ 结论：已检查范围内未发现已确认问题。"
        assert "具备发布资格" not in markdown
        assert "本轮已确认 0 项" not in markdown
        assert "未获取的 GitHub CI 状态" not in markdown
        assert "Mergeability" not in markdown
        assert "未执行目标仓库代码、测试或构建" in markdown


@pytest.mark.parametrize(("severity", "action"), [
    ("high", "先修复上述高优先级问题，并在新快照重新核验"),
    ("medium", "请评估上述非高优先级问题的业务影响"),
])
def test_default_findings_keep_full_list_and_action_without_duplicate_editorial_summary(severity, action):
    from app.services.publication_renderer import render_review_comment

    task = publishable_task(severity=severity, claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    plan = PublicationPlanner().build(new_output(task))
    plan.presentation.review_summary = "重复问题概述不应挤占默认结果。"
    before = plan.model_dump(mode="json")
    compact = render_review_comment(plan)
    full = render_review_comment(plan, detail="full")
    assert all(finding.body_markdown in compact for finding in plan.presentation.findings)
    assert action in compact
    assert plan.presentation.review_summary not in compact
    assert plan.presentation.review_summary in full
    assert "具备发布资格" in full
    assert "不会自动创建 GitHub" in full
    assert plan.model_dump(mode="json") == before


def test_short_output_keeps_combined_blocker_and_head_validation_failure():
    from app.models.review import TestRunResult as RunResult, ValidationSnapshot
    from app.services.publication_renderer import render_review_comment, render_summary

    task = publishable_task(severity="high", claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    task.validation_snapshots = [ValidationSnapshot(
        stage="head", sha=task.run_manifest.head_sha, passed=False, failure_kind="code_regression",
        command_results=[RunResult(tool="test_runner", command="pytest", exit_code=1, passed=False)])]
    plan = PublicationPlanner().build(new_output(task))
    for markdown in (render_summary(plan), render_review_comment(plan)):
        assert "发现 1 项高优先级问题，另有当前版本动态检查未通过" in markdown
        assert "与当前 Head 绑定的验证失败记录" in markdown
        assert "先修复上述高优先级问题，并在新快照重新核验" in markdown
        assert "已检查范围内未发现已确认问题" not in markdown


@pytest.mark.parametrize(("state", "expected"), [
    ("not_executed", "未执行目标仓库代码、测试或构建"),
    ("passed", "与当前 Head 绑定的受控验证通过记录"),
    ("failed", "与当前 Head 绑定的验证失败记录"),
    ("independent_results", "补丁通过不代表当前 PR Head 已通过测试"),
    ("unknown", "动态验证状态未知"),
])
def test_short_validation_section_preserves_all_validation_states(state, expected):
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task()))
    plan.digest.validation_state = state
    plan.presentation.validation_state = state
    markdown = render_summary(plan)
    validation = markdown.split("### 验证与合并建议", 1)[1]
    assert expected in validation
    assert "未获取的 GitHub CI 状态" not in validation


def test_unknown_scope_is_not_presented_as_zero_of_zero_completed():
    from app.services.publication_renderer import render_summary

    plan = PublicationPlanner().build(new_output(completed_task(initial_scope_coverage=[])))
    markdown = render_summary(plan)
    assert "检查范围未知" in markdown
    assert "已检查 0 / 0" not in markdown
    assert "已检查范围内未发现已确认问题" not in markdown


@pytest.mark.asyncio
async def test_existing_legacy_inline_is_reused_by_new_presentation(persistence):
    from datetime import timedelta
    from app.models.publication import PublicationSelection
    from test_publication_worker import worker_setup
    repo, attempt, fake, worker, delivery, _ = worker_setup(persistence)
    await worker.run_once()
    task = new_output(persistence[0].get_task(attempt.review_id).model_copy(deep=True))
    task.id = "new-presentation"
    task.run_manifest.review_id = task.id
    task.run_manifest.completed_at += timedelta(days=1)
    persistence[0].create_task(task)
    checked = await delivery.preflight.check(task)
    selection = PublicationSelection(expected_plan_hash=checked["plan_hash"],
        expected_head_sha=attempt.head_sha, finding_keys=checked["finding_keys"])
    current = repo.create(PublicationPlanner().build(task), selection, checked["context"], "new-presentation")
    await worker.run_once()
    assert repo.get(current.id).state == "succeeded"
    assert len(fake.comments) == len(fake.reviews) == len(fake.inline) == 1
    body = next(iter(fake.comments.values()))["body"]
    assert "本次 PR 主要变更" in body
    assert "[查看代码讨论](" in body
