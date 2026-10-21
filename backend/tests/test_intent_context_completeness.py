import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.api import reviews
from app.main import app
from app.graph.nodes.task_review_runtime import frozen_runtime_spec
from app.models.review_runtime import ReviewRunSpec, TaskRuntimeReport
from app.services.fingerprints import stable_hash
from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import render_technical_audit
from app.services.review_intent import author_targets
from test_review_outcome import completed_task


def spec_for(statement, contract="pr-intent-assessment-v2"):
    state = {"task_id": "run", "head_sha": "a" * 40, "effective_base_sha": "c" * 40,
        "base_sha": "b" * 40, "pr_info": {"title": "", "body": statement}}
    return frozen_runtime_spec(state, {"provider": "fixture", "model": "fixture", "policy_version": "p1",
        "pr_intent_contract": contract}, SimpleNamespace(max_tokens=100, max_calls=1))


@pytest.mark.parametrize("length,truncated", [(3999, False), (4000, False), (4001, True)])
def test_frozen_boundary_is_explicit_and_round_trips(length, truncated):
    spec = spec_for("文" * length)
    assert len(spec.pr_intent_context) == min(length, 4000)
    assert spec.pr_intent_context_truncated is truncated
    assert ReviewRunSpec.model_validate_json(spec.model_dump_json()) == spec
    if truncated:
        assert spec.pr_intent_context.endswith("…")


def test_complete_input_ending_in_ellipsis_is_not_guessed_to_be_truncated():
    spec = spec_for("文" * 3999 + "…")
    assert spec.pr_intent_context_truncated is False
    assert PublicationPlanner().build(assessed_task(spec)).digest.author_context_status == "complete"


@pytest.mark.parametrize("contract", ["legacy", "pr-intent-context-v1", "pr-intent-assessment-v1"])
def test_historical_spec_identity_and_request_inputs_do_not_gain_default_fields(contract):
    spec = spec_for("说明" * 2100, contract)
    saved = spec.model_dump(mode="json")
    assert "pr_intent_context_truncated" not in saved
    assert stable_hash(ReviewRunSpec.model_validate(saved).model_dump(mode="json")) == stable_hash(saved)
    assert spec_for("说明" * 2100, contract).model_dump(mode="json") == saved
    assert spec.pr_intent_context_truncated is None


def assessed_task(spec, alignment="not_applicable"):
    observations = [{"author_quote": target, "actual_behavior": "已核对当前局部文档范围", "alignment": alignment,
        "evidence_ids": ["e1"] if alignment == "supported" else [], "limitations": []}
        for target in author_targets(spec.pr_intent_context)]
    return completed_task(pr_intent_context=spec.pr_intent_context,
        **({"pr_intent_context_truncated": spec.pr_intent_context_truncated} if spec.pr_intent_context_truncated is not None else {}),
        initial_scope_coverage=[{"goal_id": "g1", "target_ref": "docs/a.md", "target_kind": "source_range",
            "status": "examined", "reason": "固定源码检查", "result_id": "accepted-result",
            "snapshot_head_sha": spec.snapshot_head_sha, "snapshot_base_sha": spec.snapshot_base_sha,
            "evidence_ids": ["e1"], "intent_assessments": observations}])


@pytest.mark.parametrize("alignment", ["not_applicable", "supported"])
def test_omitted_tail_is_disclosed_without_becoming_a_merge_requirement(alignment):
    marker = "必须拒绝已过期的登录令牌"
    spec = spec_for("说明" * 2100 + "\n" + marker)
    task = assessed_task(spec, alignment)
    original = deepcopy(task.task_runtime_report)
    plan = PublicationPlanner().build(task, dispositions={"g1": {"current": {"status": "resolved", "reason": "人工处理"}}})
    assert marker not in spec.pr_intent_context
    assert marker not in [item.author_target for item in plan.digest.intent_assessments]
    assert plan.outcome.review_completeness == "complete"
    assert plan.digest.author_context_status == "truncated"
    assert plan.digest.merge_decision.status == "conditional_merge"
    assert "author_context_truncated" not in plan.digest.merge_decision.unresolved_reasons
    for markdown in (plan.review_comment_markdown, plan.user_report_markdown, plan.summary_markdown, render_technical_audit(plan)):
        assert "送审内容已截断" in markdown and "未输入的部分尚未核验" in markdown
    assert task.task_runtime_report == original
    assert TaskRuntimeReport.model_validate_json(json.dumps(original)).pr_intent_context_truncated is True


@pytest.mark.parametrize("flag,expected", [(False, "complete"), (None, "unknown")])
def test_missing_historical_metadata_is_not_complete(flag, expected):
    spec = spec_for("更新文档。")
    spec = spec.model_copy(update={"pr_intent_context_truncated": flag})
    plan = PublicationPlanner().build(assessed_task(spec))
    assert plan.digest.author_context_status == expected
    assert plan.digest.merge_decision.status == "conditional_merge"
    if flag is None:
        assert "author_context_unknown" not in plan.digest.merge_decision.unresolved_reasons
        assert "历史审查未记录" in plan.review_comment_markdown


def test_no_statement_and_invalid_metadata_do_not_become_complete():
    spec = spec_for("")
    assert spec.pr_intent_context is None and spec.pr_intent_context_truncated is None
    assert PublicationPlanner().build(completed_task()).digest.author_context_status == "not_recorded"
    for value in ("false", 0):
        with pytest.raises(ValidationError):
            ReviewRunSpec.model_validate({**spec.model_dump(mode="json"), "pr_intent_context_truncated": value})


async def test_copy_check_preserves_input_gap_and_rechecks_head(monkeypatch):
    task = assessed_task(spec_for("说明" * 2100 + "\n尾部目标"))
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(get_task=lambda _: task))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        return SimpleNamespace(head=SimpleNamespace(sha=task.run_manifest.head_sha))
    monkeypatch.setattr(reviews.GitHubTool, "fetch_pr", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/reviews/run/review-comment-check")
    assert response.status_code == 200
    assert response.json()["status"] == "current" and calls == [task.pr_url]
    assert "未输入的部分尚未核验" in response.json()["review_comment_markdown"]
