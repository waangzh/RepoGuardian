"""由保存的运行报告确定性地判定结论，不把缺失或未知投影为 Clean。"""

import re

from pydantic import ValidationError

from app.models.publication import CoverageKind, OutcomeReason as Reason, ReviewOutcome, ReviewOutcomeKind as Kind
from app.models.review import ReviewTask
from app.models.review_runtime import TaskRuntimeReport
from app.services.review_publication import select_publication_findings
from app.services.review_validation_digest import validation_digest


def snapshot_valid(task: ReviewTask) -> bool:
    manifest = task.run_manifest
    return bool(manifest and manifest.review_id == task.id and manifest.engine == "task_review_v1"
                and manifest.coverage_version == "task-goals-v1"
                and all(isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", sha)
                        for sha in (manifest.head_sha, manifest.effective_base_sha)))


def derive_review_outcome(task: ReviewTask, *, publishable_count: int | None = None) -> ReviewOutcome:
    reasons = []
    report = None
    if task.engine != "task_review_v1":
        reasons.append(Reason.UNSUPPORTED_LEGACY_PROJECTION)
    elif task.task_runtime_report is None:
        reasons.append(Reason.RUNTIME_REPORT_MISSING)
    else:
        try:
            report = TaskRuntimeReport.model_validate(task.task_runtime_report)
        except ValidationError:
            reasons.append(Reason.RUNTIME_REPORT_INVALID)
    cancelled = task.status == "cancelled" or (report and report.execution_status == "cancelled")
    failed = task.status == "failed" or (report and report.execution_status == "failed")
    kind = Kind.CANCELLED if cancelled else Kind.FAILED if failed else Kind.INCONCLUSIVE
    if cancelled:
        reasons.append(Reason.TASK_CANCELLED)
    elif failed:
        reasons.append(Reason.TASK_FAILED)
    if not snapshot_valid(task):
        reasons.append(Reason.SNAPSHOT_MISSING)
    if report is None:
        return ReviewOutcome(kind=kind, coverage=CoverageKind.UNKNOWN, reason_codes=reasons, dynamic_validation="unknown")

    confirmed = sum(str(item.get("status")) in {"confirmed", "published"} for item in report.findings)
    unresolved = sum(str(item.get("status")) in {"candidate", "unresolved", "needs_human"} for item in report.findings)
    unknown = any(str(item.get("status")) not in {"confirmed", "published", "candidate", "unresolved", "needs_human", "dismissed"}
                  for item in report.findings)
    total = len(report.initial_scope_coverage)
    examined = sum(item.get("status") == "examined" for item in report.initial_scope_coverage)
    investigations = sum(item.get("status") != "answered" or str(item.get("answer")) not in {"supported", "refuted"}
                         for item in report.admitted_investigations)
    if not total:
        reasons.append(Reason.SCOPE_EMPTY)
    if not report.scope_complete or examined != total:
        reasons.append(Reason.SCOPE_INCOMPLETE)
    if unresolved:
        reasons.append(Reason.VERIFICATION_UNRESOLVED)
    if unknown:
        reasons.append(Reason.FINDING_STATUS_UNKNOWN)
    if investigations:
        reasons.append(Reason.INVESTIGATION_INCOMPLETE)
    if any(item.get("status") != "duplicate" for item in report.unscheduled_leads):
        reasons.append(Reason.UNSCHEDULED_INVESTIGATION)
    manifest = task.run_manifest
    if report.usage_integrity != "complete" or report.unknown_attempts or (manifest and manifest.usage_integrity != "complete"):
        reasons.append(Reason.USAGE_INTEGRITY_UNKNOWN)
    if report.execution_status != "completed" or report.termination_reason != "plan_exhausted":
        reasons.append(Reason.TERMINATION_INCOMPLETE)
    if report.termination_reason == "budget_exhausted":
        reasons.append(Reason.BUDGET_EXHAUSTED)
    elif report.termination_reason == "deadline":
        reasons.append(Reason.DEADLINE_EXCEEDED)
    if not cancelled and not failed:
        kind = Kind.FINDINGS if confirmed else Kind.CLEAN if not reasons else Kind.INCONCLUSIVE
    publishable = 0
    if not cancelled and not failed and snapshot_valid(task):
        # Planner 使用同一个准入函数；原始确认数始终保留，不因扣留而减少。
        publishable = (publishable_count if publishable_count is not None
                       else len(select_publication_findings(task, report.findings)[0]))
    return ReviewOutcome(kind=kind, coverage=CoverageKind.PARTIAL if reasons else CoverageKind.COMPLETE,
                         confirmed_count=confirmed, publishable_count=publishable, unresolved_count=unresolved,
                         total_goals=total, examined_goals=examined, incomplete_investigations=investigations,
                         reason_codes=reasons,
                         dynamic_validation=validation_digest(task)[0],
                         finding_conclusion="confirmed" if confirmed else "unresolved" if unresolved else
                             "unknown" if unknown else "no_confirmed_findings",
                         review_completeness=CoverageKind.UNKNOWN if not total else CoverageKind.PARTIAL if
                             any(reason in reasons for reason in (Reason.SCOPE_INCOMPLETE, Reason.VERIFICATION_UNRESOLVED,
                                 Reason.FINDING_STATUS_UNKNOWN, Reason.INVESTIGATION_INCOMPLETE, Reason.UNSCHEDULED_INVESTIGATION))
                             else CoverageKind.COMPLETE,
                         runtime_reliability="unreliable" if any(reason in reasons for reason in (
                             Reason.TASK_FAILED, Reason.TASK_CANCELLED, Reason.TERMINATION_INCOMPLETE,
                             Reason.USAGE_INTEGRITY_UNKNOWN, Reason.SNAPSHOT_MISSING)) else "reliable")
