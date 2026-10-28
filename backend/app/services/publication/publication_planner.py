"""Current saved findings to publication drafts; no model or remote operations."""

from app.models.publication import (
    ChangeUnderstanding, DigestExcludedFile, DigestGoal, PublicationAnchor, PublicationFinding, PublicationPlan,
    PublicationSnapshot, ReviewDigest, ReviewSummary, ReviewSummaryDigest, WithheldFinding,
)
from app.services.publication.publication_renderer import (
    render_summary, render_user_report, render_review_comment,
)
from app.services.publication.review_merge_decision import merge_decision
from app.services.publication.review_presentation import build_presentation
from app.services.publication.review_publication import validated_findings
from app.review.findings import finding_attribution, finding_severity
from app.review.evidence import EvidenceArchive
from app.review.observations import author_comment_text, validated_observations
from app.services.publication.review_text_format import markdown_text
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.review_findings import finding_views
from app.services.review.review_outcome import derive_review_outcome, snapshot_valid
from app.services.publication.review_prose import author_goal_text


def summary_projection(plan):
    return ReviewSummary(review_id=plan.review_id, snapshot=plan.snapshot, outcome=plan.outcome,
        digest=ReviewSummaryDigest.model_validate({key: value for key, value in plan.digest.model_dump().items()
            if key in ReviewSummaryDigest.model_fields}), review_comment_markdown=plan.review_comment_markdown,
        withheld_findings=plan.withheld_findings, content_eligible=plan.content_eligible,
        blocking_reasons=plan.blocking_reasons, presentation=plan.presentation)


