"""一轮有界协调：模型提议，服务端校验，独立补查与候选批次验证。"""

import asyncio
import json
from typing import Any

from app.agents.providers import LLMProviderError
from app.models.review import (
    ChangedFile, CrossUnitCoordinationPlan, CrossUnitFollowupResult,
    CrossUnitRiskAssessment, ExecutionBudget, IssueStatus, ReviewUnit,
    ReviewUnitResult, ReviewUnitStatus, ReviewIssueInput, EvidenceAnchorInput,
)
from app.review.tool_scope import is_sensitive_repository_change
from app.services.model_usage import unpack_model_call
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.review_planner import DeterministicReviewPlanner
from app.services.unit_review_summary import build_record_input
from app.services.coordination_runtime import (
    CoordinationRuntime, coordination_fingerprint, estimate_request, budget_rejection,
)
from app.services.fingerprints import stable_hash


class SharedBudgetProvider:
    """协调、所有补查和 verifier 共用调用/估算 token 预算；失败也计费。"""

    def __init__(self, provider: Any, budget: ExecutionBudget) -> None:
        self.provider = provider
        self._budget = budget
        self.runtime: CoordinationRuntime | None = None
        self.holdback: dict | None = None

    @property
    def budget(self) -> ExecutionBudget:
        return self.runtime.budget if self.runtime else self._budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def _call(self, name: str, args: tuple, output_tokens: int) -> Any:
        if self.runtime is not None:
            return await self.runtime.call(self.provider, name, args, output_tokens, holdback=self.holdback)
        estimate = estimate_request(self.provider, name, args, output_tokens)["estimate"]
        held = self.holdback or {}
        if not self.budget.can_consume(model_calls=1 + held.get("model_calls", 0),
                                       token_usage=estimate + held.get("token_usage", 0)):
            raise LLMProviderError("cross_unit_shared_budget_exhausted: " + json.dumps(
                budget_rejection(self.budget, name, estimate, held)))
        self._budget = self.budget.consume(model_calls=1, token_usage=estimate)
        return await getattr(self.provider, name)(*args)

    async def coordinate_cross_units(self, *args: Any) -> Any:
        return await self._call("coordinate_cross_units", args, 4096)

    async def decide(self, *args: Any) -> Any:
        return await self._call("decide", args, 1200)

    async def review_unit(self, *args: Any) -> Any:
        return await self._call("review_unit", args, 4096)

    async def verify_issue(self, *args: Any) -> Any:
        return await self._call("verify_issue", args, args[0].budget.max_output_tokens)


