"""一轮有界协调：模型提议，服务端校验，独立补查与候选批次验证。"""

import asyncio
import json
import re
from typing import Any

from app.agents.providers import LLMProviderError
from app.review.diff_base import review_diff_base
from app.models.review import (
    ChangedFile, CrossUnitCoordinationPlan, CrossUnitFollowupResult,
    CrossUnitRiskAssessment, ExecutionBudget, IssueStatus, ReviewUnit,
    ReviewUnitResult, ReviewUnitStatus, ReviewIssueInput, EvidenceAnchorInput, ReviewIssue, IssueMetrics,
)
from app.review.tool_scope import is_sensitive_repository_change
from app.review.unit_completion import is_review_unit_complete
from app.services.model_usage import unpack_model_call
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.review_planner import DeterministicReviewPlanner
from app.services.unit_review_summary import build_record_input
from app.services.coordination_runtime import (
    CoordinationRuntime, coordination_fingerprint, estimate_request, budget_rejection,
)
from app.services.fingerprints import stable_hash
from app.services.coordination_catalog import catalog_batches
from app.services.review_run_budget import current_run_budget
from app.services.coordination_questions import bind_plan_questions, refresh_plan_questions


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
        if current_run_budget() is not None:
            return await getattr(self.provider, name)(*args)
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

    async def repair_cross_unit_plan(self, *args: Any) -> Any:
        return await self._call("repair_cross_unit_plan", args, 4096)

    async def decide(self, *args: Any) -> Any:
        return await self._call("decide", args, 1200)

    async def review_unit(self, *args: Any) -> Any:
        return await self._call("review_unit", args, 4096)

    async def review_step(self, *args: Any) -> Any:
        return await self._call("review_step", args, 4096)

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
        repository_files = {item["path"] for item in state.get("file_index") or [] if item.get("path")}
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
                None, str(state.get("head_sha") or ""),
                review_diff_base(state),
            )
            if result.diff_batches:
                from app.services.unit_worksets import restore_unit_workset_files

                for batch in result.diff_batches:
                    if batch.result is None:
                        continue
                    try:
                        batch_files = restore_unit_workset_files(unit, files, batch)
                        batch_records = build_record_input(batch_files, [], None,
                            str(state.get("head_sha") or ""), review_diff_base(state))
                    except (KeyError, TypeError, ValueError):
                        continue
                    records["evidence"].extend(batch_records["evidence"])
            trusted = {item.id: item.model_dump(mode="json") for item in result.review_summary.evidence}
            readable = repository_files | set(unit.primary_files) | set(unit.related_files)
            restored_ids = []
            for item in records["evidence"]:
                reference = {key: value for key, value in item.items() if key != "content"}
                if (trusted.get(item["id"]) == reference and item["file_path"] in readable
                        and not is_sensitive_repository_change(item["file_path"])):
                    # 协调只规划补查，不以重复的仓库全文直接确认 Issue。
                    evidence[item["id"]] = reference
                    restored_ids.append(item["id"])
            summaries.append({"unit_id": unit.id, "status": result.status.value,
                              "summary": {
                                  "status": result.review_summary.status,
                                  "record": (result.review_summary.record.model_dump(mode="json")
                                             if result.review_summary.record else None),
                                  "last_valid_record": (result.review_summary.last_valid_record.model_dump(mode="json")
                                                        if result.review_summary.last_valid_record and not result.review_summary.record else None),
                                  "last_valid_snapshot": result.review_summary.last_valid_snapshot,
                                  "latest_attempt_status": result.review_summary.latest_attempt_status,
                                  "latest_attempt_reason": result.review_summary.latest_attempt_reason,
                                  "reason": result.review_summary.reason,
                                  "strict_complete": is_review_unit_complete(result),
                                  "input_coverage": (result.input_coverage.model_dump(mode="json", exclude={"omitted_plan"})
                                                     if result.input_coverage else None),
                                  "evidence_ids": restored_ids,
                              }})
        from app.services.followup_contracts import origin_obligations
        return {
            "risk": state["cross_unit_risk"],
            "units": [item.model_dump(mode="json") for item in units],
            "summaries": summaries, "evidence": list(evidence.values()),
            "origin_obligations": origin_obligations(state, set(evidence)),
            "verified_scopes": CrossUnitCoordinationService._verified_scopes(results, units, evidence),
        }

    @staticmethod
    def _verified_scopes(results, units, evidence):
        """只暴露已规划、已反证且具有当前跨文件引用的限定结论，不从局部完成补造。"""
        scopes = []
        for unit in units:
            result = results.get(unit.id)
            if result is None or result.plan is None or not is_review_unit_complete(result):
                continue
            hypotheses = {item.id: item for item in result.plan.risk_hypotheses}
            for check in result.review_summary.record.hypothesis_checks:
                hypothesis = hypotheses.get(check.hypothesis_id)
                if hypothesis is None or check.status != "refuted" or not check.evidence_ids:
                    continue
                files = set(hypothesis.affected_files)
                owners = {item.id for item in units if files & set(item.primary_files)}
                if (len(files) < 2 or len(owners) < 2 or set(check.evidence_ids) - evidence.keys()
                        or files - {evidence[key]["file_path"] for key in check.evidence_ids}):
                    continue
                scopes.append({"unit_id": unit.id, "hypothesis_id": hypothesis.id,
                    "description": hypothesis.description, "completion_criteria": hypothesis.completion_criteria,
                    "files": sorted(files), "unit_ids": sorted(owners | {unit.id}),
                    "evidence_ids": check.evidence_ids, "reason": check.reason})
        return scopes

    @staticmethod
    def _validate_empty_relationship_skip(plan, payload, units, evidence, risk):
        summaries = {item["unit_id"]: item["summary"] for item in payload.get("summaries", [])}
        if (not units or payload.get("origin_obligations") or any(
                not summaries.get(key, {}).get("strict_complete") for key in units)):
            raise ValueError("empty relationship skip requires current complete Units without origin obligations")
        paths = {evidence[key]["file_path"] for key in plan.evidence_ids}
        primary = {path for unit in units.values() for path in unit.primary_files}
        if primary - paths:
            raise ValueError("empty relationship skip requires current evidence for every primary file")
        scopes = [set(item["files"]) for item in payload.get("verified_scopes", [])
            if set(item["unit_ids"]) <= units.keys() and item["evidence_ids"]
            and set(item["evidence_ids"]) <= set(plan.evidence_ids)]
        required = []
        for reason in risk.reasons:
            if set(reason.unit_ids) - units.keys():
                raise ValueError("empty relationship skip contains unknown risk scope")
            required.append(set(reason.files) | {path for key in reason.unit_ids for path in units[key].primary_files})
        # 无范围的理由不能被空集包含关系自动放行；也不拼接不相干的局部反证。
        if any(not any((files or primary) <= scope for scope in scopes) for files in (required or [primary])):
            raise ValueError("empty relationship skip requires verified cross-file scope for every risk reason")

    @staticmethod
    def _mentioned_files(request, payload):
        text = "\n".join([request.question, request.counterevidence_goal, request.stop_condition])
        return {path for unit in payload["units"] for path in [*unit["primary_files"], *unit.get("related_files", [])]
            if re.search(r"(?<![A-Za-z0-9_./\\-])" + re.escape(path) + r"(?![A-Za-z0-9_./\\-])", text)}

    @staticmethod
    def _followup_readable(request, payload):
        units = {item["id"]: item for item in payload["units"]}
        readable = {path for key in request.unit_ids if key in units
            for path in [*units[key]["primary_files"], *units[key].get("related_files", [])]}
        scoped_ids = {identity for item in payload.get("summaries", []) if item["unit_id"] in request.unit_ids
            for identity in item["summary"].get("evidence_ids", [])}
        readable.update(item["file_path"] for item in payload["evidence"] if item["id"] in scoped_ids)
        return readable

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
            ) or set(plan.relationship_ids) != relationships:
                raise ValueError("insufficient coverage to skip uncertain risk")
            if plan.unresolved_questions or any(item.status != "resolved" for item in plan.question_states):
                raise ValueError("skip cannot retain unresolved planning questions")
            if not risk.relationships:
                CrossUnitCoordinationService._validate_empty_relationship_skip(plan, payload, units, evidence, risk)
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
            if any(index < 0 or index >= len(plan.unresolved_questions) for index in request.planning_question_indices):
                raise ValueError("unknown planning question index")
            origins = {item["id"]: item for item in payload.get("origin_obligations", [])}
            if any(identity not in origins or origins[identity]["unit_id"] not in request.unit_ids
                   for identity in request.origin_obligation_ids):
                raise ValueError("unknown or out-of-scope origin obligation")
            if len(set(request.unit_ids)) < 2 or set(request.unit_ids) - units.keys():
                raise ValueError("followup must name at least two known Units")
            selected = [units[key] for key in request.unit_ids]
            primary = {path for unit in selected for path in unit.primary_files}
            readable = CrossUnitCoordinationService._followup_readable(request, payload)
            mentioned = CrossUnitCoordinationService._mentioned_files(request, payload)
            if (set(request.supporting_files) | set(request.verification_files) | mentioned) - readable:
                raise ValueError("followup supporting files outside selected Units; include their owning Unit")
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
                request.counterevidence_goal.strip(), request.stop_condition.strip(), tuple(sorted(set(request.origin_obligation_ids))),
                tuple(sorted(set(request.supporting_files))), tuple(sorted(set(request.verification_files))),
                tuple(sorted(set(request.tracking_queries))))

    @classmethod
    def normalize_plan(cls, plan, payload, budget=None):
        plan = CrossUnitCoordinationPlan.model_validate(plan.model_dump(mode="json"))
        plan = bind_plan_questions(plan)
        aliases = {}
        unique, seen, warnings = [], {}, []
        for request in plan.followups:
            # 仅补全目录内已知主文件的拥有者；未知路径仍由严格范围门拒绝。
            mentioned = cls._mentioned_files(request, payload)
            needed = set(request.supporting_files) | set(request.verification_files) | mentioned
            missing = needed - cls._followup_readable(request, payload)
            owners = {unit["id"] for unit in payload["units"] if missing & set(unit["primary_files"])}
            if owners:
                request.unit_ids = list(dict.fromkeys([*request.unit_ids, *sorted(owners)]))
                request.supporting_files = sorted(needed - set(request.primary_files))
                warnings.append(f"补查 {request.id} 纳入必要支持文件所属 Unit：{','.join(sorted(owners))}")
            elif mentioned:
                request.supporting_files = sorted(needed - set(request.primary_files))
            if request.verification_files or mentioned:
                request.verification_files = sorted(set(request.verification_files) | mentioned)
            # 重新经过字段长度与路径校验，不能靠原地赋值绕过 schema。
            request = type(request).model_validate(request.model_dump(mode="json"))
            # 去重前逐项核验，不能因重复而绕过路径或引用校验。
            cls.validate_plan(plan.model_copy(update={"followups": [request]}), payload)
            identity = cls.followup_identity(request)
            if identity in seen:
                aliases[request.id] = seen[identity]
                warnings.append(f"完全重复补查已合并：{request.id} -> {seen[identity]}")
            else:
                unique.append(request)
                seen[identity] = request.id
        normalized = plan.model_copy(update={"followups": unique, "question_states": [
            item.model_copy(update={"followup_ids": list(dict.fromkeys(aliases.get(key, key) for key in item.followup_ids))})
            for item in plan.question_states]})
        cls.validate_plan(normalized, payload, budget)
        return normalized, warnings

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        fingerprint = coordination_fingerprint(state)
        existing = state.get("coordination_plan")
        if (existing and existing.get("runtime_fingerprint") == fingerprint
                and existing.get("status") in {"completed", "unresolved", "failed", "cancelled"}
                and not existing.get("reason", "").startswith("deferred_budget_not_executed")):
            return {}
        risk = CrossUnitRiskAssessment.model_validate(state["cross_unit_risk"])
        if risk.decision == "skip":
            return {}
        self.runtime = CoordinationRuntime(str(state.get("task_id") or ""), fingerprint,
            self.provider.budget, self.repository, self.lease, self.progress)
        await self.runtime.load()
        self.provider.runtime = self.runtime
        if self.runtime.data["output"] is not None:
            if self.runtime.data.get("deferred_signature") != self.runtime.budget_signature():
                if self.runtime.data.get("deferred_signature") is not None:
                    self.runtime.data["output"] = None
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
                try:
                    saved_plan = self.runtime.data["plan"]
                    if saved_plan and saved_plan["status"] == "validated":
                        plan = CrossUnitCoordinationPlan.model_validate(saved_plan)
                        self.validate_plan(plan, payload)
                    else:
                        while True:
                            before = self.runtime.budget_signature()
                            try:
                                plan = await self.plan_catalog(payload, state.get("model"), usages)
                            except Exception:
                                if self._catalog_deferred() and await self._budget_progress(before):
                                    continue
                                raise
                            if not self._catalog_deferred() or not await self._budget_progress(before):
                                break
                except Exception as exc:
                    usage = getattr(exc, "usage", None)
                    if usage is not None:
                        usages.append(usage.model_dump(mode="json"))
                    raise
                plan = plan.model_copy(update={"status": "validated"})
                self.runtime.data["plan"] = plan.model_dump(mode="json")
                await self.runtime.persist()
                result = await self.execute_followups(plan, state)
                plan = refresh_plan_questions(plan, result.get("followup_results", []), state)
                status = "completed" if not any(
                    item.status != "validated" for item in plan.catalog_batches
                ) and (plan.decision == "skip" or (
                    result.get("followup_results") and all(
                        item["outcome"] in {"candidate_found", "refuted"}
                        for item in result["followup_results"]
                    ) and not plan.unresolved_questions
                ) and not any(issue["status"] in {"candidate", "evidence_resolved", "unresolved", "needs_human"}
                              for issue in result.get("review_issues") or []
                              if issue["review_unit_id"].startswith("followup-"))) else "unresolved"
                plan = plan.model_copy(update={"status": status})
                if self._pending_budget_work():
                    plan = plan.model_copy(update={"status": "unresolved",
                        "reason": "deferred_budget_not_executed: coordination"})
                if self.provider.budget.token_usage > self.provider.budget.max_token_usage:
                    plan = plan.model_copy(update={"status": "unresolved", "reason": "cross_unit_budget_overrun",
                        "unresolved_questions": [*plan.unresolved_questions, "实际用量超过共享预留预算，需核对账本。"]})
                result["model_usages"] = [*(state.get("model_usages") or []), *usages,
                                          *result.get("model_usages", [])]
        except asyncio.CancelledError:
            plan = CrossUnitCoordinationPlan.model_validate(self.runtime.data["plan"] or plan)
            self.runtime.data["status"] = "interrupted"
            self.runtime.data["plan"] = plan.model_dump(mode="json")
            # 调用已预留的预算不退还；取消状态不会转成成功结果。
            try:
                await asyncio.shield(self.runtime.persist())
            except asyncio.CancelledError:
                pass
            raise
        except Exception as exc:
            plan = CrossUnitCoordinationPlan.model_validate(self.runtime.data["plan"] or plan)
            deferred = self._catalog_deferred()
            plan = plan.model_copy(update={"status": "unresolved" if deferred else "failed",
                "reason": ("deferred_budget_not_executed: catalog" if deferred
                           else self._planning_failure_reason(exc))})
            result = {**self._aggregate_output(state), "warnings": [*(state.get("warnings") or []),
                                   f"跨 Unit 协调未完成：{plan.reason}"],
                      "model_usages": [*(state.get("model_usages") or []), *usages]}
            plan = refresh_plan_questions(plan, result.get("followup_results", []), state)
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
        self.runtime.data["deferred_signature"] = (self.runtime.budget_signature()
            if self._pending_budget_work() else None)
        await self.runtime.persist()
        result.update(self.runtime.public_state())
        return result

    def _catalog_deferred(self) -> bool:
        return any(item.get("deferred_budget") for item in self.runtime.data.get("catalog_batches", {}).values())

    def _pending_budget_work(self) -> bool:
        return self._catalog_deferred() or bool(self.runtime.data.get("deferred_followups")) or any(
            item.get("pending_verifications") for item in self.runtime.data["batches"].values())

    async def _budget_progress(self, before: str) -> bool:
        if self.runtime.budget_signature() != before:
            return True
        run = current_run_budget()
        if run is not None and run.in_flight_reserved:
            await run.wait_for_change(run.revision)
            return self.runtime.budget_signature() != before
        return False

    async def plan_catalog(self, payload: dict, model: str | None,
                           usages: list[dict]) -> CrossUnitCoordinationPlan:
        admission = getattr(self.provider, "coordination_request_admission", None)
        batches = catalog_batches(payload, (lambda item: admission(item, model)) if callable(admission) else None)
        saved = self.runtime.data.setdefault("catalog_batches", {})
        entries = [type(entry).model_validate(saved.get(entry.id, {}).get("coverage", entry))
                   for entry, _ in batches]
        aggregate = CrossUnitCoordinationPlan(decision=payload["risk"]["decision"], catalog_batches=entries)
        self.runtime.data["plan"] = aggregate.model_dump(mode="json")
        await self.runtime.persist()
        followups, identities, questions, relationships, evidence = [], set(), [], set(), set()
        proposals, warnings, question_states = [], [], {}
        for index, (entry, batch) in enumerate(batches):
            saved = self.runtime.data.setdefault("catalog_batches", {})
            cached = saved.get(entry.id)
            if cached and cached.get("deferred_budget") and cached.get("budget_signature") != self.runtime.budget_signature():
                saved.pop(entry.id)
            if entry.id in saved:
                raw = saved[entry.id]
                coverage = type(entry).model_validate(raw["coverage"])
                proposal = CrossUnitCoordinationPlan.model_validate(raw["proposal"]) if raw.get("proposal") else None
                self.runtime.data["cache_hits"] += 1
            else:
                coverage, proposal = entry, None
                if entry.status == "pending" and len(batch["units"]) < 2:
                    coverage = entry.model_copy(update={"status": "skipped",
                        "reason": "coordination_batch_insufficient_cross_unit_scope"})
                if coverage.status == "pending":
                    # 已接受的补查仍保留执行/独立验证空间，后续目录不能透支。
                    self.provider.holdback = ({"model_calls": 5 * len(followups),
                        "token_usage": 18_000 * len(followups), "phase": "catalog"}
                        if len(batches) > 1 else None)
                    try:
                        async with asyncio.timeout(60):
                            raw = await self._coordinate_with_output_repair(batch, model, usages)
                        proposed, usage = unpack_model_call(raw)
                        if usage is not None:
                            usages.append(usage.model_dump(mode="json"))
                        proposal = CrossUnitCoordinationPlan.model_validate(proposed)
                        proposal, notes = self.normalize_plan(proposal, batch)
                        warnings.extend(notes)
                        if len(batches) == 1:
                            self.validate_plan(proposal, batch, self.provider.budget)
                        coverage = entry.model_copy(update={"status": "validated"})
                    except Exception as exc:
                        usage = getattr(exc, "usage", None)
                        if usage is not None:
                            usages.append(usage.model_dump(mode="json"))
                        proposal = None
                        coverage = entry.model_copy(update={"status": "failed",
                            "reason": self._planning_failure_reason(exc)})
                        rejection = getattr(exc, "admission_rejection", None)
                        if (rejection and rejection.get("can_defer")
                                and getattr(exc, "coordination_error_code", None) != "coordination_repair_not_admitted"):
                            saved[entry.id] = {"deferred_budget": True,
                                "budget_signature": self.runtime.budget_signature()}
                    finally:
                        self.provider.holdback = None
                self.runtime.data["catalog_batches"][entry.id] = {**saved.get(entry.id, {}), "coverage": coverage.model_dump(mode="json"),
                                  "proposal": proposal.model_dump(mode="json") if proposal else None}
            if proposal is not None:
                self.validate_plan(proposal, batch)
                proposals.append(proposal)
                relationships.update(proposal.relationship_ids)
                evidence.update(proposal.evidence_ids)
                questions.extend(proposal.unresolved_questions)
                request_ids = {}
                for request in proposal.followups:
                    identity = self.followup_identity(request)
                    if identity in identities:
                        request_ids[request.id] = next(item.id for item in followups if self.followup_identity(item) == identity)
                        continue
                    if len(followups) >= 3 or not self.provider.budget.can_consume(
                        model_calls=5 * (len(followups) + 1), token_usage=18_000 * (len(followups) + 1),
                    ):
                        questions.append(f"补查未执行（数量或共享预算限制）：{request.question}")
                        warnings.append(f"目录批次 {entry.id} 提出的补查未获准执行：{request.question}")
                        request_ids[request.id] = "not-admitted-" + stable_hash([entry.id, request.id])[:24]
                        continue
                    identities.add(identity)
                    old_id = request.id
                    if len(batches) > 1:
                        request = request.model_copy(update={"id": "followup-" + stable_hash(identity)[:24]})
                    request_ids[old_id] = request.id
                    request = request.model_copy(update={"planning_question_indices": []})
                    followups.append(request)
                for question in (proposal.question_states or bind_plan_questions(proposal).question_states):
                    linked = [request_ids[key] for key in question.followup_ids]
                    previous = question_states.get(question.id)
                    question_states[question.id] = question.model_copy(update={"followup_ids": list(dict.fromkeys([
                        *(previous.followup_ids if previous else []), *linked]))})
            else:
                message = f"目录批次 {entry.id} 未覆盖：{coverage.reason}"
                questions.append(message)
                warnings.append(message)
            entries[index] = coverage
            aggregate = aggregate.model_copy(update={"catalog_batches": entries})
            self.runtime.data["plan"] = aggregate.model_dump(mode="json")
            self.runtime.data["plan_warnings"] = list(dict.fromkeys(warnings))
            await self.runtime.persist()
        if not proposals:
            raise ValueError(next((entry.reason for entry in entries if entry.reason),
                                  "coordination_catalog_not_covered"))
        decision = ("required" if any(item.decision == "required" for item in proposals)
                    else "skip" if not questions and all(item.decision == "skip" for item in proposals)
                    else payload["risk"]["decision"])
        aggregate = aggregate.model_copy(update={"decision": decision, "followups": followups,
            "unresolved_questions": list(dict.fromkeys(questions)), "relationship_ids": sorted(relationships),
            "evidence_ids": sorted(evidence), "reason": (proposals[0].reason if len(batches) == 1 else
                f"目录分为 {len(batches)} 批；{len(proposals)} 批完成规划，{len(batches) - len(proposals)} 批未覆盖")})
        for question in bind_plan_questions(aggregate).question_states:
            question_states.setdefault(question.id, question)
        aggregate = aggregate.model_copy(update={"question_states": list(question_states.values())})
        self.validate_plan(aggregate, payload)
        return aggregate

    async def _coordinate_with_output_repair(self, batch, model, usages):
        try:
            return await self.provider.coordinate_cross_units(batch, model)
        except LLMProviderError as exc:
            usage = exc.usage
            errors = getattr(exc, "output_schema_errors", [])
            if (getattr(exc, "coordination_error_code", None) != "decision_followups_conflict"
                    or not errors or any(item.get("type") != "decision_followups_conflict" for item in errors)
                    or usage is None or not usage.usage_available or usage.actual_total_tokens is None
                    or not callable(getattr(self.provider.provider, "repair_cross_unit_plan", None))):
                raise
            # 初次失败的响应已计费；保留它，再由同一共享账本准入一个独立纠错请求。
            # 缓存身份包含相同目录及稳定反馈，恢复只复用这一次纠错，不产生第三次发送。
            usages.append(usage.model_dump(mode="json"))
            feedback = {"attempt": 1, "errors": errors[:10]}
            try:
                return await self.provider.repair_cross_unit_plan(batch, model, feedback)
            except LLMProviderError as repair_error:
                if not getattr(repair_error, "coordination_error_code", None):
                    not_admitted = getattr(repair_error, "admission_rejection", None) or any(code in str(repair_error) for code in (
                        "model_context_window_exceeded", "model_input_limit_exceeded", "model_output_limit_exceeded"))
                    repair_error.coordination_error_code = ("coordination_repair_not_admitted" if not_admitted
                                                            else "coordination_repair_failed")
                raise

    @staticmethod
    def _planning_failure_reason(exc):
        reasons = {
            "decision_followups_conflict": "协调计划的决策与补查列表冲突，未执行补查",
            "coordination_output_invalid": "协调计划响应已收到，但输出协议无效，未执行补查",
            "coordination_repair_not_admitted": "协调计划输出无效，纠错请求未获预算或窗口准入，未执行补查",
            "coordination_repair_failed": "协调计划输出无效，后续纠错请求失败，未执行补查",
        }
        code = getattr(exc, "coordination_error_code", None)
        if code in reasons:
            return reasons[code]
        # 唯一目录批次失败时，外层只传递已生成的可读原因，不再叠加异常类名。
        if str(exc).startswith("协调计划"):
            return str(exc)[:1000]
        return f"{type(exc).__name__}: {exc}"[:1000]

    def _aggregate_output(self, state: dict) -> dict:
        from app.services.followup_contracts import reconcile_contracts
        current_plan = (self.runtime.data.get("plan") or {}) if self.runtime else {}
        reconciled = reconcile_contracts({**state, "coordination_plan": current_plan},
            list(self.runtime.data["followups"].values()) if self.runtime else [], self.catalog)
        batches = list(self.runtime.data["batches"].values()) if self.runtime else []
        metrics = dict(self.runtime.data["base_metrics"] if self.runtime else state.get("issue_metrics") or {})
        for batch in batches:
            for key, value in batch.get("issue_metrics", {}).items():
                metrics[key] = metrics.get(key, 0) + value
        issues = {item["id"]: item for item in state.get("review_issues") or []}
        for batch in batches:
            issues.update({item["id"]: item for item in batch.get("review_issues") or []})
        return {**({"review_unit_results": reconciled} if reconciled else {}),
            "review_issues": list(issues.values()), "issue_metrics": metrics,
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
        # 每轮只执行缺失任务；后续补查结算后重查，未知/已发送失败不能重发。
        for round_index in range(len(plan.followups) + 1):
            before = self.runtime.budget_signature()
            await self._execute_followup_pass(plan, state)
            await self._retry_verifications(state)
            if not self.runtime.data.get("deferred_followups") and not any(
                    item.get("pending_verifications") for item in self.runtime.data["batches"].values()):
                break
            changed = await self._budget_progress(before)
            if not changed and round_index > 0:
                break
            # 首轮所有未来任务已尝试，旧份额保留不再阻断暂缓任务。
            for request_id in self.runtime.data.get("deferred_followups", {}):
                allocation = self.runtime.data["allocations"].get(request_id)
                if allocation:
                    allocation.update(future_calls=0, future_tokens=0,
                        exploration_calls=max(1, self.provider.budget.max_model_calls - self.provider.budget.model_calls),
                        exploration_tokens=max(1, self.provider.budget.max_token_usage - self.provider.budget.token_usage))
            await self.runtime.persist()
        output = self._aggregate_output(state)
        usages = {raw["id"]: raw for raw in output["model_usages"]}
        for followup in self.runtime.data["followups"].values():
            usages.update({raw["id"]: raw for raw in followup["unit_result"].get("model_usages") or []})
        output["model_usages"] = list(usages.values())
        return output

    async def _execute_followup_pass(self, plan: CrossUnitCoordinationPlan,
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
            readable = self._followup_readable(request, self.catalog(state))
            unit = ReviewUnit(
                id="followup-" + digest[:24], primary_files=request.primary_files,
                related_files=sorted(readable - set(request.primary_files)),
                diff_hunk_ids=list(dict.fromkeys(key for item in selected for key in item.diff_hunk_ids)),
                rule_ids=list(dict.fromkeys(key for item in selected for key in item.rule_ids)),
                risk_tags=["cross_module"], complexity="small", estimated_tokens=0,
                fingerprint=digest, grouping_reason="cross_unit_followup",
            )
            local = {**state, "cross_unit_followup": request.model_dump(mode="json"),
                     "origin_obligations": [item for item in self.catalog(state).get("origin_obligations", [])
                                            if item["id"] in request.origin_obligation_ids],
                     "cross_unit_followup_context": [
                         {**snippet, "review_unit_id": unit.id}
                         for raw in state.get("review_unit_results") or []
                         if raw["review_unit_id"] in request.unit_ids
                         for snippet in raw.get("context_snippets") or []
                         if snippet.get("file") in readable
                     ]}
            # 继承正文必须能按当前快照重建到原 Unit 的真实证据，旧 ID 不授予当前引用权。
            from app.services.review_input_context import build_context_evidence
            from app.review.diff_base import review_diff_base
            trusted = {ref["id"] for raw in state.get("review_unit_results") or []
                if raw["review_unit_id"] in request.unit_ids
                for ref in (raw.get("review_summary") or {}).get("evidence", [])
                if ref.get("head_sha") == str(state.get("head_sha") or "")
                and ref.get("base_sha", "") == review_diff_base(state)}
            inherited = []
            for snippet in local["cross_unit_followup_context"]:
                try:
                    _, references = build_context_evidence([snippet], str(state.get("head_sha") or ""), review_diff_base(state))
                    if references and {ref["id"] for ref in references} <= trusted:
                        inherited.append(snippet)
                except (ValueError, TypeError, KeyError):
                    continue
            local["cross_unit_followup_context"] = inherited
            saved = self.runtime.data["followups"].get(request.id)
            allocations = self.runtime.data.setdefault("allocations", {})
            budget = self.provider.budget
            calls = max(0, budget.max_model_calls - budget.model_calls)
            tokens = max(0, budget.max_token_usage - budget.token_usage)
            if request.id not in allocations:
                pending = sum(item.id not in self.runtime.data["batches"] for item in plan.followups)
                share_calls, share_tokens = calls // max(1, pending), tokens // max(1, pending)
                allocations[request.id] = {
                    "followup_id": request.id, "future_calls": calls - share_calls,
                    "future_tokens": tokens - share_tokens,
                    "verification_calls": min(2, share_calls), "verification_tokens": share_tokens // 3,
                    "exploration_calls": max(1, share_calls - min(2, share_calls)),
                    "exploration_tokens": max(1, share_tokens - share_tokens // 3),
                }
                await self.runtime.persist()
            allocation = allocations[request.id]
            # 子图和共享请求账本使用同一额度；不再额外套用固定的 12K 上限。
            local["_cross_unit_budget"] = ExecutionBudget(
                max_model_calls=allocation["exploration_calls"],
                max_token_usage=allocation["exploration_tokens"],
                max_diagnosis_attempts=allocation["exploration_calls"] if executor._supports_native_review_step() else 1,
                max_context_retrievals=min(12, max(2, 2 * len(
                    (set(request.verification_files) | set(request.supporting_files))
                    - {file["file_path"] for file in state.get("changed_files") or [] if file.get("hunks")})),
                    max(0, allocation["exploration_calls"] - 1)) + int(bool(request.tracking_queries)), max_patch_attempts=0,
            ).model_dump(mode="json")
            deferred_followups = self.runtime.data.setdefault("deferred_followups", {})
            if saved and request.id in deferred_followups:
                local["review_unit_results"] = [saved["unit_result"]]
                saved = None
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
            refusal = result.budget_rejection
            deferred_followups = self.runtime.data.setdefault("deferred_followups", {})
            if ((refusal and refusal.can_defer) or any(
                    item.admission.get("deferred_budget") for item in result.diff_batches)):
                deferred_followups[request.id] = {"reason": result.error or "budget",
                    "budget_signature": self.runtime.budget_signature()}
                self.runtime.data["followups"][request.id] = CrossUnitFollowupResult(
                    request_id=request.id, outcome="unresolved", unit_result=result, fingerprint=digest,
                    evidence_ids=request.evidence_ids, reason="deferred_budget_not_executed: followup").model_dump(mode="json")
                await self._persist_followup_progress(state)
                continue
            self.runtime.data["deferred_followups"].pop(request.id, None)
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
                audit_issue("identity", issue, reason="followup_server_identity", canonical_id=identity,
                            canonical_review_unit_id=unit.id)
                audit_issue("identity_assigned", candidate, reason="followup_server_identity")
                issues.append(candidate)
            result = result.model_copy(update={"issues": issues})
            usages.extend(item.model_dump(mode="json") for item in result.model_usages)
            outcome, reason = self.followup_conclusion(request, result, selected)
            followup = CrossUnitFollowupResult(
                request_id=request.id, outcome=outcome, unit_result=result,
                fingerprint=digest,
                evidence_ids=request.evidence_ids, reason=reason,
            )
            self.runtime.data["followups"][request.id] = followup.model_dump(mode="json")
            await self._persist_followup_progress(state)
            if not issues:
                self.runtime.data["batches"][request.id] = {"review_issues": [], "issue_metrics": {}}
                followup = followup.model_copy(update={"validation_status": "completed"})
                self.runtime.data["followups"][request.id] = followup.model_dump(mode="json")
                await self._persist_followup_progress(state)
                continue
            # persist 会规范化并替换嵌套字典，重新取得本次账本中的引用。
            allocation = self.runtime.data["allocations"][request.id]
            if "verification_capacity" not in allocation:
                # 诊断已结束：本份额未用的探索调用可交给独立验证，但不借用后续补查。
                # 保存一次，恢复时仍使用相同的 remaining_calls 与模型请求身份。
                allocation["verification_capacity"] = max(0, self.provider.budget.max_model_calls
                    - self.provider.budget.model_calls - allocation["future_calls"])
                await self.runtime.persist()
            # 只处理新增批次，不重验或重新计数原有候选，不写入普通 Unit 结果。
            batch = {**state, "review_units": [unit.model_dump(mode="json")],
                     "review_unit_results": [], "review_issues": [item.model_dump(mode="json") for item in issues],
                     "model_usages": [], "issue_metrics": {}, "step_progress": [],
                     "context_snippets": [item.model_dump(mode="json") for item in result.context_snippets],
                     "_issue_verifier_service": IssueVerifierService(
                         self.provider, enabled=True, fail_mode="unresolved",
                         max_calls_per_unit=allocation["verification_capacity"],
                     )}
            self.provider.holdback = {
                "followup_id": request.id, "phase": "verification",
                "model_calls": allocation["future_calls"], "token_usage": allocation["future_tokens"],
            }
            try:
                for node in (resolve_evidence_node, issue_policy_node):
                    batch.update(await node(batch))
                ready = {raw["id"]: raw for raw in batch["review_issues"] if raw["status"] == "evidence_resolved"}
                batch.update(await issue_verifier_node(batch))
            finally:
                self.provider.holdback = None
            self.runtime.data["batches"][request.id] = {key: batch.get(key) for key in (
                "review_issues", "issue_metrics", "model_usages", "deterministic_issue_checks",
                "issue_verifications", "warnings")}
            pending = {raw["id"]: ready[raw["id"]] for raw in batch["review_issues"]
                if "deferred_budget_not_executed:" in str(raw.get("unresolved_reason") or "")}
            self.runtime.data["batches"][request.id].update(pending_verifications=pending,
                verification_unit=unit.model_dump(mode="json"))
            result = result.model_copy(update={"issues": [type(issues[0]).model_validate(raw)
                                                           for raw in batch["review_issues"]]})
            followup = followup.model_copy(update={"unit_result": result,
                "validation_status": "pending" if pending else "completed"})
            self.runtime.data["followups"][request.id] = followup.model_dump(mode="json")
            await self._persist_followup_progress(state)
        aggregated = self._aggregate_output(state)
        aggregated["model_usages"].extend(usages)
        return aggregated

    async def _persist_followup_progress(self, state):
        plan = CrossUnitCoordinationPlan.model_validate(self.runtime.data["plan"])
        plan = refresh_plan_questions(plan, list(self.runtime.data["followups"].values()), state)
        self.runtime.data["plan"] = plan.model_dump(mode="json")
        await self.runtime.persist()

    @staticmethod
    def followup_conclusion(request, result, selected):
        if result.status != ReviewUnitStatus.completed:
            return "failed", result.error or "补查执行未完成"
        summary, record = result.review_summary, result.review_summary.record
        valid = summary.status == "reported" and summary.latest_attempt_status == "reported" and record is not None
        if result.issues:
            return "candidate_found", "新增候选将接受证据、策略与独立验证" + (
                "；补查记录无效，不能据此关闭原问题" if not valid else "")
        if not valid:
            return "unresolved", ("补查记录未通过校验，无法采用结论：" +
                str(summary.latest_attempt_reason or summary.reason or "missing_review_record"))[:1000]
        refs = {item.id: item.file_path for item in summary.evidence if item.source != "repository_tree"}
        all_refs = {item.id for item in summary.evidence}
        paths = {refs[key] for check in record.hypothesis_checks for key in check.evidence_ids if key in refs}
        # 新请求按显式核验范围；旧请求仍要求覆盖每个所选 Unit 的主文件。
        scope_covered = (set(request.verification_files) <= paths if request.verification_files else
                         all(set(item.primary_files) & paths for item in selected))
        closed = {item.obligation_id for item in record.obligation_updates}
        gaps = []
        if not scope_covered:
            required = set(request.verification_files) or {path for unit in selected for path in unit.primary_files}
            gaps.append("缺少核验正文引用：" + "、".join(sorted(required - paths)))
        if not record.hypothesis_checks:
            gaps.append("缺少风险假设检查")
        for check in record.hypothesis_checks:
            if check.status != "refuted":
                gaps.append(f"风险假设 {check.hypothesis_id}={check.status}，未按风险反证协议闭合")
            if not check.evidence_ids or not set(check.evidence_ids) <= all_refs:
                gaps.append(f"风险假设 {check.hypothesis_id} 缺少当前证据")
        if not record.target_checks or any(item.status != "checked" for item in record.target_checks):
            gaps.append("目标检查未完成")
        gaps.extend("未决问题：" + item.question for item in record.unresolved_questions)
        gaps.extend(f"未闭合契约 {item.file_path}：{item.assumption}" for item in record.contract_dependencies
                    if item.status != "verified")
        if set(request.origin_obligation_ids) - closed:
            gaps.append("父义务未闭合：" + "、".join(sorted(set(request.origin_obligation_ids) - closed)))
        if request.tracking_queries:
            cited = {key for check in record.hypothesis_checks for key in check.evidence_ids}
            import hashlib
            applicable = False
            for snippet in result.context_snippets:
                if snippet.source != "repository_tree":
                    continue
                try:
                    fact = json.loads(snippet.content)
                    applicable |= bool(fact.get("complete") and set(request.tracking_queries) <= set(fact.get("queries", []))
                        and any(ref.id in cited and ref.source == "repository_tree" and ref.head_sha == fact.get("head_sha")
                            and ref.content_hash == hashlib.sha256(snippet.content.encode("utf-8")).hexdigest()
                            for ref in summary.evidence))
                except (ValueError, TypeError):
                    continue
            if not applicable:
                gaps.append("仓库跟踪状态缺少适用的同快照完整 tree 证据或引用")
        for event in result.messages:
            if (event.message or "").startswith("followup_evidence_"):
                gaps.append(event.message)
        if gaps:
            budget = result.execution_budget
            gaps.append(f"取证额度 {budget.context_retrievals}/{budget.max_context_retrievals}；"
                f"模型调用 {budget.model_calls}/{budget.max_model_calls}")
            return "unresolved", "；".join(dict.fromkeys(gaps))[:1000]
        return "refuted", "已找到限定核验范围的反证且完成目标检查"

    async def _retry_verifications(self, state: dict) -> None:
        from app.services.issue_verifier import IssueVerifierService

        for request_id, saved in list(self.runtime.data["batches"].items()):
            pending = saved.get("pending_verifications") or {}
            if not pending:
                continue
            followup = self.runtime.data["followups"][request_id]
            unit = ReviewUnit.model_validate(saved["verification_unit"])
            context = (followup.get("unit_result") or {}).get("context_snippets") or []
            self.provider.holdback = {"followup_id": request_id, "phase": "verification"}
            try:
                verified = await IssueVerifierService(self.provider, enabled=True, fail_mode="unresolved",
                    max_calls_per_unit=max(0, self.provider.budget.max_model_calls - self.provider.budget.model_calls)).verify_issues(
                        [ReviewIssue.model_validate(raw) for raw in pending.values()], [unit],
                        {**state, "context_snippets": context}, IssueMetrics.model_validate(saved.get("issue_metrics") or {}))
            finally:
                self.provider.holdback = None
            # persist 可能替换嵌套字典，重新取引用；只替换本次延期候选，不重做 Policy。
            saved = self.runtime.data["batches"][request_id]
            updated = {issue.id: issue.model_dump(mode="json") for issue in verified.issues}
            saved["review_issues"] = [updated.get(raw["id"], raw) for raw in saved["review_issues"]]
            saved["issue_metrics"] = verified.metrics.model_dump(mode="json")
            saved["issue_verifications"] = [*(saved.get("issue_verifications") or []),
                *(item.model_dump(mode="json") for item in verified.verifications)]
            usages = {item["id"]: item for item in saved.get("model_usages") or []}
            usages.update({item.id: item.model_dump(mode="json") for item in verified.model_usages})
            saved["model_usages"] = list(usages.values())
            saved["warnings"] = [warning for warning in saved.get("warnings") or []
                if not any(warning.startswith(f"Issue {identity} ") for identity in pending)] + verified.warnings
            saved["pending_verifications"] = {identity: pending[identity] for identity, raw in updated.items()
                if "deferred_budget_not_executed:" in str(raw.get("unresolved_reason") or "")}
            followup = self.runtime.data["followups"][request_id]
            followup["unit_result"]["issues"] = saved["review_issues"]
            followup["validation_status"] = "pending" if saved["pending_verifications"] else "completed"
            await self._persist_followup_progress(state)
