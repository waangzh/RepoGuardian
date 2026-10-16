from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.models.review import ReviewUnit, ReviewIssue, EvidenceAnchor, ResolvedIssueLocation
from app.models.review_runtime import Candidate, VerificationResult
from app.services.publication_planner import PublicationPlanner
from app.services.review_outcome import derive_review_outcome
from app.services.review_publication import publication_view, verification_subject_hash
from test_review_outcome import completed_task


def publishable_task(*, placement="inline", side="head", claim="空返回值破坏调用方契约", candidate_id="candidate-a", severity="medium"):
    task = completed_task()
    task.review_units = [ReviewUnit(id="u1", primary_files=["src/a.py"], related_files=[], rule_ids=[],
        estimated_tokens=0, complexity="small", fingerprint="fixture", grouping_reason="fixture")]
    candidate = Candidate(candidate_id=candidate_id, severity=severity, claim=claim, primary_anchor="g1", supporting_evidence=["e1"],
        source_result_ids=["r1"], affected_behavior="调用方字段读取失败", failure_scenario="服务返回空值时直接读取字段", recommendation="先处理空值")
    spec = SimpleNamespace(snapshot_head_sha=task.run_manifest.head_sha, snapshot_base_sha=task.run_manifest.effective_base_sha)
    verification = VerificationResult(candidate_id=candidate.candidate_id, decision="supported", evidence_ids=["e1"],
        reason="独立核对主张", request_id="req1", attempt_id="attempt1", work_item_id="verify1",
        snapshot_head_sha=spec.snapshot_head_sha, subject_hash=verification_subject_hash(candidate, spec), publication_action="retain")
    issue = ReviewIssue(id=candidate.candidate_id, review_unit_id="u1", title=claim, category="correctness", severity=severity, confidence=0.5,
        affected_behavior=candidate.affected_behavior, failure_scenario=candidate.failure_scenario, recommendation=candidate.recommendation,
        placement=placement, primary_evidence=EvidenceAnchor(file_path="src/a.py", existing_code="return None",
        resolved_start_line=7, resolved_end_line=7, resolved_side=side, resolution_method="diff_exact", resolution_status="exact",
        match_count=1, anchor_hash="hash"), resolved_location=ResolvedIssueLocation(file_path="src/a.py", start_line=7, end_line=7, side=side))
    task.task_runtime_report["findings"] = [{"key": "key-a", "candidate": candidate.model_dump(mode="json"), "status": "confirmed",
        "verification": verification.model_dump(mode="json"), "publication": publication_view(candidate, verification, spec, usage_known=True),
        "issue": issue.model_dump(mode="json")}]
    return task


def test_verified_text_merge_base_and_deterministic_read_only_projection():
    task = publishable_task(side="base")
    task.issues = []  # 不依赖兼容性投影。
    before = task.model_dump(mode="json")
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "findings" and plan.content_eligible
    assert plan.outcome.publishable_count == 1 and len(plan.inline_drafts) == 1
    assert plan.inline_drafts[0].anchor.side == "base"
    assert plan.snapshot.effective_base_sha != task.run_manifest.base_sha
    assert plan == PublicationPlanner().build(task)
    assert task.model_dump(mode="json") == before
    assert derive_review_outcome(task) == plan.outcome


@pytest.mark.parametrize("problem,reason", [("contract", "publication_contract_missing"), ("decision", "verification_not_supported"),
    ("text", "publication_text_missing"), ("usage", "publication_usage_unknown"), ("sha", "snapshot_mismatch"),
    ("issue", "issue_policy_failed"), ("placement", "placement_not_publishable"), ("subject", "publication_subject_mismatch"),
    ("rewrite", "publication_subject_mismatch"), ("location", "issue_policy_failed"), ("policy", "issue_policy_failed")])
def test_withholds_without_losing_confirmed_count(problem, reason):
    task = publishable_task()
    record = task.task_runtime_report["findings"][0]
    if problem == "contract":
        record.pop("publication")
    elif problem == "decision":
        record["publication"]["decision"] = "refuted"
    elif problem == "text":
        record["publication"]["published_text"]["recommendation"] = ""
    elif problem == "usage":
        record["publication"]["usage_known"] = False
    elif problem == "sha":
        record["publication"]["base_sha"] = task.run_manifest.base_sha
    elif problem == "issue":
        record["issue"] = None
    elif problem == "placement":
        record["issue"]["placement"] = "suppressed"
    elif problem == "subject":
        record["verification"]["candidate_id"] = "other"
    elif problem == "rewrite":
        record["publication"]["published_text"]["claim"] = "未核验的新主张"
    elif problem == "location":
        record["issue"]["resolved_location"]["side"] = "base"
    else:
        record["issue"]["primary_evidence"]["match_count"] = 2
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "findings" and plan.outcome.confirmed_count == 1
    assert plan.outcome.publishable_count == 0 and not plan.inline_drafts and not plan.summary_findings
    assert plan.withheld_findings[0].reason_code == reason
    assert "1 项已确认记录未进入发布草稿" in plan.summary_markdown
    assert "未核验的新主张" not in plan.summary_markdown


def test_summary_routing_duplicates_conflicts_and_order():
    task = publishable_task(placement="summary")
    record = task.task_runtime_report["findings"][0]
    task.task_runtime_report["findings"].append(deepcopy(record))
    plan = PublicationPlanner().build(task)
    assert len(plan.summary_findings) == plan.outcome.publishable_count == 1
    assert not plan.inline_drafts and "**触发场景：**" in plan.summary_markdown
    task.task_runtime_report["findings"][1]["status"] = "unresolved"
    plan = PublicationPlanner().build(task)
    assert not plan.summary_findings
    assert all(item.reason_code == "duplicate_finding_conflict" for item in plan.withheld_findings)
    task.task_runtime_report["findings"].reverse()
    assert plan == PublicationPlanner().build(task)


def test_severity_sort_is_stable_across_inline_and_summary():
    task = publishable_task(claim="低等级问题", severity="low")
    summary_record = publishable_task(placement="summary", claim="中等级问题", candidate_id="candidate-b").task_runtime_report["findings"][0]
    summary_record["key"] = "key-b"
    task.task_runtime_report["findings"].append(summary_record)
    plan = PublicationPlanner().build(task)
    assert plan.outcome.publishable_count == 2
    assert plan.summary_markdown.index("中等级问题") < plan.summary_markdown.index("低等级问题")
    task.task_runtime_report["findings"].reverse()
    assert plan == PublicationPlanner().build(task)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_have_no_finding_drafts(status):
    task = publishable_task()
    task.status = status
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == status and plan.outcome.confirmed_count == 1
    assert not plan.content_eligible and not plan.inline_drafts and not plan.summary_findings
    assert plan.outcome.publishable_count == 0