class PublicationPlanner:
    def __init__(self, artifacts=None):
        self.artifacts = artifacts

    def build(self, task, *, dispositions=None, include_markdown=True):
        selected, withheld = validated_findings(task, self.artifacts)
        blockers = []
        if task.status in {"failed", "cancelled"}:
            blockers.append("task_" + task.status)
        if not snapshot_valid(task):
            blockers.append("snapshot_missing")
        if blockers:
            withheld.extend((finding.id, "review_content_blocked") for finding in selected)
            selected = []
        author_goal = author_goal_text(task.pr.body) or task.pr.title
        digest = ReviewDigest(pr_url=task.pr_url, author_goal=author_goal,
            author_context_status="truncated" if len(task.pr.body or "") > 4000 else "complete",
            author_context_limitation="模型仅收到前 4000 字符作者声明。" if len(task.pr.body or "") > 4000 else None,
            discussion_review_enabled=bool(task.configuration.options.include_pr_discussions),
            actual_changes=[f"{item.file_path}: +{item.additions} / -{item.deletions}" for item in task.changed_files],
            findings=finding_views(task, dispositions or {}, self.artifacts),
            execution_status=task.status)
        digest.change_understanding = ChangeUnderstanding(author_goal=author_goal,
            author_goal_source="description" if author_goal_text(task.pr.body) else "title",
            diff_summary=f"本次变更涉及 {len(task.changed_files)} 个文件，新增 "
                f"{sum(item.additions for item in task.changed_files)} 行、删除 "
                f"{sum(item.deletions for item in task.changed_files)} 行。")
        report = task.review_report
        if report:
            digest.termination_reason = report.termination
            file_states = {}
            for goal in report.goals:
                item = DigestGoal(goal_id=goal.id, path=goal.path, side=goal.side,
                    start_line=goal.start_line, end_line=goal.end_line, status=goal.status, reason=goal.reason)
                (digest.examined_goals if goal.status == "examined" else digest.incomplete_goals).append(item)
                file_states.setdefault(goal.path, []).append(goal.status)
            digest.reviewed_files = sorted(path for path, states in file_states.items()
                                           if all(state == "examined" for state in states))
            digest.checked_areas = sorted({goal.side + "_source_ranges" for goal in report.goals
                                           if goal.status == "examined"})
            reviewed_paths = {path for assignment in report.assignments for path in assignment.focus_paths}
            digest.excluded_files = [DigestExcludedFile(path=item.file_path, reason="未纳入主审范围")
                                    for item in task.changed_files if item.file_path not in reviewed_paths]
        if task.pr_discussion:
            digest.discussion_stats = {"collected": task.pr_discussion["thread_count"],
                "status": "complete" if all(value == "complete" for value in task.pr_discussion["sources"].values())
                    else "partial", "limitations": task.pr_discussion["limitations"]}
        evidence = EvidenceArchive(snapshot=report.snapshot, artifacts=self.artifacts or LocalArtifactStore()) if report else None
        outcome = derive_review_outcome(task, publishable_count=len(selected), evidence=evidence)
        plan = PublicationPlan(review_id=task.id, snapshot=PublicationSnapshot(
            head_sha=task.snapshot.head_sha, effective_base_sha=task.snapshot.effective_base_sha),
            outcome=outcome, digest=digest, content_eligible=not blockers,
            blocking_reasons=blockers, withheld_findings=[WithheldFinding(candidate_id=identity, reason_code=reason)
                                                       for identity, reason in sorted(set(withheld))])
        for finding in selected:
            candidate = finding.candidate
            members = [item for item in report.candidates
                       if item.id in {finding.id, *finding.related_candidate_ids}]
            text = {"claim": candidate.title, "affected_behavior": candidate.impact,
                    "failure_scenario": "\n".join(dict.fromkeys(
                        item.trigger + "；" + item.actual_behavior for item in members)),
                    "recommendation": candidate.recommendation}
            comment = finding.verification.author_comment
            if comment:
                text.update(affected_behavior=comment.impact,
                    failure_scenario=comment.trigger + "；" + comment.cause,
                    recommendation=comment.recommendation)
            body = author_comment_text(finding.verification)
            member_decisions = {item.candidate_id: item for item in report.decisions}
            related_bodies = [author_comment_text(member_decisions[identity])
                              for identity in finding.related_candidate_ids]
            body = "\n\n".join(dict.fromkeys([body, *related_bodies]))
            change = next(item for item in task.changed_files if item.file_path == candidate.affected_path)
            draft = PublicationFinding(finding_key=finding.id, candidate_id=finding.id,
                attribution=finding_attribution(finding),
                severity=finding_severity(finding), placement="inline" if finding.inline_line else "summary",
                anchor=PublicationAnchor(file_path=candidate.affected_path, start_line=candidate.line,
                    end_line=candidate.line, side="base" if change.change_type == "deleted" else "head"),
                title=candidate.title, category="correctness",
                evidence_summary=finding.verification.explanation, evidence_ids=finding.evidence_ids,
                affected_behavior=text["affected_behavior"], failure_scenario=text["failure_scenario"],
                recommendation=text["recommendation"],
                body_markdown=(f"**[{finding_severity(finding).title()}] "
                    f"{markdown_text(candidate.title, 200)}**\n\n" +
                    "\n\n".join(markdown_text(part, 3000) for part in body.split("\n\n"))))
            draft.body_markdown += "\n\n**变更归因：** " + {
                "introduced": "由本次 PR 引入", "exposed": "由本次 PR 暴露或扩大影响",
                "pre_existing": "既有问题，不计作本次 PR 新增", "uncertain": "尚未确定",
            }[finding_attribution(finding)]
            (plan.inline_drafts if finding.inline_line else plan.summary_findings).append(draft)
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for drafts in (plan.inline_drafts, plan.summary_findings):
            drafts.sort(key=lambda item: (rank[item.severity], item.anchor.file_path, item.finding_key))
        plan.digest.merge_decision = merge_decision(
            outcome, selected, content_eligible=plan.content_eligible,
            execution_complete=bool(report and report.termination == "completed"),
            coverage_limitations=report.coverage_limitations if report else [],
        )
        observations = validated_observations(report, evidence) if report and snapshot_valid(task) else []
        plan.presentation = build_presentation(plan, report=report, observations=observations)
        if include_markdown:
            plan.summary_markdown = render_summary(plan)
            plan.user_report_markdown = render_user_report(task, plan)
            plan.review_comment_markdown = render_review_comment(plan)
        return plan
