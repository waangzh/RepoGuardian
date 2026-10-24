"""历史 PR 讨论复核的独立结论；不并入也不降级当前 Diff 主审查。"""

from app.models.pr_discussion import DiscussionUsage
from app.models.publication import HistoricalFindingView
from app.services.discussions.pr_discussion_matching import safe_discussion_url

# 历史上已由独立代码核验确认仍存在的问题才会影响整体风险判断。
_VERIFIED = {"still_present"}


def historical_discussion_outcome(task) -> dict | None:
    """由保存的讨论元数据确定性投影；缺失记录保持未知，不推断为无讨论。"""
    value = getattr(task, "pr_discussion", None)
    if not isinstance(value, dict) or not value.get("stats"):
        return None
    stats = value["stats"] or {}
    checks = [item for item in value.get("rechecks") or [] if isinstance(item, dict)]
    collected = max(0, int(stats.get("collected") or 0))
    checked = max(0, int(stats.get("checked") or 0))
    # 旧记录可能因快照外复核项出现负数；结论层只保留可解释的计数。
    unchecked = max(0, int(stats.get("unchecked") or 0))
    termination = stats.get("termination_reason")
    source_status = stats.get("status") or "not_recorded"
    if source_status in {"not_recorded", "unavailable"}:
        status = source_status
    elif termination in {"main_review_not_completed", "journal_unavailable_after_deadline"}:
        # 补充阶段没有执行或日志不可读：不能宣称复核已完成。
        status = "unavailable"
    elif termination in {"deadline", "cancelled"}:
        status = "timed_out"
    elif checked >= collected and source_status == "complete":
        status = "completed"
    else:
        status = "partial"
    verified = [{"discussion_id": item.get("discussion_id"),
                 "path": item.get("path"),
                 "reason": item.get("reason"),
                 "evidence_ids": list(item.get("evidence_ids") or [])}
                for item in checks
                if item.get("status") in _VERIFIED and item.get("evidence_ids")]
    findings = []
    for item in checks:
        if item.get("status") not in _VERIFIED or not item.get("evidence_ids") or not item.get("discussion_id"):
            continue
        # 旧记录只有“仍存在”并不等于已评估影响；不从原讨论、路径或标签猜严重度。
        severity = item.get("severity")
        behavior = str(item.get("affected_behavior") or "").strip() or None
        recommendation = str(item.get("recommendation") or "").strip() or None
        if severity not in {"critical", "high", "medium", "low"} or not behavior or not recommendation:
            severity = "unknown"
        findings.append(HistoricalFindingView(
            discussion_id=item["discussion_id"], path=item.get("path"), severity=severity,
            priority="fix_before_merge" if severity in {"critical", "high"} else
                "assess_improvement" if severity in {"medium", "low"} else "assess_impact",
            affected_behavior=behavior, recommendation=recommendation,
            reason=item.get("reason") or "未记录原因", evidence_ids=item["evidence_ids"],
            discussion_url=safe_discussion_url((value.get("links") or {}).get(item["discussion_id"]), task.pr_url)
        ).model_dump(mode="json"))
    usage = stats.get("usage") or {}
    try:
        usage_value = DiscussionUsage.model_validate(usage)
    except ValueError:
        usage_value = DiscussionUsage()
    return {
        "status": status,
        "collected": collected,
        "checked": checked,
        "unchecked": unchecked,
        "verified_defects": sorted(verified, key=lambda item: str(item.get("discussion_id"))),
        "termination_reason": termination,
        "limitations": [str(item) for item in stats.get("limitations") or []],
        "usage": usage_value.model_dump(mode="json"),
        # 只有独立代码核验确认仍存在的问题才影响整体风险判断。
        "affects_risk": bool(verified),
        "findings": sorted(findings, key=lambda item: item["discussion_id"]),
    }


def verified_historical_defects(task) -> list[dict]:
    outcome = historical_discussion_outcome(task)
    return list(outcome["verified_defects"]) if outcome else []
