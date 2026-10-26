"""Human dispositions do not override machine evidence or publication eligibility."""

from app.models.review_plan import FindingLocation, FindingVerification, FindingView
from app.services.publication.review_publication import validated_findings


def finding_views(task, dispositions, artifacts=None):
    report = task.review_report
    if report is None:
        return []
    verified = {finding.id: finding for finding in validated_findings(task, artifacts)[0]}
    decisions = {item.candidate_id: item for item in report.decisions}
    items = []
    for candidate in report.candidates:
        decision = decisions.get(candidate.id)
        accepted = verified.get(candidate.id)
        state = ("confirmed" if accepted else "dismissed" if decision and decision.decision == "refuted"
                 else "unresolved")
        status = "verified" if accepted else "insufficient" if state == "unresolved" else "unknown"
        location = None
        if accepted:
            change = next(item for item in task.changed_files if item.file_path == candidate.affected_path)
            location = FindingLocation(path=candidate.affected_path, start_line=candidate.line,
                end_line=candidate.line, side="left" if change.change_type == "deleted" else "right")
        items.append(FindingView(id=candidate.id, status=state, review_status=state,
            attribution=candidate.attribution,
            title=candidate.title, message=candidate.trigger + "；" + candidate.actual_behavior,
            impact=candidate.impact, recommendation=candidate.recommendation, category="correctness",
            location=location, severity=candidate.severity, evidence_status=status,
            verification=FindingVerification(evidence_status=status),
            limitations=[decision.explanation] if decision else ["独立核验尚未完成"],
            source_ids=candidate.assignment_ids, disposition=dispositions.get(candidate.id)))
    return items
