"""Normalized Run -> immutable Publisher plan; no model or remote operations."""

from app.core.fingerprints import stable_hash
from app.review.evidence import EvidenceArchive
from app.models.publication import (
    PublicationAnchor, PublicationFinding, PublicationPlan, PublicationSnapshot,
    ReviewDigest, ReviewOutcome, ReviewSummary, WithheldFinding,
)
from app.services.publication.publication_renderer import (
    markdown_text, render_review_comment, render_summary, render_user_report,
)
from app.services.publication.review_publication import validated_findings
from app.services.review.artifact_store import LocalArtifactStore


def summary_projection(plan):
    return ReviewSummary(review_id=plan.review_id, report_version=plan.report_version,
        snapshot=plan.snapshot, outcome=plan.outcome,
        review_comment_markdown=plan.review_comment_markdown,
        withheld_findings=plan.withheld_findings, content_eligible=plan.content_eligible,
        blocking_reasons=plan.blocking_reasons)


def _anchor(run, finding):
    location = finding.location
    if location is None:
        return PublicationAnchor(file_path="", side=None)
    changed = next((item for item in run.changed_files if item.file_path == location.path
                    or (location.side == "base" and item.old_file_path == location.path)), None)
    return PublicationAnchor(file_path=location.path, start_line=location.line,
        end_line=location.line, side=location.side,
        hunk_id=next((hunk.hunk_id for hunk in (changed.hunks if changed else [])
                      if any(line.new_line_no == location.line if location.side == "head"
                             else line.old_line_no == location.line for line in hunk.lines)), None))


class PublicationPlanner:
    def __init__(self, artifacts=None):
        self.artifacts = artifacts or LocalArtifactStore()

    def build(self, run, *, dispositions=None, include_markdown=True):
        report = run.report
        report_version = report.version if report else stable_hash({"missing": run.id})
        selected, withheld = validated_findings(run, self.artifacts)
        blockers = []
        if run.status in {"failed", "cancelled"}:
            blockers.append("task_" + run.status)
        if report is None:
            blockers.append("runtime_report_missing")
        if report and report.status in {"failed", "cancelled"}:
            blockers.append("report_" + report.status)
        if blockers:
            withheld.extend((finding.candidate_id, "review_content_blocked") for finding in selected)
            selected = []
        unresolved = sum(candidate.verification is None or candidate.verification.verdict == "insufficient"
                         for candidate in (report.candidates if report else []))
        outcome = ReviewOutcome(
            kind=("failed" if run.status == "failed" else "cancelled" if run.status == "cancelled"
                  else "findings" if selected else "inconclusive" if unresolved
                    or run.status != "completed" or (report and "scope_empty" in report.reasons) else "clean"),
            coverage="complete" if run.status == "completed" else "partial",
            confirmed_count=len([candidate for candidate in (report.candidates if report else [])
                                 if candidate.verification and candidate.verification.verdict == "confirmed"]),
            publishable_count=len(selected), unresolved_count=unresolved,
            reason_codes=[*blockers, *(report.reasons if report else [])],
        )
        plan = PublicationPlan(review_id=run.id, report_version=report_version,
            snapshot=PublicationSnapshot(declared_base_sha=run.snapshot.declared_base_sha,
                effective_base_sha=run.snapshot.base_sha, head_sha=run.snapshot.head_sha,
                repository_id=run.snapshot.repository_id), outcome=outcome,
            digest=ReviewDigest(pr_url=run.pr_url, execution_status=run.status,
                actual_changes=[f"{item.file_path}: +{item.additions} / -{item.deletions}" for item in run.changed_files]),
            content_eligible=not blockers, blocking_reasons=blockers,
            overview_status=report.overview.status if report else "pending",
            scope_reasons=report.reasons if report else [],
            withheld_findings=[WithheldFinding(candidate_id=identity, reason_code=reason)
                               for identity, reason in sorted(set(withheld))])
        for finding in selected:
            anchor = _anchor(run, finding)
            draft = PublicationFinding(finding_key=finding.id, candidate_id=finding.candidate_id,
                attribution=finding.attribution, severity=finding.severity,
                placement="inline" if anchor.side and anchor.start_line else "summary",
                anchor=anchor, title=finding.claim, affected_behavior=finding.impact,
                failure_scenario=finding.trigger, recommendation=finding.author_comment,
                evidence_ids=[source.id for source in finding.sources],
                body_markdown=(f"**[{finding.severity.title()}] {markdown_text(finding.claim, 300)}**\n\n"
                    f"{markdown_text(finding.author_comment, 3000)}"))
            (plan.inline_drafts if draft.placement == "inline" else plan.summary_findings).append(draft)
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for drafts in (plan.inline_drafts, plan.summary_findings):
            drafts.sort(key=lambda item: (rank[item.severity], item.anchor.file_path, item.finding_key))
        if report and report.overview.status == "ready" and report.overview.output:
            try:
                archive = EvidenceArchive(snapshot=run.snapshot, artifacts=self.artifacts)
                for source in report.overview.sources:
                    if archive.read_source(source.id)[0] != source:
                        raise ValueError("overview_source_mismatch")
                allowed = {f"S{index + 1}" for index in range(len(report.overview.sources))}
                if any(handle not in allowed for paragraph in report.overview.output.change_overview
                       for handle in paragraph.source_refs):
                    raise ValueError("overview_source_unknown")
                plan.overview_markdown = "\n\n".join(markdown_text(paragraph.text, 1000)
                    for paragraph in report.overview.output.change_overview)
            except (ValueError, OSError):
                plan.overview_status = "failed"
        if include_markdown:
            plan.summary_markdown = render_summary(plan)
            plan.user_report_markdown = render_user_report(run, plan)
            plan.review_comment_markdown = render_review_comment(plan)
        return plan
