"""Derive conclusions from current graph results, preserving every unknown."""

from app.models.publication import CoverageKind, OutcomeReason as Reason, ReviewOutcome


def snapshot_valid(task):
    report = task.review_report
    return bool(report and report.snapshot.review_id == task.id
        and report.snapshot.head_sha == task.snapshot.head_sha
        and report.snapshot.base_sha == task.snapshot.effective_base_sha
        and report.snapshot.repository == f"{task.pr.owner}/{task.pr.repo}"
        and report.snapshot.pr_number == task.pr.number)


def derive_review_outcome(task, *, publishable_count=None):
    report = task.review_report
    reasons = []
    if task.status == "cancelled":
        reasons.append(Reason.TASK_CANCELLED)
    elif task.status == "failed":
        reasons.append(Reason.TASK_FAILED)
    if not snapshot_valid(task):
        reasons.append(Reason.SNAPSHOT_MISSING)
    if report is None:
        reasons.append(Reason.RUNTIME_REPORT_MISSING)
        return ReviewOutcome(kind=task.status if task.status in {"failed", "cancelled"} else "inconclusive",
                             coverage="unknown", reason_codes=reasons)
    total, examined = len(report.goals), sum(goal.status == "examined" for goal in report.goals)
    unresolved = sum(item.decision == "insufficient_evidence" for item in report.decisions)
    unresolved += sum(candidate.id not in {item.candidate_id for item in report.decisions}
                      for candidate in report.candidates)
    if not total:
        reasons.append(Reason.SCOPE_EMPTY)
    if not report.complete:
        reasons.append(Reason.SCOPE_INCOMPLETE)
    if unresolved:
        reasons.append(Reason.VERIFICATION_UNRESOLVED)
    results = [*report.discovery_results, *report.verification_results]
    if any(result.input_tokens is None or result.output_tokens is None for result in results):
        reasons.append(Reason.USAGE_INTEGRITY_UNKNOWN)
    if any(result.status != "completed" for result in results):
        reasons.append(Reason.TERMINATION_INCOMPLETE)
    elif report.termination not in {"completed", "partial"}:
        reasons.append(Reason.TERMINATION_INCOMPLETE)
    if any(result.read_errors or result.output.get("limitations") for result in results):
        if Reason.TERMINATION_INCOMPLETE not in reasons:
            reasons.append(Reason.TERMINATION_INCOMPLETE)
    if task.error == "review_deadline":
        reasons.append(Reason.DEADLINE_EXCEEDED)
    confirmed = len(report.findings)
    kind = (task.status if task.status in {"failed", "cancelled"} else "findings" if confirmed
            else "inconclusive" if reasons else "clean")
    coverage = CoverageKind.COMPLETE if report.complete else CoverageKind.PARTIAL if total else CoverageKind.UNKNOWN
    return ReviewOutcome(kind=kind, coverage=coverage, confirmed_count=confirmed,
        publishable_count=publishable_count or 0, unresolved_count=unresolved,
        total_goals=total, examined_goals=examined, reason_codes=reasons,
        finding_conclusion="confirmed" if confirmed else "unresolved" if unresolved else "no_confirmed_findings",
        review_completeness=coverage,
        runtime_reliability="unreliable" if any(result.status != "completed" for result in results)
            else "unknown" if Reason.USAGE_INTEGRITY_UNKNOWN in reasons else "reliable")
