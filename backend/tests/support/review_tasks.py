"""已完成审查任务与可发布任务的确定性工厂。"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.review import (
    EvidenceAnchor, ResolvedIssueLocation, ReviewIssue, ReviewRunManifest, ReviewTask, ReviewUnit,
)
from app.models.review_runtime import Candidate, VerificationResult
from app.services.publication.review_publication import publication_view, verification_subject_hash


def completed_task(**report_updates):
    task = ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1", status="completed")
    task.run_manifest = ReviewRunManifest(review_id=task.id, engine=task.engine, coverage_version="task-goals-v1",
        head_sha="a" * 40, base_sha="b" * 40, effective_base_sha="c" * 40, usage_integrity="complete",
        provider="fixture", model="fixture", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), duration_ms=0, coverage=task.coverage)
    task.task_runtime_report = {"execution_status": "completed", "termination_reason": "plan_exhausted",
        "scope_complete": True, "initial_scope_coverage": [{"status": "examined"}], "usage_integrity": "complete",
        **report_updates}
    return task


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


def discussion(stats=None, rechecks=None):
    return {"stats": {"status": "complete", "collected": 2, "comments": 3, "referenced": 2,
                      "referenced_comments": 2, "checked": 1, "unchecked": 1, "limitations": [],
                      "termination_reason": "plan_exhausted", **(stats or {})},
            "rechecks": rechecks or []}
