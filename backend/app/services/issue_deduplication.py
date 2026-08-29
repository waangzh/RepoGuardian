"""Issue 的保守候选分组、可选语义判定和稳定全局聚合。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from app.agents.providers import LLMProvider
from app.core.config import settings
from app.models.review import (
    EvidenceAnchor,
    IssueDeduplicationDecision,
    IssueMetrics,
    IssueStatus,
    ModelUsage,
    ReviewIssue,
    Severity,
)
from app.services.model_usage import unpack_model_call

logger = logging.getLogger("RepoGuardian.IssueDeduplication")


@dataclass(frozen=True)
class IssueDeduplicationResult:
    issues: list[ReviewIssue]
    decisions: list[IssueDeduplicationDecision]
    metrics: IssueMetrics
    model_usages: list[ModelUsage]


class IssueDeduplicationService:
    """完全相同 anchor 可确定性合并，其余候选组必须由可选模型确认。"""

    _severity_rank = {
        Severity.critical: 0,
        Severity.high: 1,
        Severity.medium: 2,
        Severity.low: 3,
    }

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self._timeout_seconds = (
            settings.repoguardian_issue_dedup_timeout_seconds
            if timeout_seconds is None else max(0.0, timeout_seconds)
        )

    async def aggregate(
        self,
        issues: list[ReviewIssue],
        provider: LLMProvider,
        model: str | None,
        metrics: IssueMetrics,
    ) -> IssueDeduplicationResult:
        publishable = [
            issue for issue in issues
            if issue.status in {IssueStatus.confirmed, IssueStatus.needs_human}
        ]
        by_id = {issue.id: issue for issue in publishable}
        removed: set[str] = set()
        decisions: list[IssueDeduplicationDecision] = []
        model_usages: list[ModelUsage] = []

        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        for group, exact_anchor_group in self._candidate_groups(publishable):
            active = [by_id[issue.id] for issue in group if issue.id not in removed]
            if len(active) < 2:
                continue
            decision: IssueDeduplicationDecision | None = None
            if exact_anchor_group:
                decision = IssueDeduplicationDecision(
                    canonical_issue_id=active[0].id,
                    duplicate_issue_ids=[issue.id for issue in active[1:]],
                    merged_rationale="deterministic_same_category_and_anchor",
                )
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    logger.warning("Semantic dedup 超过 %.1f 秒硬上限，保留未合并 Issue", self._timeout_seconds)
                    break
                try:
                    async with asyncio.timeout(remaining):
                        raw_result = await provider.deduplicate_issues(active, model)
                    proposed, usage = unpack_model_call(raw_result)
                    if usage is not None:
                        model_usages.append(usage)
                    decision = IssueDeduplicationDecision.model_validate(proposed)
                    self._validate_decision(active, decision, exact_anchor_group)
                except TimeoutError:
                    logger.warning("Semantic dedup 超过 %.1f 秒硬上限，保留未合并 Issue", self._timeout_seconds)
                    break
                except Exception as exc:
                    usage = getattr(exc, "usage", None)
                    if usage is not None and all(
                        item.id != usage.id for item in model_usages
                    ):
                        model_usages.append(usage)
            if decision is None or not decision.duplicate_issue_ids:
                continue

            canonical = by_id[decision.canonical_issue_id]
            duplicates = [by_id[issue_id] for issue_id in decision.duplicate_issue_ids]
            by_id[canonical.id] = self._merge(canonical, duplicates)
            removed.update(issue.id for issue in duplicates)
            decisions.append(decision)

        final = [issue for issue_id, issue in by_id.items() if issue_id not in removed]
        final.sort(key=self._stable_key)
        updated_metrics = metrics.model_copy(update={
            "duplicate_count": len(removed),
            "confirmed_count": sum(issue.status == IssueStatus.confirmed for issue in final),
            "needs_human_count": sum(issue.status == IssueStatus.needs_human for issue in final),
        })
        return IssueDeduplicationResult(final, decisions, updated_metrics, model_usages)

    def _candidate_groups(
        self, issues: list[ReviewIssue]
    ) -> list[tuple[list[ReviewIssue], bool]]:
        exact: dict[tuple[str, str, str, str], list[ReviewIssue]] = defaultdict(list)
        semantic: dict[tuple[str, str, str, str, str], list[ReviewIssue]] = defaultdict(list)
        for issue in issues:
            anchor = issue.primary_evidence
            exact_key = (
                issue.status.value,
                issue.category.value,
                anchor.file_path,
                anchor.anchor_hash or "",
            )
            if anchor.anchor_hash:
                exact[exact_key].append(issue)
            root = self._normalized_root_cause(issue)
            symbol = anchor.symbol or ""
            if symbol and root:
                semantic[(
                    issue.status.value,
                    issue.category.value,
                    anchor.file_path,
                    symbol.casefold(),
                    root,
                )].append(issue)

        grouped: list[tuple[list[ReviewIssue], bool]] = []
        consumed_exact: set[frozenset[str]] = set()
        for group in exact.values():
            if len(group) > 1:
                ids = frozenset(issue.id for issue in group)
                consumed_exact.add(ids)
                grouped.append((self._stable(group), True))
        for group in semantic.values():
            ids = frozenset(issue.id for issue in group)
            if len(group) > 1 and ids not in consumed_exact:
                grouped.append((self._stable(group), False))
        return grouped

    @classmethod
    def _validate_decision(
        cls,
        group: list[ReviewIssue],
        decision: IssueDeduplicationDecision,
        exact_anchor_group: bool,
    ) -> None:
        allowed = {issue.id for issue in group}
        duplicates = set(decision.duplicate_issue_ids)
        if decision.canonical_issue_id not in allowed:
            raise ValueError("dedup_canonical_out_of_group")
        if not duplicates <= allowed or decision.canonical_issue_id in duplicates:
            raise ValueError("dedup_duplicates_out_of_group")
        if len(duplicates) != len(decision.duplicate_issue_ids):
            raise ValueError("dedup_duplicate_ids_repeated")
        selected = [
            issue for issue in group
            if issue.id == decision.canonical_issue_id or issue.id in duplicates
        ]
        anchors = {issue.primary_evidence.anchor_hash for issue in selected}
        symbols = {issue.primary_evidence.symbol for issue in selected}
        if not exact_anchor_group and len(anchors) > 1 and (None in symbols or len(symbols) > 1):
            raise ValueError("dedup_unrelated_anchors")

    @staticmethod
    def _merge(canonical: ReviewIssue, duplicates: list[ReviewIssue]) -> ReviewIssue:
        anchors = [*canonical.supporting_evidence]
        for issue in duplicates:
            anchors.extend([issue.primary_evidence, *issue.supporting_evidence])
        supporting: list[EvidenceAnchor] = []
        seen = {IssueDeduplicationService._anchor_key(canonical.primary_evidence)}
        for anchor in anchors:
            key = IssueDeduplicationService._anchor_key(anchor)
            if key in seen:
                continue
            seen.add(key)
            supporting.append(anchor)
            if len(supporting) == 12:
                break
        source_units = list(dict.fromkeys(
            unit_id
            for issue in [canonical, *duplicates]
            for unit_id in issue.source_review_unit_ids
        ))
        source_issues = list(dict.fromkeys(
            issue_id
            for issue in [canonical, *duplicates]
            for issue_id in issue.source_issue_ids
        ))
        return canonical.model_copy(update={
            "supporting_evidence": supporting,
            "source_review_unit_ids": source_units,
            "source_issue_ids": source_issues,
        })

    @staticmethod
    def _anchor_key(anchor: EvidenceAnchor) -> tuple[str, str, str, str]:
        return (
            anchor.anchor_hash or "",
            anchor.file_path,
            anchor.resolved_side or anchor.expected_side,
            re.sub(r"\s+", " ", anchor.existing_code).strip(),
        )

    @staticmethod
    def _normalized_root_cause(issue: ReviewIssue) -> str:
        text = f"{issue.affected_behavior} {issue.failure_scenario}".casefold()
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)[:400]

    @classmethod
    def _stable(cls, issues: list[ReviewIssue]) -> list[ReviewIssue]:
        return sorted(issues, key=cls._stable_key)

    @classmethod
    def _stable_key(cls, issue: ReviewIssue) -> tuple[int, str, int, str]:
        return (
            cls._severity_rank[issue.severity],
            issue.primary_evidence.file_path,
            issue.primary_evidence.resolved_start_line or 2**31,
            issue.id,
        )
