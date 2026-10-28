"""显式的报告建议策略；不授予 Approve 或合并权限。"""

from collections.abc import Sequence

from app.models.publication import MergeDecision, OutcomeReason, ReviewOutcome
from app.review.models import VerifiedFinding
from app.review.findings import finding_severity
from app.services.publication.review_text_format import MERGE_LABELS, REASON_LABELS
# 合并建议是审查策略输出，不是 GitHub Mergeability 或分支保护结论。
POLICY_BOUNDARY = "这是 RepoGuardian 按审查策略给出的建议，不代表 GitHub 的 Mergeability 状态或分支保护会阻止合并。"


def merge_decision(
    outcome: ReviewOutcome,
    findings: list[VerifiedFinding],
    *,
    content_eligible: bool,
    execution_complete: bool,
    coverage_limitations: Sequence[str] = (),
) -> MergeDecision:
    blocking = sorted({item.id for item in findings if finding_severity(item) in {"critical", "high"}})
    suggestions = sorted({item.id for item in findings if finding_severity(item) in {"medium", "low"}})
    reasons = {reason.value for reason in outcome.reason_codes
               if reason != OutcomeReason.USAGE_INTEGRITY_UNKNOWN}
    scope_complete = (outcome.coverage == "complete" and outcome.review_completeness == "complete"
                      and outcome.total_goals > 0 and outcome.examined_goals == outcome.total_goals)
    if not scope_complete:
        reasons.add("scope_incomplete" if outcome.total_goals else "scope_empty")
    if outcome.unresolved_count:
        reasons.add("verification_unresolved")
    if not execution_complete or outcome.runtime_reliability == "unreliable":
        reasons.add("termination_incomplete")
    if outcome.confirmed_count > outcome.publishable_count:
        reasons.add("confirmed_findings_withheld")
    if coverage_limitations:
        reasons.add("review_plan_limited")
    limits = {**REASON_LABELS, "confirmed_findings_withheld": "仍有已确认但未通过发布准入的记录"}
    decision = MergeDecision(
        blocking_finding_keys=blocking, nonblocking_finding_keys=suggestions,
        unresolved_reasons=sorted(reasons),
        limits=list(dict.fromkeys([limits[reason] for reason in sorted(reasons)] + list(coverage_limitations))),
    )
    if outcome.kind in {"failed", "cancelled"} or not content_eligible:
        decision.label = {"failed": "审查中断，尚无完整结论",
                          "cancelled": "审查已停止，已保留部分结果"}.get(
                              outcome.kind, MERGE_LABELS["unavailable"])
        if outcome.kind == "failed":
            decision.label = "审查执行失败，尚无完整结论"
    elif blocking:
        decision.status = "fix_blockers"
        decision.label = f"发现 {len(blocking)} 项高优先级问题，建议修复后复审"
        decision.advice = ("请优先修复已确认的高优先级问题，并在新快照重新核验；"
                           "人工标记已处理不能代替源码核验。" + POLICY_BOUNDARY)
    elif reasons:
        decision.status = "resolve_unknowns"
        if not scope_complete:
            remaining = outcome.total_goals - outcome.examined_goals
            decision.label = (f"还有 {remaining} 项约定检查未完成" if remaining > 0
                              else "检查范围尚不完整或未知")
        elif outcome.unresolved_count:
            decision.label = f"有 {outcome.unresolved_count} 项疑点需要进一步确认"
        elif outcome.confirmed_count > outcome.publishable_count:
            decision.label = "部分已确认记录未通过发布准入，需核对证据"
        elif outcome.incomplete_investigations:
            decision.label = f"仍有 {outcome.incomplete_investigations} 项必要证据缺口，暂不能给出完整结论"
        elif outcome.pending_limitation_count:
            decision.label = f"仍有 {outcome.pending_limitation_count} 条限制声明待复核，暂不能给出完整结论"
        else:
            decision.label = "审查执行仍有限制，暂不能给出完整结论"
        decision.advice = "请核对限制说明，补齐未完成范围、候选核验或执行记录后，再结合 CI 和人工评审决定是否合并。"
    else:
        decision.status = "conditional_merge"
        decision.label = (f"发现 {len(suggestions)} 项改进建议，暂无高优先级问题" if suggestions
                          else "已检查范围内未发现已确认问题")
        decision.advice = ("在已检查范围内，本次静态审查没有发现按审查策略建议合并前处理的问题；"
                           "请结合 CI、审批和人工判断决定是否合并。"
                           + (f"另有 {len(suggestions)} 项已确认的改进建议，请评估影响后处理。" if suggestions else "")
                           + POLICY_BOUNDARY)
    return decision
