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
from app.services.review_run_budget import current_run_budget, run_budget_attempt_observer
from app.services.review_runtime import ReviewRuntime, ReviewTask


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
        output: dict[int, ReviewIssue] = {}
        runtime = ReviewRuntime()
        run_budget = current_run_budget()
        local_revision = 0
        pending = list(enumerate(issues))
        decisions: list[IssueVerification] = []
        warnings: list[str] = []
        model_usages: list[ModelUsage] = []
        verified_inputs: dict[str, IssueVerification] = {}
        call_count = metrics.verifier_call_count
        token_count = metrics.verifier_token_count
        verifier_drop_count = metrics.verifier_drop_count
        severity_adjustments = metrics.severity_adjustment_count

        def revision():
            return (run_budget.revision if run_budget is not None else 0) + local_revision

        async def requeue(task):
            pending.append(task.payload)

        while pending or runtime.deferred.pending():
            if not pending:
                await runtime.deferred.drain(budget_revision=revision(),
                    admit=lambda _task: {"admitted": True}, execute=requeue)
                if not pending:
                    break
            index, issue = pending.pop(0)
            if issue.status != IssueStatus.evidence_resolved:
                output[index] = issue
                continue
            unit = by_unit.get(issue.review_unit_id)
            used = calls_by_unit.get(issue.review_unit_id, 0)
            if unit is None:
                reason = "review_unit_not_found" if unit is None else "verifier_budget_exhausted"
                output[index] = self._on_failure(issue, reason)
                warnings.append(f"Issue {issue.id} 未完成独立验证：{reason}")
                continue

            request = self._build_request(issue, unit, state, used)
            key = self._verification_input_key(request, unit, state)
            cached = verified_inputs.get(key)
            reused = cached is not None
            if not reused and run_budget is None and used >= self.max_calls_per_unit:
                output[index] = self._on_failure(issue, "verifier_budget_exhausted")
                warnings.append(f"Issue {issue.id} 未完成独立验证：verifier_budget_exhausted")
                continue
            observed_attempts = []
            outer_observer = run_budget_attempt_observer.get()
            async def observe(record, admission):
                observed_attempts.append(record.attempt_id)
                if outer_observer is not None:
                    await outer_observer(record, admission)
            observer_token = run_budget_attempt_observer.set(observe) if run_budget is not None else None
            rejected = False
            usage = None
            try:
                if reused:
                    verification = cached.model_copy(update={"issue_id": issue.id}, deep=True)
                    from app.review.issue_audit import audit_issue
                    audit_issue("verifier_reuse", issue, reason="identical_verified_input:" + cached.issue_id)
                else:
                    async with asyncio.timeout(self.timeout_seconds):
                        raw_result = await self.provider.verify_issue(request, state.get("model"))
                    verification, usage = unpack_model_call(raw_result)
                    usage = annotate_usage(usage, accounted_tokens_estimate=self._estimate_tokens(request),
                                           review_unit_id=unit.id, unit_complexity=unit.complexity)
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
                if (not reused and usage is not None and usage.usage_available and request.evidence_complete
                        and verification.decision in {IssueVerificationDecision.keep, IssueVerificationDecision.drop}):
                    verified_inputs[key] = verification.model_copy(deep=True)
            except Exception as exc:
                rejection = getattr(exc, "admission_rejection", None)
                if rejection and rejection.get("kind") == "budget":
                    rejected = not observed_attempts and not rejection.get("transport_retry", False)
                    if rejection.get("can_defer") and not observed_attempts:
                        runtime.defer(ReviewTask(f"verify:{issue.id}", "verify_issue", priority=(index,),
                            payload=(index, issue)), reason=rejection["reason"], budget_revision=revision())
                        continue
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
                output[index] = self._on_failure(issue, reason)
                warnings.append(f"Issue {issue.id} verifier 失败：{reason}")
                continue
            finally:
                if observer_token is not None:
                    run_budget_attempt_observer.reset(observer_token)
                if run_budget is not None:
                    sent = [run_budget.attempts[key] for key in observed_attempts
                            if run_budget.attempts[key].sent]
                    call_count += len(sent)
                    calls_by_unit[unit.id] = used + len(sent)
                    token_count += sum(item.actual_tokens if item.actual_tokens is not None
                                       else item.reserved_tokens for item in sent)
                elif not rejected and not reused:
                    calls_by_unit[unit.id] = used + 1
                    call_count += 1
                    token_count += self._estimate_tokens(request)
                if not rejected and not reused:
                    local_revision += 1

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
            output[index] = updated

        for task in runtime.deferred.pending():
            index, issue = task.payload
            output[index] = self._on_failure(issue, "deferred_budget_not_executed:" + (task.reason or "budget"))
            warnings.append(f"Issue {issue.id} 未完成独立验证：{task.reason}")

        updated_metrics = metrics.model_copy(update={
            "verifier_call_count": call_count,
            "verifier_token_count": token_count,
            "verifier_drop_count": verifier_drop_count,
            "severity_adjustment_count": severity_adjustments,
        })
        return IssueVerifierBatchResult(
            [output[index] for index in range(len(issues))], decisions, updated_metrics, warnings, model_usages
        )

    @staticmethod
    def _verification_input_key(request, unit, state):
        from app.services.fingerprints import stable_hash
        from app.review.diff_base import review_diff_base
        payload = request.model_dump(mode="json")
        for field in ("id", "review_unit_id", "source_issue_ids", "source_review_unit_ids"):
            payload["issue"].pop(field, None)
        payload["budget"].pop("remaining_calls", None)
        for snippet in payload["readonly_context"]:
            snippet.pop("review_unit_id", None)
        return stable_hash({"request": payload, "model": state.get("model"),
            "head": state.get("head_sha"), "base": review_diff_base(state),
            "scope": unit.model_dump(mode="json", include={"primary_files", "related_files", "diff_hunk_ids", "rule_ids"})})

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
        diff_complete = len(unit_diff) <= 60_000
        if not diff_complete:
            # 不能在 JSON/代码正文中间截断；只保留完整 hunk，并明确输入不完整。
            files = json.loads(unit_diff)
            bounded = []
            for file in files:
                selected = {**file, "hunks": []}
                if len(json.dumps([*bounded, selected], ensure_ascii=False)) > 60_000:
                    continue
                bounded.append(selected)
                for hunk in file["hunks"]:
                    selected["hunks"].append(hunk)
                    if len(json.dumps(bounded, ensure_ascii=False)) > 60_000:
                        selected["hunks"].pop()
            unit_diff = json.dumps(bounded, ensure_ascii=False)
        cited_text = issue.model_dump_json() + unit_diff
        known_paths = sorted({raw["path"] for raw in state.get("file_index") or []
            if isinstance(raw.get("path"), str) and raw["path"] in cited_text})[:50]
        return IssueVerificationRequest(
            issue=issue,
            primary_evidence=issue.primary_evidence,
            supporting_evidence=issue.supporting_evidence,
            unit_diff=unit_diff,
            readonly_context=context,
            known_existing_paths=known_paths,
            evidence_complete=complete and diff_complete,
            applicable_rules=unit.rule_ids,
            budget=IssueVerificationBudget(
                remaining_calls=(current_run_budget().available()["calls"] if current_run_budget() is not None
                                 else self.max_calls_per_unit - used_calls),
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
        files = json.loads(planner.normalized_unit_diff(unit, by_path, hunk_ids))
        for file in files:
            for hunk in file["hunks"]:
                if hunk.get("lines"):
                    hunk.pop("added_lines", None)
                    hunk.pop("removed_lines", None)
        return json.dumps(files, ensure_ascii=False)

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
            if anchor.branch_claim:
                from app.evidence.branch import counterevidence_branch_error
                if not anchor.file_path.endswith(".py") or not any(
                    counterevidence_branch_error(text, anchor) is None for candidate_side in sides
                    for text in sources.get((anchor.file_path, candidate_side), [])):
                    raise ValueError("verifier_branch_claim_unverified")

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
                branch_claim=anchor.branch_claim,
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
