"""当前风险、历史状态和评论选择之间的交付回归。"""

import pytest

from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import comment_selection, render_review_comment
from test_publication_planner import publishable_task


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
@pytest.mark.parametrize("detail", ["compact", "full"])
def test_historical_duplicates_remain_visible_at_every_severity(severity, detail):
    task = publishable_task(severity=severity, claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.95
    plan = PublicationPlanner().build(task)
    finding = plan.inline_drafts[0]
    assert finding.severity == severity
    finding.default_selected = False
    finding.default_selection_reason = "historical_duplicate"
    finding.discussion_urls = [task.pr_url + "#discussion_r1", "https://evil.test/thread"]
    before = plan.model_dump(mode="json")
    included, omitted, _ = comment_selection(plan)
    assert included == [finding] and omitted == []
    comment = render_review_comment(plan, detail=detail)
    for text in [finding.title, "src/a", finding.affected_behavior, finding.recommendation,
                 task.pr_url + "#discussion_r1", "当前快照仍确认此问题"]:
        assert text in comment
    assert "evil.test" not in comment
    assert "已处理的非阻断建议已按本次选择省略" not in comment
    if severity in {"critical", "high"}:
        assert "## 阻断问题" in comment
    assert plan.model_dump(mode="json") == before
    expanded = render_review_comment(plan, detail=detail, include_discussed=True)
    assert finding.evidence_summary in expanded
    assert finding.failure_scenario in expanded


@pytest.mark.parametrize("status", ["insufficient_evidence", "not_checked", "unexpected"])
def test_unverified_history_never_becomes_resolved(status):
    task = publishable_task()
    task.pr_discussion = {"rechecks": [{"discussion_id": "old", "status": status, "reason": "调用方尚未核验"}],
                          "links": {"old": task.pr_url + "#issuecomment-1", "unsafe": "https://evil.test"}}
    plan = PublicationPlanner().build(task)
    assert plan.digest.discussion_links == {"old": task.pr_url + "#issuecomment-1"}
    assert "待核实，不能判断已修复" in plan.review_comment_markdown
    assert task.pr_url + "#issuecomment-1" in plan.review_comment_markdown
    assert "调用方尚未核验" in plan.review_comment_markdown


@pytest.mark.parametrize("status", ["fixed", "not_present", "background", "still_present"])
def test_history_recheck_statuses_are_kept_separate(status):
    task = publishable_task()
    task.pr_discussion = {"rechecks": [{"discussion_id": "old", "status": status, "reason": "历史事项独立复核依据"}]}
    comment = PublicationPlanner().build(task).review_comment_markdown
    assert ("历史事项独立复核依据" in comment) is (status == "still_present")
    if status == "still_present":
        assert "复核仍存在" in comment


@pytest.mark.parametrize("same_issue", [False, True])
def test_related_history_is_not_hidden_by_a_different_current_finding(same_issue):
    task = publishable_task()
    task.pr_discussion = {"rechecks": [{"discussion_id": "old", "status": "insufficient_evidence",
                                      "reason": "历史调用路径尚未核验"}]}
    plan = PublicationPlanner().build(task)
    plan.inline_drafts[0].discussion_matches = [{"discussion_id": "old", "same_subject": same_issue,
        "same_trigger": True, "same_impact": True, "new_information": False}]
    comment = render_review_comment(plan)
    assert ("历史调用路径尚未核验" in comment) is (not same_issue)
    assert plan.inline_drafts[0].title in comment


def test_human_resolution_is_explicit_and_never_hides_blockers():
    for severity in ["medium", "high"]:
        task = publishable_task(severity=severity, claim="权限绕过导致未授权数据访问")
        task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.95
        plan = PublicationPlanner().build(task, dispositions={
            "key-a": {"current": {"status": "resolved", "reason": "作者声明已经处理"}}})
        finding = plan.inline_drafts[0]
        finding.default_selected = False
        finding.default_selection_reason = "historical_duplicate"
        included, omitted, retained = comment_selection(plan, exclude_resolved=True)
        assert (finding in included) is (severity == "high")
        assert (finding in omitted) is (severity == "medium")
        comment = render_review_comment(plan, exclude_resolved=True)
        if severity == "medium":
            assert "不代表机器重新核验通过" in comment
        else:
            assert retained == ["key-a"]
            assert finding.title in comment
