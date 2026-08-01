"""Issue 的确定性准入、严重度归一化与自动修复资格策略。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.models.review import (
    DeterministicIssueCheck,
    EvidenceResolutionMethod,
    IssueCategory,
    IssueStatus,
    ReviewIssue,
    ReviewUnit,
    Severity,
)


_CRITICAL_SIGNALS = (
    "permission bypass", "authorization bypass", "权限绕过", "认证绕过",
    "remote code execution", "rce", "远程代码执行",
    "secret leak", "credential leak", "密钥泄露", "凭据泄露",
    "irreversible data", "不可逆数据", "永久数据丢失",
    "billing error", "payment error", "计费错误", "资金错误", "重复扣款",
    "large-scale outage", "大规模生产故障", "全站不可用",
)
_HIGH_SIGNALS = (
    "api contract", "breaking api", "api 合约", "接口契约",
    "data consistency", "数据一致性", "race condition", "并发错误", "竞态",
    "authentication", "authorization", "权限", "认证", "security", "安全",
    "major feature", "主要功能", "核心功能", "data corruption", "数据损坏",
)
_STYLE_SIGNALS = (
    "style only", "formatting", "naming preference", "pure style",
    "仅风格", "格式偏好", "命名偏好", "代码风格",
)
_VAGUE_ASSUMPTIONS = {"unknown", "maybe", "可能", "不确定", "待确认"}


class SeverityPolicy:
    """只依据服务端已解析证据与明确故障类型给严重度设上限。"""

    _rank = {
        Severity.low: 0,
        Severity.medium: 1,
        Severity.high: 2,
        Severity.critical: 3,
    }

    def normalize(self, issue: ReviewIssue) -> Severity:
        requested = issue.severity
        text = self._issue_text(issue)
        evidence_is_strong = self._has_strong_primary_evidence(issue)

        if requested == Severity.critical:
            if evidence_is_strong and issue.confidence >= 0.8 and self._contains(text, _CRITICAL_SIGNALS):
                return Severity.critical
            requested = Severity.high

        if requested == Severity.high:
            high_signal = self._contains(text, _HIGH_SIGNALS) or self._contains(text, _CRITICAL_SIGNALS)
            if evidence_is_strong and issue.confidence >= 0.75 and high_signal:
                return Severity.high
            return Severity.medium

        if requested == Severity.medium and issue.confidence < 0.45:
            return Severity.low
        return requested

    def is_adjustment(self, issue: ReviewIssue, normalized: Severity) -> bool:
        return normalized != issue.severity

    def does_not_raise(self, current: Severity, proposed: Severity | None) -> Severity:
        if proposed is None or self._rank[proposed] >= self._rank[current]:
            return current
        return proposed

    @staticmethod
    def _has_strong_primary_evidence(issue: ReviewIssue) -> bool:
        anchor = issue.primary_evidence
        return (
            anchor.resolution_method != EvidenceResolutionMethod.unresolved
            and anchor.match_count == 1
            and anchor.anchor_hash is not None
            and anchor.resolved_start_line is not None
            and anchor.resolved_side is not None
        )

    @staticmethod
    def _issue_text(issue: ReviewIssue) -> str:
        return " ".join((issue.title, issue.affected_behavior, issue.failure_scenario)).casefold()

    @staticmethod
    def _contains(text: str, signals: tuple[str, ...]) -> bool:
        return any(signal in text for signal in signals)


class IssuePolicyService:
    """在 verifier 前拒绝不可定位、越界、含糊或违反策略的 Issue。"""

    def __init__(self, severity_policy: SeverityPolicy | None = None) -> None:
        self.severity_policy = severity_policy or SeverityPolicy()

    def check(
        self,
        raw_issue: ReviewIssue | dict[str, Any],
        review_units: list[ReviewUnit],
    ) -> DeterministicIssueCheck:
        issue_id = (
            raw_issue.id if isinstance(raw_issue, ReviewIssue)
            else str(raw_issue.get("id") or "invalid")
        )
        try:
            issue = (
                raw_issue if isinstance(raw_issue, ReviewIssue)
                else ReviewIssue.model_validate(raw_issue)
            )
        except ValidationError as exc:
            return DeterministicIssueCheck(
                issue_id=issue_id,
                passed=False,
                reasons=[f"schema_invalid:{exc.errors()[0]['type']}"],
            )

        reasons: list[str] = []
        by_id = {unit.id: unit for unit in review_units}
        unit = by_id.get(issue.review_unit_id)
        if unit is None:
            reasons.append("review_unit_not_found")
        else:
            if issue.primary_evidence.file_path not in set(unit.primary_files):
                reasons.append("primary_file_out_of_scope")

        anchor = issue.primary_evidence
        if (
            anchor.resolution_method == EvidenceResolutionMethod.unresolved
            or anchor.match_count != 1
            or anchor.anchor_hash is None
            or anchor.resolved_start_line is None
            or anchor.resolved_end_line is None
            or anchor.resolved_side is None
        ):
            reasons.append("primary_evidence_not_uniquely_resolved")

        if not issue.affected_behavior.strip():
            reasons.append("affected_behavior_empty")
        if not issue.failure_scenario.strip():
            reasons.append("failure_scenario_empty")
        if not 0 <= issue.confidence <= 1:
            reasons.append("confidence_out_of_range")
        if any(not value.strip() or value.strip().casefold() in _VAGUE_ASSUMPTIONS for value in issue.assumptions):
            reasons.append("assumptions_not_explicit")
        if self._is_unruled_style_preference(issue, unit):
            reasons.append("pure_style_preference_without_rule")
        if self._has_side_conflict(issue):
            reasons.append("evidence_side_conflict")

        normalized = self.severity_policy.normalize(issue)
        return DeterministicIssueCheck(
            issue_id=issue.id,
            passed=not reasons,
            reasons=reasons,
            normalized_severity=normalized,
        )

    def auto_fix_allowed(self, issue: ReviewIssue) -> bool:
        """保留旧字段，但将最终资格收敛到低风险、已确认前置条件。"""
        return bool(
            issue.auto_fix_eligible
            and issue.status == IssueStatus.evidence_resolved
            and issue.severity in {Severity.low, Severity.medium}
            and issue.confidence >= 0.8
            and not issue.assumptions
            and not issue.requires_human_confirmation
            and issue.primary_evidence.resolved_side == "head"
        )

    @staticmethod
    def _is_unruled_style_preference(
        issue: ReviewIssue, unit: ReviewUnit | None
    ) -> bool:
        if issue.category != IssueCategory.maintainability:
            return False
        text = " ".join((issue.title, issue.affected_behavior, issue.failure_scenario)).casefold()
        is_style = any(signal in text for signal in _STYLE_SIGNALS)
        explicit_rules = set(unit.rule_ids if unit else []) - {"review.general"}
        return is_style and not explicit_rules

    @staticmethod
    def _has_side_conflict(issue: ReviewIssue) -> bool:
        anchors = [issue.primary_evidence, *issue.supporting_evidence]
        if any(anchor.unresolved_reason == "side_conflict" for anchor in anchors):
            return True
        seen: dict[tuple[str, str], str] = {}
        for anchor in anchors:
            if anchor.resolved_side is None:
                continue
            key = (
                anchor.file_path,
                re.sub(r"\s+", " ", anchor.existing_code).strip().casefold(),
            )
            previous = seen.setdefault(key, anchor.resolved_side)
            if previous != anchor.resolved_side:
                return True
        return False
