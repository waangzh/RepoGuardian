"""保存快照的确定性发布投影，无远端读写或模型请求。"""

from pydantic import ValidationError

from app.models.publication import (DigestExcludedFile, DigestGoal, ReviewDigest, ReviewSummary, ReviewSummaryDigest,
    PublicationAnchor, PublicationFinding, PublicationPlan, PublicationSnapshot, WithheldFinding)
from app.models.review import ReviewTask
from app.models.review_runtime import TaskRuntimeReport
from app.services.publication_renderer import finding_body, render_summary, render_user_report, render_review_comment
from app.services.review_digest import enrich_review_digest
from app.services.review_outcome import derive_review_outcome, snapshot_valid
from app.services.review_publication import select_publication_findings
from app.services.review_merge_decision import merge_decision


def build_review_digest(task, report):
    digest = ReviewDigest(excluded_files=[DigestExcludedFile(path=item.file_path, reason=item.reason)
        for item in sorted(task.excluded_files, key=lambda item: (item.file_path, item.reason))])
    if task.pr_discussion:
        digest.discussion_stats = task.pr_discussion.get("stats")
        digest.discussion_rechecks = task.pr_discussion.get("rechecks") or []
    if report is None:
        return digest
    digest.validation_state = "not_executed"
    digest.termination_reason = report.termination_reason
    areas = set()
    file_goals = {}
    for item in report.initial_scope_coverage:
        kind = item.get("target_kind")
        goal = DigestGoal(goal_id=item.get("goal_id"), path=item.get("target_ref"),
            start_line=item.get("start_line"), end_line=item.get("end_line"),
            side="base" if kind == "base_source_range" else "head" if kind == "source_range" else None,
            status=item.get("status") or "unknown", reason=item.get("reason"))
        (digest.examined_goals if goal.status == "examined" else digest.incomplete_goals).append(goal)
        if goal.path:
            file_goals.setdefault(goal.path, []).append(goal.status)
        if goal.status == "examined" and kind in {"source_range", "base_source_range", "file_change"}:
            areas.add({"source_range": "head_source_ranges", "base_source_range": "base_source_ranges",
                       "file_change": "file_changes"}[kind])
    for goals in (digest.examined_goals, digest.incomplete_goals):
        goals.sort(key=lambda item: (item.path or "", item.side or "", item.start_line or 0, item.goal_id or ""))
    digest.reviewed_files = sorted(path for path, statuses in file_goals.items() if all(s == "examined" for s in statuses))
    digest.checked_areas = sorted(areas)
    return digest


def summary_projection(plan: PublicationPlan) -> ReviewSummary:
    """结果概览的轻量投影；技术审计与逐条正文仍只存在于完整计划。"""
    digest = plan.digest
    return ReviewSummary(review_id=plan.review_id, snapshot=plan.snapshot, outcome=plan.outcome,
        digest=ReviewSummaryDigest(author_goal=digest.author_goal,
            discussion_stats=digest.discussion_stats,
            author_context_status=digest.author_context_status,
            author_context_limitation=digest.author_context_limitation,
            change_understanding=digest.change_understanding, actual_changes=digest.actual_changes,
            intent_alignment=digest.intent_alignment, merge_decision=digest.merge_decision,
            excluded_files=digest.excluded_files, validation_state=digest.validation_state,
            validations=digest.validations, checked_areas=digest.checked_areas,
            termination_reason=digest.termination_reason),
        review_comment_markdown=plan.review_comment_markdown, withheld_findings=plan.withheld_findings,
        content_eligible=plan.content_eligible, blocking_reasons=plan.blocking_reasons)


class PublicationPlanner:
    def build(self, task: ReviewTask, *, dispositions: dict | None = None) -> PublicationPlan:
        records = []
        report = None
        if task.engine == "task_review_v1" and task.task_runtime_report is not None:
            try:
                report = TaskRuntimeReport.model_validate(task.task_runtime_report)
                records = report.findings
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
            digest=enrich_review_digest(build_review_digest(task, report), task, report, dispositions),
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
            evidence_summary = record["publication"]["basis"]
            category = record["candidate"].get("category", "correctness")
            finding = PublicationFinding(finding_key=key, candidate_id=record["candidate"]["candidate_id"],
                severity=issue.severity.value, placement=issue.placement.value, anchor=anchor,
                title=text["claim"], raw_title=text["claim"],
                category=category, evidence_summary=evidence_summary,
                evidence_ids=record["publication"]["evidence_ids"],
                affected_behavior=text["affected_behavior"], failure_scenario=text["failure_scenario"],
                recommendation=text["recommendation"],
                body_markdown=finding_body(text, issue.severity.value,
                    category=category, evidence_summary=evidence_summary))
            if task.pr_discussion and (record.get("verification") or {}).get("discussion_manifest_hash") == task.pr_discussion.get("manifest_hash"):
                from app.services.pr_discussion_matching import finding_discussions
                matches, duplicate = finding_discussions(record)
                finding.discussion_matches = matches
                finding.discussion_urls = sorted({url for match in matches
                    if (url := (task.pr_discussion.get("links") or {}).get(match["discussion_id"]))})
                finding.default_selected = not duplicate
                finding.default_selection_reason = "historical_duplicate" if duplicate else None
            (plan.inline_drafts if finding.placement == "inline" else plan.summary_findings).append(finding)
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        def sort_key(finding):
            return (rank[finding.severity], finding.anchor.file_path, finding.anchor.start_line or 0, finding.finding_key)
        plan.inline_drafts.sort(key=sort_key)
        plan.summary_findings.sort(key=sort_key)
        plan.digest.merge_decision = merge_decision(plan)
        plan.summary_markdown = render_summary(plan)
        plan.user_report_markdown = render_user_report(task, plan)
        plan.review_comment_markdown = render_review_comment(plan)
        return plan