class CrossUnitCoordinationService:
    def __init__(self, provider: Any, *, timeout_seconds: int = 660,
                 budget: ExecutionBudget | None = None, repository: Any = None,
                 lease: dict | None = None, progress: Any = None) -> None:
        self.provider = SharedBudgetProvider(provider, budget or ExecutionBudget(
            max_model_calls=16, max_token_usage=120_000, max_patch_attempts=0,
        ))
        self.timeout_seconds = timeout_seconds
        self.repository, self.lease, self.progress = repository, lease, progress
        self.runtime: CoordinationRuntime | None = None

    @staticmethod
    def catalog(state: dict[str, Any]) -> dict[str, Any]:
        units = [ReviewUnit.model_validate(raw) for raw in state.get("review_units") or []]
        results = {raw["review_unit_id"]: ReviewUnitResult.model_validate(raw)
                   for raw in state.get("review_unit_results") or []}
        changed = {raw["file_path"]: ChangedFile.model_validate(raw)
                   for raw in state.get("changed_files") or []}
        evidence: dict[str, dict] = {}
        summaries = []
        for unit in units:
            result = results.get(unit.id)
            if result is None:
                continue
            files = [changed[path].model_copy(update={"hunks": [
                hunk for index, hunk in enumerate(changed[path].hunks)
                if not unit.diff_hunk_ids or DeterministicReviewPlanner.hunk_id(
                    path, index, hunk.model_dump(mode="json")) in unit.diff_hunk_ids
            ]}) for path in unit.primary_files]
            records = build_record_input(
                files, [item.model_dump(mode="json") for item in result.context_snippets],
                None, str(state.get("head_sha") or ""), str(state.get("base_sha") or ""),
            )
            trusted = {item.id: item.model_dump(mode="json") for item in result.review_summary.evidence}
            for item in records["evidence"]:
                reference = {key: value for key, value in item.items() if key != "content"}
                if trusted.get(item["id"]) == reference:
                    # 协调只规划补查，不以重复的仓库全文直接确认 Issue。
                    evidence[item["id"]] = reference
            summaries.append({"unit_id": unit.id, "status": result.status.value,
                              "summary": {
                                  "status": result.review_summary.status,
                                  "record": (result.review_summary.record.model_dump(mode="json")
                                             if result.review_summary.record else None),
                                  "reason": result.review_summary.reason,
                                  "evidence_ids": [key for key in trusted if key in evidence],
                              }})
        return {
            "risk": state["cross_unit_risk"],
            "units": [item.model_dump(mode="json") for item in units],
            "summaries": summaries, "evidence": list(evidence.values()),
        }

    @staticmethod
    def validate_plan(plan: CrossUnitCoordinationPlan, payload: dict[str, Any],
                      budget: ExecutionBudget | None = None) -> None:
        units = {item["id"]: ReviewUnit.model_validate(item) for item in payload["units"]}
        evidence = {item["id"]: item for item in payload["evidence"]}
        risk = CrossUnitRiskAssessment.model_validate(payload["risk"])
        relationships = {item.id for item in risk.relationships}
        if set(plan.relationship_ids) - relationships or set(plan.evidence_ids) - evidence.keys():
            raise ValueError("unknown relationship or stale evidence reference")
        if risk.decision == "required" and plan.decision != "required":
            raise ValueError("required risk cannot be downgraded")
        if plan.decision == "skip":
            # 缺记录、缺索引、覆盖缺口不能被一次模型判断解释为无风险。
            if risk.index_status != "available" or any(
                item.code != "relationship_unknown" for item in risk.reasons
            ) or not risk.relationships or set(plan.relationship_ids) != relationships:
                raise ValueError("insufficient coverage to skip uncertain risk")
            paths = {evidence[key]["file_path"] for key in plan.evidence_ids}
            if any({item.source_file, item.target_file} - paths for item in risk.relationships):
                raise ValueError("skip requires evidence for both ends of every relationship")
        if plan.decision != "required" and plan.followups:
            raise ValueError("followups require a required decision")
        if budget is not None and not budget.can_consume(
            model_calls=5 * len(plan.followups), token_usage=18_000 * len(plan.followups),
        ):
            raise ValueError("insufficient shared budget for proposed followups and verification")
        # 这是计划准入门槛，不是实际调用预留；每次请求仍按真实提示和实测补记控制。
        seen: set[tuple] = set()
        for request in plan.followups:
            if len(set(request.unit_ids)) < 2 or set(request.unit_ids) - units.keys():
                raise ValueError("followup must name at least two known Units")
            selected = [units[key] for key in request.unit_ids]
            primary = {path for unit in selected for path in unit.primary_files}
            readable = primary | {path for unit in selected for path in unit.related_files}
            if set(request.primary_files) - primary:
                raise ValueError("followup primary files exceed selected Units")
            if any(is_sensitive_repository_change(path) for path in readable):
                raise ValueError("sensitive followup scope")
            if not request.evidence_ids or set(request.evidence_ids) - evidence.keys():
                raise ValueError("followup requires current evidence references")
            if any(evidence[key]["file_path"] not in readable for key in request.evidence_ids):
                raise ValueError("followup evidence outside selected Units")
            identity = CrossUnitCoordinationService.followup_identity(request)
            if identity in seen:
                raise ValueError("duplicate followup task")
            seen.add(identity)

    @staticmethod
    def followup_identity(request) -> tuple:
        return (tuple(sorted(set(request.unit_ids))), tuple(sorted(set(request.primary_files))),
                tuple(sorted(set(request.evidence_ids))), request.question.strip(),
                request.counterevidence_goal.strip(), request.stop_condition.strip())

    @classmethod
    def normalize_plan(cls, plan, payload, budget):
        plan = CrossUnitCoordinationPlan.model_validate(plan.model_dump(mode="json"))
        unique, seen, warnings = [], {}, []
        for request in plan.followups:
            # 去重前逐项核验，不能因重复而绕过路径或引用校验。
            cls.validate_plan(plan.model_copy(update={"followups": [request]}), payload)
            identity = cls.followup_identity(request)
            if identity in seen:
                warnings.append(f"完全重复补查已合并：{request.id} -> {seen[identity]}")
            else:
                unique.append(request)
                seen[identity] = request.id
        normalized = plan.model_copy(update={"followups": unique})
        cls.validate_plan(normalized, payload, budget)
        return normalized, warnings

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        fingerprint = coordination_fingerprint(state)
        existing = state.get("coordination_plan")
        if (existing and existing.get("runtime_fingerprint") == fingerprint
                and existing.get("status") in {"completed", "unresolved", "failed", "cancelled"}):
            return {}
        risk = CrossUnitRiskAssessment.model_validate(state["cross_unit_risk"])
        if risk.decision == "skip":
            return {}
        self.runtime = CoordinationRuntime(str(state.get("task_id") or ""), fingerprint,
            self.provider.budget, self.repository, self.lease, self.progress)
        await self.runtime.load()
        self.provider.runtime = self.runtime
        if self.runtime.data["output"] is not None:
            self.runtime.data["cache_hits"] += 1
            await self.runtime.persist()
            return {**self.runtime.data["output"], **self.runtime.public_state()}
        if self.runtime.data["revision"] == -1:
            self.runtime.data["base_metrics"] = dict(state.get("issue_metrics") or {})
        await self.runtime.persist()
        plan = CrossUnitCoordinationPlan(decision=risk.decision)
        usages: list[dict] = []
        try:
            async with asyncio.timeout(self.timeout_seconds):
                payload = self.catalog(state)
                if len(json.dumps(payload, ensure_ascii=False)) > 48_000:
                    raise ValueError("coordination_catalog_exceeds_48000_chars")
                try:
                    saved_plan = self.runtime.data["plan"]
                    if saved_plan and saved_plan["status"] == "validated":
                        plan = CrossUnitCoordinationPlan.model_validate(saved_plan)
                        self.validate_plan(plan, payload)
                    else:
                        async with asyncio.timeout(60):
                            raw = await self.provider.coordinate_cross_units(payload, state.get("model"))
                        proposed, usage = unpack_model_call(raw)
                        if usage is not None:
                            usages.append(usage.model_dump(mode="json"))
                        plan = CrossUnitCoordinationPlan.model_validate(proposed)
                        plan, plan_warnings = self.normalize_plan(plan, payload, self.provider.budget)
                        self.runtime.data["plan_warnings"] = plan_warnings
                except Exception as exc:
                    usage = getattr(exc, "usage", None)
                    if usage is not None:
                        usages.append(usage.model_dump(mode="json"))
                    raise
                plan = plan.model_copy(update={"status": "validated"})
                self.runtime.data["plan"] = plan.model_dump(mode="json")
                await self.runtime.persist()
                result = await self.execute_followups(plan, state)
                status = "completed" if plan.decision == "skip" or (
                    result.get("followup_results") and all(
                        item["outcome"] in {"candidate_found", "refuted"}
                        for item in result["followup_results"]
                    ) and not plan.unresolved_questions
                ) and not any(issue["status"] in {"candidate", "evidence_resolved", "needs_human"}
                              for issue in result.get("review_issues") or []
                              if issue["review_unit_id"].startswith("followup-")) else "unresolved"
                plan = plan.model_copy(update={"status": status})
                result["model_usages"] = [*(state.get("model_usages") or []), *usages,
                                          *result.get("model_usages", [])]
        except asyncio.CancelledError:
            self.runtime.data["status"] = "interrupted"
            self.runtime.data["plan"] = plan.model_dump(mode="json")
            # 调用已预留的预算不退还；取消状态不会转成成功结果。
            try:
                await asyncio.shield(self.runtime.persist())
            except asyncio.CancelledError:
                pass
            raise
        except Exception as exc:
            plan = plan.model_copy(update={"status": "failed",
                                          "reason": f"{type(exc).__name__}: {exc}"[:1000]})
            result = {**self._aggregate_output(state), "warnings": [*(state.get("warnings") or []),
                                   f"跨 Unit 协调未完成：{plan.reason}"],
                      "model_usages": [*(state.get("model_usages") or []), *usages]}
        plan = plan.model_copy(update={"execution_budget": self.provider.budget})
        result["coordination_plan"] = plan.model_dump(mode="json")
        result["cross_unit_risk"] = risk.model_copy(update={
            "execution_status": plan.status if plan.status in {"completed", "failed"} else "unresolved",
            "non_execution_reason": None if plan.status == "completed" else plan.reason,
        }).model_dump(mode="json")
        if plan.status == "unresolved":
            result["warnings"] = list(dict.fromkeys([*(result.get("warnings") or []),
                "跨 Unit 补查仍有未决项，请结合补查记录确认。"] ))
        self.runtime.data["plan"] = plan.model_dump(mode="json")
        self.runtime.data["status"] = plan.status
        # 账本保存派生输出，重复恢复时直接返回，指标不再次累加。
        known = {usage["id"]: usage for usage in result.get("model_usages") or []}
        for call in self.runtime.data["calls"].values():
            if call.get("usage"):
                known.setdefault(call["usage"]["id"], call["usage"])
        result["model_usages"] = list(known.values())
        self.runtime.data["output"] = result
        await self.runtime.persist()
        result.update(self.runtime.public_state())
        return result

    def _aggregate_output(self, state: dict) -> dict:
        batches = list(self.runtime.data["batches"].values()) if self.runtime else []
        metrics = dict(self.runtime.data["base_metrics"] if self.runtime else state.get("issue_metrics") or {})
        for batch in batches:
            for key, value in batch.get("issue_metrics", {}).items():
                metrics[key] = metrics.get(key, 0) + value
        issues = {item["id"]: item for item in state.get("review_issues") or []}
        for batch in batches:
            issues.update({item["id"]: item for item in batch.get("review_issues") or []})
        return {"review_issues": list(issues.values()), "issue_metrics": metrics,
            "followup_results": list(self.runtime.data["followups"].values()) if self.runtime else [],
            "model_usages": [usage for batch in batches for usage in batch.get("model_usages") or []],
            "deterministic_issue_checks": [*(state.get("deterministic_issue_checks") or []),
                *(item for batch in batches for item in batch.get("deterministic_issue_checks") or [])],
            "issue_verifications": [*(state.get("issue_verifications") or []),
                *(item for batch in batches for item in batch.get("issue_verifications") or [])],
            "warnings": list(dict.fromkeys([*(state.get("warnings") or []),
                *(self.runtime.data.get("plan_warnings") or [] if self.runtime else []),
                *(item for batch in batches for item in batch.get("warnings") or [])])),
            "context_snippets": [*(state.get("context_snippets") or []),
                *(item for raw in (self.runtime.data["followups"].values() if self.runtime else [])
                  for item in (raw.get("unit_result") or {}).get("context_snippets") or [])]}

    async def execute_followups(self, plan: CrossUnitCoordinationPlan,
                               state: dict[str, Any]) -> dict[str, Any]:
        from app.graph.nodes.resolve_evidence import resolve_evidence_node
        from app.graph.nodes.issue_validation import issue_policy_node, issue_verifier_node
        from app.services.issue_verifier import IssueVerifierService
        from app.review.issue_audit import audit_issue

        by_id = {raw["id"]: ReviewUnit.model_validate(raw) for raw in state["review_units"]}
        # 补查仅由专属账本恢复，显式禁止继承父图的 Unit checkpoint/cache。
        executor = ReviewUnitExecutor(self.provider, concurrency=1, timeout_seconds=60, checkpointer=False)
        usages = []
        for request in plan.followups:
            selected = [by_id[key] for key in request.unit_ids]
            digest = stable_hash({"purpose": "cross-unit-followup-v1",
                "runtime": self.runtime.fingerprint, "request": request.model_dump(mode="json")})
            if request.id in self.runtime.data["batches"]:
                self.runtime.data["cache_hits"] += 1
                continue
            readable = {path for unit in selected for path in [*unit.primary_files, *unit.related_files]}
            unit = ReviewUnit(
                id="followup-" + digest[:24], primary_files=request.primary_files,
                related_files=sorted(readable - set(request.primary_files)),
                diff_hunk_ids=list(dict.fromkeys(key for item in selected for key in item.diff_hunk_ids)),
                rule_ids=list(dict.fromkeys(key for item in selected for key in item.rule_ids)),
                risk_tags=["cross_module"], complexity="small", estimated_tokens=0,
                fingerprint=digest, grouping_reason="cross_unit_followup",
            )
            local = {**state, "cross_unit_followup": request.model_dump(mode="json"),
                     "cross_unit_followup_context": [
                         {**snippet, "review_unit_id": unit.id}
                         for raw in state.get("review_unit_results") or []
                         if raw["review_unit_id"] in request.unit_ids
                         for snippet in raw.get("context_snippets") or []
                         if snippet.get("file") in readable
                     ]}
            saved = self.runtime.data["followups"].get(request.id)
            allocations = self.runtime.data.setdefault("allocations", {})
            if request.id not in allocations:
                pending = sum(item.id not in self.runtime.data["batches"] for item in plan.followups)
                budget = self.provider.budget
                calls = max(0, budget.max_model_calls - budget.model_calls)
                tokens = max(0, budget.max_token_usage - budget.token_usage)
                share_calls, share_tokens = calls // max(1, pending), tokens // max(1, pending)
                allocations[request.id] = {
                    "followup_id": request.id, "future_calls": calls - share_calls,
                    "future_tokens": tokens - share_tokens,
                    "verification_calls": min(2, share_calls), "verification_tokens": share_tokens // 3,
                }
                await self.runtime.persist()
            allocation = allocations[request.id]
            if saved:
                self.runtime.data["cache_hits"] += 1
            self.provider.holdback = {
                "followup_id": request.id, "phase": "exploration",
                "model_calls": allocation["future_calls"] + allocation["verification_calls"],
                "token_usage": allocation["future_tokens"] + allocation["verification_tokens"],
            }
            try:
                result = (ReviewUnitResult.model_validate(saved["unit_result"]) if saved
                          else await executor.execute_unit(unit, local))
            finally:
                self.provider.holdback = None
            # 候选 ID 和生命周期由服务端重新赋值；补查不能冒充已确认 Issue。
            issues = []
            for index, issue in enumerate(result.issues[:20]):
                identity = f"{unit.id}-issue-{index}"
                raw_issue = issue.model_dump(mode="json", include=set(ReviewIssueInput.model_fields))
                raw_issue["primary_evidence"] = issue.primary_evidence.model_dump(
                    mode="json", include=set(EvidenceAnchorInput.model_fields))
                raw_issue["supporting_evidence"] = [anchor.model_dump(
                    mode="json", include=set(EvidenceAnchorInput.model_fields))
                    for anchor in issue.supporting_evidence]
                raw_issue["auto_fix_eligible"] = False
                candidate = ReviewIssueInput.model_validate(raw_issue).to_issue(unit.id)
                candidate = candidate.model_copy(update={"id": identity, "status": IssueStatus.candidate,
                    "source_issue_ids": [identity], "source_review_unit_ids": request.unit_ids})
                audit_issue("identity", issue, reason="followup_server_identity", canonical_id=identity)
                audit_issue("identity_assigned", candidate, reason="followup_server_identity")
                issues.append(candidate)
            result = result.model_copy(update={"issues": issues})
            usages.extend(item.model_dump(mode="json") for item in result.model_usages)
            # 零候选不是反证；只有目标检查完成且具有证据的 refuted 假设才是反证。
            record = result.review_summary.record
            refuted = bool(record and record.hypothesis_checks and
                           all(item.status == "refuted" and item.evidence_ids for item in record.hypothesis_checks)
                           and not record.unresolved_questions
                           and all(item.status == "checked" for item in record.target_checks))
            if refuted:
                refs = {item.id: item.file_path for item in result.review_summary.evidence}
                paths = {refs[key] for check in record.hypothesis_checks for key in check.evidence_ids}
                refuted = all(set(item.primary_files) & paths for item in selected)
            outcome = "failed" if result.status != ReviewUnitStatus.completed else (
                "candidate_found" if issues else "refuted" if refuted else "unresolved")
            followup = CrossUnitFollowupResult(
                request_id=request.id, outcome=outcome, unit_result=result,
                fingerprint=digest,
                evidence_ids=request.evidence_ids, reason=result.error or (
                    "新增候选将接受证据、策略与独立验证" if issues else
                    "已找到相关 Unit 的反证且完成目标检查" if refuted else
                    "未发现候选；没有充分反证时保留未决"
                ),
            )
            self.runtime.data["followups"][request.id] = followup.model_dump(mode="json")
            await self.runtime.persist()
            if not issues:
                self.runtime.data["batches"][request.id] = {"review_issues": [], "issue_metrics": {}}
                followup = followup.model_copy(update={"validation_status": "completed"})
                self.runtime.data["followups"][request.id] = followup.model_dump(mode="json")
                await self.runtime.persist()
                continue
            # 只处理新增批次，不重验或重新计数原有候选，不写入普通 Unit 结果。
            batch = {**state, "review_units": [unit.model_dump(mode="json")],
                     "review_unit_results": [], "review_issues": [item.model_dump(mode="json") for item in issues],
                     "model_usages": [], "issue_metrics": {}, "step_progress": [],
                     "context_snippets": [item.model_dump(mode="json") for item in result.context_snippets],
                     "_issue_verifier_service": IssueVerifierService(
                         self.provider, enabled=True, fail_mode="needs_human", max_calls_per_unit=2,
                     )}
            self.provider.holdback = {
                "followup_id": request.id, "phase": "verification",
                "model_calls": allocation["future_calls"], "token_usage": allocation["future_tokens"],
            }
            try:
                for node in (resolve_evidence_node, issue_policy_node, issue_verifier_node):
                    batch.update(await node(batch))
            finally:
                self.provider.holdback = None
            self.runtime.data["batches"][request.id] = {key: batch.get(key) for key in (
                "review_issues", "issue_metrics", "model_usages", "deterministic_issue_checks",
                "issue_verifications", "warnings")}
            result = result.model_copy(update={"issues": [type(issues[0]).model_validate(raw)
                                                           for raw in batch["review_issues"]]})
            followup = followup.model_copy(update={"unit_result": result, "validation_status": "completed"})
            self.runtime.data["followups"][request.id] = followup.model_dump(mode="json")
            await self.runtime.persist()
        aggregated = self._aggregate_output(state)
        aggregated["model_usages"].extend(usages)
        return aggregated
