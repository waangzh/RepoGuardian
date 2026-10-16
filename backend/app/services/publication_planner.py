"""保存快照的确定性发布投影，无远端读写或模型请求。"""

from pydantic import ValidationError

from app.models.publication import PublicationAnchor, PublicationFinding, PublicationPlan, PublicationSnapshot, WithheldFinding
from app.models.review import ReviewTask
from app.models.review_runtime import TaskRuntimeReport
from app.services.publication_renderer import finding_body, markdown_text, render_summary, render_user_report
from app.services.review_outcome import derive_review_outcome, snapshot_valid
from app.services.review_publication import select_publication_findings


class PublicationPlanner:
    def build(self, task: ReviewTask) -> PublicationPlan:
        records = []
        if task.engine == "task_review_v1" and task.task_runtime_report is not None:
            try:
                records = TaskRuntimeReport.model_validate(task.task_runtime_report).findings
            except ValidationError:
                pass  # Outcome 显式报告 runtime_report_invalid。
        selected, withheld = select_publication_findings(task, records)
        outcome = derive_review_outcome(task, publishable_count=len(selected))
        blockers = [reason.value for reason in outcome.reason_codes if reason in {
            "task_failed", "task_cancelled", "snapshot_missing", "runtime_report_missing", "runtime_report_invalid",
            "unsupported_legacy_projection"}]
        if blockers:
            withheld.extend((record["candidate"]["candidate_id"], "review_content_blocked") for _, record, _ in selected)
            selected = []
            outcome.publishable_count = 0
        manifest = task.run_manifest
        plan = PublicationPlan(review_id=task.id, outcome=outcome,
            snapshot=PublicationSnapshot(head_sha=manifest.head_sha if manifest else None,
                effective_base_sha=manifest.effective_base_sha if manifest else None),
            blocking_reasons=blockers, content_eligible=not blockers and snapshot_valid(task),
            withheld_findings=[WithheldFinding(candidate_id=identity, reason_code=reason)
                               for identity, reason in sorted(set(withheld))])
        for key, record, issue in selected:
            location = issue.resolved_location
            evidence = issue.primary_evidence
            anchor = PublicationAnchor(**location.model_dump()) if location else PublicationAnchor(
                file_path=evidence.file_path, start_line=evidence.resolved_start_line,
                end_line=evidence.resolved_end_line, side=evidence.resolved_side)
            text = record["publication"]["published_text"]
            finding = PublicationFinding(finding_key=key, candidate_id=record["candidate"]["candidate_id"],
                severity=issue.severity.value, placement=issue.placement.value, anchor=anchor,
                title=markdown_text(text["claim"], 200),
                body_markdown=finding_body(text, issue.severity.value, manifest.head_sha))
            (plan.inline_drafts if finding.placement == "inline" else plan.summary_findings).append(finding)
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        def sort_key(finding):
            return (rank[finding.severity], finding.anchor.file_path, finding.anchor.start_line or 0, finding.finding_key)
        plan.inline_drafts.sort(key=sort_key)
        plan.summary_findings.sort(key=sort_key)
        plan.summary_markdown = render_summary(plan)
        plan.user_report_markdown = render_user_report(task, plan)
        return plan
