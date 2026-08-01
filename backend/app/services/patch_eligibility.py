"""候选补丁资格判定与最小 Provider 输入构建。"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.models.review import (
    ContextSnippet,
    EvidenceResolutionMethod,
    FixRisk,
    IssueCategory,
    IssueStatus,
    PatchEligibilityDecision,
    PatchGenerationRequest,
    PatchRelatedSymbol,
    ReviewIssue,
)


_LOCK_FILES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
})
_MIGRATION_PARTS = frozenset({"migration", "migrations", "alembic", "schema"})
_SENSITIVE_TERMS = (
    "permission",
    "authorization",
    "billing",
    "payment",
    "money",
    "pricing decision",
    "product decision",
    "business decision",
    "public api",
    "breaking change",
    "unknown runtime",
)
_PROHIBITED_OPERATIONS = [
    "只输出标准 unified diff，不使用 Markdown 包裹补丁",
    "不得修改 allowed_files 之外的文件",
    "不得新增依赖，除非 Issue 明确是依赖问题",
    "不得修改 lockfile，除非该文件明确在 allowed_files 中",
    "不得修改 CI/workflow，除非 Issue 的 primary evidence 位于该文件",
    "不得进行大规模重构或公共 API 重构",
    "保持仓库现有风格",
    "不得声称测试通过或补丁已验证",
    "无法安全修复时必须输出 abandon 结构",
]


class PatchEligibilityPolicy:
    """用服务端结构化事实保守筛选可生成候选补丁的 Issue。"""

    def __init__(
        self,
        confidence_threshold: float | None = None,
        max_files: int | None = None,
        max_changed_lines: int | None = None,
    ) -> None:
        self._confidence_threshold = (
            settings.repoguardian_patch_confidence_threshold
            if confidence_threshold is None else confidence_threshold
        )
        self._max_files = settings.repoguardian_patch_max_files if max_files is None else max_files
        self._max_changed_lines = (
            settings.repoguardian_patch_max_changed_lines
            if max_changed_lines is None else max_changed_lines
        )

    def evaluate(self, issue: ReviewIssue) -> PatchEligibilityDecision:
        reasons: list[str] = []
        anchor = issue.primary_evidence
        allowed_files = [anchor.file_path]

        if issue.status != IssueStatus.confirmed:
            reasons.append("Issue 状态不是 confirmed")
        if (
            anchor.resolution_method == EvidenceResolutionMethod.unresolved
            or anchor.match_count != 1
            or anchor.resolved_start_line is None
            or anchor.resolved_end_line is None
            or not anchor.anchor_hash
        ):
            reasons.append("primary evidence 未唯一定位")
        if issue.confidence < self._confidence_threshold:
            reasons.append(f"Issue confidence 低于阈值 {self._confidence_threshold:.2f}")
        if not issue.auto_fix_eligible:
            reasons.append("auto_fix_eligible=false")
        if issue.requires_human_confirmation:
            reasons.append("修复需要人工、产品或业务决策")
        if issue.fix_risk != FixRisk.low:
            reasons.append("问题风险过高，不允许自动生成候选补丁")
        if issue.category == IssueCategory.security:
            reasons.append("安全或权限类问题不进入自动候选补丁")

        path_parts = {part.lower() for part in anchor.file_path.split("/")}
        if path_parts & _MIGRATION_PARTS:
            reasons.append("涉及 migration/schema，可能不可逆")
        if anchor.file_path.rsplit("/", 1)[-1] in _LOCK_FILES:
            reasons.append("lockfile 修改不在本阶段自动修复范围")

        issue_text = " ".join([
            issue.title,
            issue.affected_behavior,
            issue.failure_scenario,
            issue.recommendation,
            *issue.assumptions,
        ]).lower()
        if any(term in issue_text for term in _SENSITIVE_TERMS):
            reasons.append("问题涉及权限、资金、公共 API、业务决策或未知运行时")

        if not reasons:
            reasons.append("confirmed Issue 具备唯一证据且修复范围受限")
        return PatchEligibilityDecision(
            issue_id=issue.id,
            eligible=len(reasons) == 1 and reasons[0].startswith("confirmed Issue"),
            reasons=reasons,
            allowed_files=allowed_files,
            max_files=min(self._max_files, len(allowed_files)),
            max_changed_lines=self._max_changed_lines,
        )

    def evaluate_all(self, issues: list[ReviewIssue]) -> list[PatchEligibilityDecision]:
        return [self.evaluate(issue) for issue in issues]

    def build_requests(
        self,
        issues: list[ReviewIssue],
        decisions: list[PatchEligibilityDecision],
        *,
        symbol_index: list[dict[str, Any]],
        context_snippets: list[dict[str, Any]],
        head_sha: str,
    ) -> list[PatchGenerationRequest]:
        """按单 Issue 构建 prompt 输入；不把未准入 Issue 或全仓 diff 传给模型。"""
        issue_by_id = {issue.id: issue for issue in issues}
        requests: list[PatchGenerationRequest] = []
        for decision in decisions:
            if not decision.eligible:
                continue
            issue = issue_by_id[decision.issue_id]
            allowed = set(decision.allowed_files)
            related_symbols = [
                PatchRelatedSymbol(
                    file=item["file"],
                    symbol=item["symbol"],
                    type=item.get("type", "unknown"),
                    lines=(int(item.get("start_line", 1)), int(item.get("end_line", 1))),
                    signature=str(item.get("signature", ""))[:500],
                )
                for item in symbol_index
                if item.get("file") in allowed
                and (
                    not issue.primary_evidence.symbol
                    or item.get("symbol") == issue.primary_evidence.symbol
                    or _line_overlaps(item, issue)
                )
            ][:12]
            context = [
                ContextSnippet.model_validate(item)
                for item in context_snippets
                if item.get("file") in allowed
            ][:12]
            requests.append(PatchGenerationRequest(
                issue=issue,
                primary_evidence=issue.primary_evidence,
                supporting_evidence=[
                    evidence for evidence in issue.supporting_evidence
                    if evidence.resolution_method != EvidenceResolutionMethod.unresolved
                ],
                related_symbols=related_symbols,
                limited_context=context,
                allowed_files=decision.allowed_files,
                max_files=decision.max_files,
                max_changed_lines=decision.max_changed_lines,
                head_sha=head_sha,
                prohibited_operations=_PROHIBITED_OPERATIONS,
            ))
        return requests


def decisions_by_issue(
    decisions: list[PatchEligibilityDecision],
) -> dict[str, PatchEligibilityDecision]:
    return {decision.issue_id: decision for decision in decisions}


def _line_overlaps(item: dict[str, Any], issue: ReviewIssue) -> bool:
    start = issue.primary_evidence.resolved_start_line
    end = issue.primary_evidence.resolved_end_line
    if start is None or end is None:
        return False
    return int(item.get("start_line", 0)) <= end and int(item.get("end_line", 0)) >= start
