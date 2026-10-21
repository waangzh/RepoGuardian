"""历史复核的已核验风险与未完成调查必须在评论中分开呈现，且计数不重复。"""

from copy import deepcopy

import pytest

from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import render_review_comment
from test_publication_planner import publishable_task
from test_review_outcome import completed_task, discussion

URL = "https://github.com/o/r/pull/1#discussion_r1"


def _history_task(*, severity="high", evidence_ids=("e1",), status="still_present",
                  discussion_id="inline:1", behavior="匿名请求仍可读取受保护数据",
                  recommendation="在读取前校验身份"):
    task = completed_task()
    task.pr_discussion = discussion(rechecks=[{
        "discussion_id": discussion_id, "path": "src/a.py", "status": status,
        "reason": "固定源码仍保留原始触发路径", "evidence_ids": list(evidence_ids),
        "severity": severity, "affected_behavior": behavior, "recommendation": recommendation}])
    task.pr_discussion["links"] = {discussion_id: URL}
    return task


@pytest.mark.parametrize("detail", ["compact", "full"])
def test_verified_history_is_a_risk_section_not_a_pending_item(detail):
    plan = PublicationPlanner().build(_history_task())
    assert plan.digest.merge_decision.status == "fix_blockers"
    comment = render_review_comment(plan, detail=detail)
    headings = [line for line in comment.splitlines() if line.startswith("## ")]
    assert headings[0] == "## 已核验的既有风险"
    assert "## 仍需确认的事项" not in headings
    assert "不计入本轮 Diff 新增问题" in comment
    for text in ("匿名请求仍可读取受保护数据", "在读取前校验身份", "e1", URL):
        assert text in comment
    # 已核验风险不能再以“待确认”措辞出现。
    assert "待核实，不能判断已修复" not in comment


@pytest.mark.parametrize("status", ["insufficient_evidence", "not_checked"])
def test_unfinished_history_stays_in_pending_section(status):
    plan = PublicationPlanner().build(_history_task(status=status))
    comment = render_review_comment(plan, detail="compact")
    headings = [line for line in comment.splitlines() if line.startswith("## ")]
    assert "## 已核验的既有风险" not in headings
    assert "## 仍需确认的事项" in headings
    assert "待核实，不能判断已修复" in comment


def test_history_without_evidence_is_not_promoted_to_verified_risk():
    plan = PublicationPlanner().build(_history_task(evidence_ids=()))
    comment = render_review_comment(plan, detail="compact")
    assert "## 已核验的既有风险" not in comment
    assert "缺少可引用的代码证据" in comment


def _duplicate_task(*, same_issue=True):
    task = publishable_task(severity="high", claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    task.pr_discussion = discussion(rechecks=[{
        "discussion_id": "inline:1", "path": "src/a.py", "status": "still_present",
        "reason": "固定源码仍保留原始触发路径", "evidence_ids": ["e1"], "severity": "high",
        "affected_behavior": "匿名请求仍可读取受保护数据", "recommendation": "在读取前校验身份"}])
    task.pr_discussion["links"] = {"inline:1": URL}
    task.pr_discussion["manifest_hash"] = "frozen"
    record = task.task_runtime_report["findings"][0]
    record["verification"]["discussion_manifest_hash"] = task.pr_discussion["manifest_hash"]
    record["verification"]["discussion_matches"] = [{
        "discussion_id": "inline:1", "comment_id": "comment:1", "quote": "retry branch may return null",
        "same_subject": same_issue, "same_trigger": True, "same_impact": True,
        "new_information": False, "reason": "同一触发路径与影响"}]
    return task


def test_same_defect_is_counted_once_but_both_sources_remain_visible():
    plan = PublicationPlanner().build(_duplicate_task())
    decision = plan.digest.merge_decision
    assert decision.blocking_finding_keys == ["key-a"]
    assert decision.blocking_historical_keys == ["inline:1"]
    assert decision.blocking_independent_count == 1
    assert decision.deduplicated_historical_keys == ["inline:1"]
    # 作者看到的标题只计一项，并说明合并口径。
    assert decision.label == "发现 1 项高优先级问题（含 1 项历史复核，1 项本轮记录），建议修复后复审"
    assert "已按一项计入标题" in decision.advice
    for markdown in (plan.review_comment_markdown, plan.user_report_markdown, plan.summary_markdown):
        assert "发现 2 项高优先级问题" not in markdown
        assert "权限绕过导致未授权数据访问" in markdown
    # 技术审计仍分别给出两份证据来源。
    from app.services.publication_renderer import merge_advice
    audit = merge_advice(plan)
    assert "独立缺陷：高优先级问题 1 项" in audit
    assert "本轮记录高优先级 1 项" in audit and "历史复核高优先级 1 项" in audit


@pytest.mark.parametrize("match", [
    {"same_subject": False}, {"same_trigger": False}, {"same_impact": False}, {"new_information": True},
])
def test_loose_similarity_never_deduplicates(match):
    task = _duplicate_task()
    task.task_runtime_report["findings"][0]["verification"]["discussion_matches"][0].update(match)
    decision = PublicationPlanner().build(task).digest.merge_decision
    assert decision.blocking_independent_count == 2
    assert decision.deduplicated_historical_keys == []
    assert decision.label == "发现 2 项高优先级问题（含历史复核），建议修复后复审"


def test_unrelated_history_on_same_file_is_not_deduplicated():
    task = _duplicate_task()
    task.task_runtime_report["findings"][0]["verification"]["discussion_matches"] = []
    decision = PublicationPlanner().build(task).digest.merge_decision
    assert decision.blocking_independent_count == 2
    assert decision.blocking_historical_keys == ["inline:1"]


def test_deduplicated_history_is_not_rendered_twice():
    plan = PublicationPlanner().build(_duplicate_task())
    comment = plan.review_comment_markdown
    assert "## 需要处理的问题" in comment
    # 当前问题已按严格关联覆盖同一缺陷，历史摘要不再重复一条相同风险。
    assert "## 已核验的既有风险" not in comment
    assert "匿名请求仍可读取受保护数据" not in comment


@pytest.mark.parametrize("detail", ["compact", "full"])
def test_full_detail_without_pending_items_does_not_create_a_risk_section(detail):
    plan = PublicationPlanner().build(completed_task())
    comment = render_review_comment(plan, detail=detail)
    headings = [line for line in comment.splitlines() if line.startswith("## ")]
    assert "## 仍需确认的事项" not in headings
    if detail == "full":
        assert "## 审查范围与补充信息" in headings
    else:
        assert "## 审查范围与补充信息" not in headings
        assert "### 审查范围" not in comment


def test_scope_detail_never_hides_a_real_pending_item():
    plan = PublicationPlanner().build(completed_task(scope_complete=False))
    comment = render_review_comment(plan, detail="full")
    headings = [line for line in comment.splitlines() if line.startswith("## ")]
    assert "## 仍需确认的事项" in headings
    assert "## 审查范围与补充信息" not in headings


def test_historical_counts_are_stable_and_do_not_mutate_saved_facts():
    task = _duplicate_task()
    before = deepcopy(task.task_runtime_report)
    plan = PublicationPlanner().build(task)
    assert plan == PublicationPlanner().build(task)
    assert task.task_runtime_report == before
