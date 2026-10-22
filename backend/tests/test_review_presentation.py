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
    assert "不会自动创建 GitHub" in plan.review_comment_markdown
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
