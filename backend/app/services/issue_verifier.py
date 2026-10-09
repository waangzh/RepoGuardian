"""独立 Issue verifier：最小只读输入、逐 Issue 隔离失败和 Unit 预算。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

from app.agents.providers import LLMProvider
from app.models.review import (
    ChangedFile,
    CommentPlacement,
    ContextSnippet,
    EvidenceAnchor,
    EvidenceResolutionMethod,
    IssueMetrics,
    IssueStatus,
    IssueVerification,
    IssueVerificationBudget,
    IssueVerificationDecision,
    IssueVerificationRequest,
    ReviewIssue,
    ModelUsage,
    ReviewUnit,
)
from app.services.issue_policy import SeverityPolicy
from app.services.model_usage import annotate_usage, unpack_model_call
from app.services.review_planner import DeterministicReviewPlanner


@dataclass(frozen=True)
class IssueVerifierBatchResult:
    issues: list[ReviewIssue]
    verifications: list[IssueVerification]
    metrics: IssueMetrics
    warnings: list[str]
    model_usages: list[ModelUsage]


class IssueVerifierService:
    """调用独立 provider 方法；任何单次失败都不会确认该 Issue。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        enabled: bool,
        fail_mode: Literal["unresolved", "candidate", "needs_human"],
        max_calls_per_unit: int,
        timeout_seconds: int = 60,
    ) -> None:
        if max_calls_per_unit < 0:
            raise ValueError("verifier max calls per unit must not be negative")
        self.provider = provider
        self.enabled = enabled
        self.fail_mode = fail_mode
        self.max_calls_per_unit = max_calls_per_unit
        self.timeout_seconds = timeout_seconds
        self.severity_policy = SeverityPolicy()

    async def verify_issues(
        self,
        issues: list[ReviewIssue],
        units: list[ReviewUnit],
        state: dict[str, Any],
        metrics: IssueMetrics,
    ) -> IssueVerifierBatchResult:
        if not self.enabled:
            pending = [
                self._on_failure(issue, "verifier_disabled")
                if issue.status == IssueStatus.evidence_resolved else issue
                for issue in issues
            ]
            return IssueVerifierBatchResult(pending, [], metrics, [], [])

        by_unit = {unit.id: unit for unit in units}
        calls_by_unit: dict[str, int] = {}
        output: list[ReviewIssue] = []
        decisions: list[IssueVerification] = []
        warnings: list[str] = []
        model_usages: list[ModelUsage] = []
        call_count = metrics.verifier_call_count
        token_count = metrics.verifier_token_count
        verifier_drop_count = metrics.verifier_drop_count
        severity_adjustments = metrics.severity_adjustment_count

        for issue in issues:
            if issue.status != IssueStatus.evidence_resolved:
                output.append(issue)
                continue
            unit = by_unit.get(issue.review_unit_id)
            used = calls_by_unit.get(issue.review_unit_id, 0)
            if unit is None or used >= self.max_calls_per_unit:
                reason = "review_unit_not_found" if unit is None else "verifier_budget_exhausted"
                output.append(self._on_failure(issue, reason))
                warnings.append(f"Issue {issue.id} 未完成独立验证：{reason}")
                continue

            request = self._build_request(issue, unit, state, used)
            calls_by_unit[unit.id] = used + 1
            call_count += 1
            token_count += self._estimate_tokens(request)
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    raw_result = await self.provider.verify_issue(
                        request, state.get("model")
                    )
                verification, usage = unpack_model_call(raw_result)
                usage = annotate_usage(
                    usage,
                    accounted_tokens_estimate=self._estimate_tokens(request),
                    review_unit_id=unit.id,
                    unit_complexity=unit.complexity,
                )
                if usage is not None:
                    model_usages.append(usage)
                verification = IssueVerification.model_validate(verification)
                self._validate_decision(issue, unit, verification, request)
                if verification.decision == IssueVerificationDecision.keep and (
                    verification.contradicting_evidence or verification.contradicting_existing_paths
                    or not request.evidence_complete
                ):
                    verification = verification.model_copy(update={
                        "decision": IssueVerificationDecision.unresolved,
                        "reason": "verifier_counterevidence_or_incomplete_context: " + verification.reason[:940],
                    })
            except Exception as exc:
                usage = getattr(exc, "usage", None)
                usage = annotate_usage(
                    usage,
                    accounted_tokens_estimate=self._estimate_tokens(request),
                    review_unit_id=unit.id,
                    unit_complexity=unit.complexity,
                )
                if usage is not None:
                    model_usages.append(usage)
                reason = f"{type(exc).__name__}:{exc}"
                output.append(self._on_failure(issue, reason))
                warnings.append(f"Issue {issue.id} verifier 失败：{reason}")
                continue

            decisions.append(self._sanitize_contradictions(verification, unit))
            updated = issue
            if verification.adjusted_severity is not None:
                adjusted = self.severity_policy.does_not_raise(
                    issue.severity, verification.adjusted_severity
                )
                if adjusted != issue.severity:
                    updated = updated.model_copy(update={"severity": adjusted})
                    severity_adjustments += 1

            if verification.decision == IssueVerificationDecision.keep:
                updated = updated.model_copy(update={"status": IssueStatus.confirmed})
            elif verification.decision == IssueVerificationDecision.drop:
                updated = updated.model_copy(update={
                    "status": IssueStatus.dismissed,
                    "auto_fix_eligible": False,
                    "unresolved_reason": "verifier_drop",
                })
                verifier_drop_count += 1
            else:
                updated = updated.model_copy(update={
                    "status": IssueStatus.unresolved,
                    "requires_human_confirmation": False,
                    "auto_fix_eligible": False,
                    "placement": CommentPlacement.unresolved,
                    "unresolved_reason": "verifier_unresolved",
                })
            output.append(updated)

        updated_metrics = metrics.model_copy(update={
            "verifier_call_count": call_count,
            "verifier_token_count": token_count,
            "verifier_drop_count": verifier_drop_count,
            "severity_adjustment_count": severity_adjustments,
        })
        return IssueVerifierBatchResult(
            output, decisions, updated_metrics, warnings, model_usages
        )

    def _build_request(
        self,
        issue: ReviewIssue,
        unit: ReviewUnit,
        state: dict[str, Any],
        used_calls: int,
    ) -> IssueVerificationRequest:
        context_budget = 12_000
        context: list[ContextSnippet] = []
        consumed = 0
        complete = True
        readable = {
            issue.primary_evidence.file_path,
            *(anchor.file_path for anchor in issue.supporting_evidence),
        }
        for raw in state.get("context_snippets") or []:
            snippet = ContextSnippet.model_validate(raw)
            # 双侧渲染的 diff 由 unit_diff 的 canonical hunks 提供，不能标成 Head 正文。
            if snippet.source == "file_read_diff":
                continue
            if snippet.review_unit_id not in {None, unit.id} or snippet.file not in readable:
                continue
            remaining = context_budget - consumed
            if len(snippet.content) > remaining or len(context) >= 20:
                complete = False
                continue
            complete = complete and not snippet.truncated
            context.append(snippet)
            consumed += len(snippet.content)

        # 只提供候选涉及的文件差分，保留该文件的完整 Unit hunks 与循环体/早退。
        # 其他文件的路径存在事实不提供正文，也不会扩张读取权限。
        focused_unit = unit.model_copy(update={"primary_files": [path for path in unit.primary_files if path in readable]})
        unit_diff = self._unit_diff(focused_unit, state)
        cited_text = issue.model_dump_json() + unit_diff
        known_paths = sorted({raw["path"] for raw in state.get("file_index") or []
            if isinstance(raw.get("path"), str) and raw["path"] in cited_text})[:50]
        return IssueVerificationRequest(
            issue=issue,
            primary_evidence=issue.primary_evidence,
            supporting_evidence=issue.supporting_evidence,
            unit_diff=unit_diff[:60_000],
            readonly_context=context,
            known_existing_paths=known_paths,
            evidence_complete=complete and len(unit_diff) <= 60_000,
            applicable_rules=unit.rule_ids,
            budget=IssueVerificationBudget(
                remaining_calls=self.max_calls_per_unit - used_calls,
                max_context_chars=context_budget,
            ),
        )

    @staticmethod
    def _unit_diff(unit: ReviewUnit, state: dict[str, Any]) -> str:
        planner = DeterministicReviewPlanner()
        changed = [ChangedFile.model_validate(item) for item in state.get("changed_files") or []]
        by_path = {item.file_path: item for item in changed}
        hunk_ids = {
            path: [
                planner.hunk_id(path, index, hunk.model_dump(mode="json"))
                for index, hunk in enumerate(item.hunks)
            ]
            for path, item in by_path.items()
        }
        return planner.normalized_unit_diff(unit, by_path, hunk_ids)

    def _on_failure(self, issue: ReviewIssue, reason: str) -> ReviewIssue:
        if self.fail_mode != "candidate":
            return issue.model_copy(update={
                "status": IssueStatus.unresolved,
                "requires_human_confirmation": False,
                "auto_fix_eligible": False,
                "placement": CommentPlacement.unresolved,
                "unresolved_reason": f"verifier_failure:{reason}",
            })
        return issue.model_copy(update={
            "status": IssueStatus.candidate,
            "requires_human_confirmation": False,
            "auto_fix_eligible": False,
            "unresolved_reason": f"verifier_failure:{reason}",
        })

    @staticmethod
    def _validate_decision(
        issue: ReviewIssue,
        unit: ReviewUnit,
        verification: IssueVerification,
        request: IssueVerificationRequest,
    ) -> None:
        if verification.issue_id != issue.id:
            raise ValueError("verifier_issue_id_mismatch")
        del unit
        if set(verification.contradicting_existing_paths) - set(request.known_existing_paths):
            raise ValueError("verifier_path_fact_not_provided")
        sources = IssueVerifierService._code_sources(request)
        readable = {path for path, _ in sources}
        if any(anchor.file_path not in readable for anchor in verification.contradicting_evidence):
            raise ValueError("verifier_evidence_out_of_scope")
        for anchor in verification.contradicting_evidence:
            side = anchor.expected_side or "head"
            sides = ("head", "base") if side == "either" else (side,)
            if not anchor.existing_code.strip() or not any(
                anchor.existing_code in text for candidate_side in sides
                for text in sources.get((anchor.file_path, candidate_side), [])
            ):
                raise ValueError("verifier_counterevidence_not_displayed")

    @staticmethod
    def _code_sources(request: IssueVerificationRequest) -> dict[tuple[str, str], list[str]]:
        """代码反证只能引用已显示的可信正文；路径库存不能成为正文来源。"""
        sources = {}
        def add(path, side, content):
            sources.setdefault((path, side), []).append(content)
        for anchor in [request.primary_evidence, *request.supporting_evidence]:
            if anchor.resolution_method != EvidenceResolutionMethod.unresolved:
                add(anchor.file_path, anchor.resolved_side or "head", anchor.existing_code)
        for snippet in request.readonly_context:
            if snippet.source != "file_read_diff":
                add(snippet.file, "head", snippet.content)
        try:
            diff = json.loads(request.unit_diff)
        except (ValueError, TypeError):
            diff = []
        for file in diff if isinstance(diff, list) else []:
            if not isinstance(file, dict) or not isinstance(file.get("file_path"), str):
                continue
            for hunk in file.get("hunks", []):
                for side, omitted, fallback in (("head", "deleted", "added_lines"), ("base", "added", "removed_lines")):
                    lines = hunk.get("lines") or []
                    if lines:
                        add(file["file_path"], side, "\n".join(line["content"] for line in lines if line["kind"] != omitted))
                    else:
                        # 旧输入没有上下文行时，不把不连续的新增/删除行拼成虚构片段。
                        for line in hunk.get(fallback, []):
                            add(file["file_path"], side, line["content"])
        return sources

    @staticmethod
    def _sanitize_contradictions(
        verification: IssueVerification,
        unit: ReviewUnit,
    ) -> IssueVerification:
        del unit
        anchors = [
            EvidenceAnchor(
                file_path=anchor.file_path,
                existing_code=anchor.existing_code,
                symbol=anchor.symbol,
                expected_side=anchor.expected_side,
                expected_hunk_id=anchor.expected_hunk_id,
                context_before=anchor.context_before,
                context_after=anchor.context_after,
                resolution_method=EvidenceResolutionMethod.unresolved,
            )
            for anchor in verification.contradicting_evidence
        ]
        return verification.model_copy(update={"contradicting_evidence": anchors})

    @staticmethod
    def _estimate_tokens(request: IssueVerificationRequest) -> int:
        payload = request.model_dump_json()
        return max(1, (len(payload) + 3) // 4)
