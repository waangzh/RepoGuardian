"""协调专用运行账本：预算预留、调用幂等、结果检查点与版本隔离。"""

import asyncio
import json
import time
from copy import deepcopy
from typing import Any

from app.agents.providers import LLMProviderError
from app.core.config import settings
from app.models.review import (
    AgentAction, CrossUnitCoordinationPlan, CrossUnitRuntimeMetrics, ExecutionBudget,
    IssueVerification, ModelCallResult, ModelUsage, UnitReviewResponse,
)
from app.services.fingerprints import stable_hash
from app.services.coordination_catalog import CATALOG_BATCH_VERSION
from app.services.model_usage import unpack_model_call, model_request_budget_hook
from app.services.model_request_budgeter import request_budget_reserver


class CoordinationLeaseLost(asyncio.CancelledError):
    """取消或写者已被替代；不能转成普通失败后继续发布。"""


def normalize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()
                if key not in {"repo_path", "repository_root"}}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def estimate_request(provider: Any, name: str, args: tuple, output_tokens: int) -> dict:
    estimator = getattr(provider, "coordination_request_chars", None)
    chars = (estimator(name, args) if callable(estimator) else
             len(json.dumps(normalize(args), ensure_ascii=False)))
    return {"estimate": (chars + 3) // 4 + output_tokens, "input_chars": chars,
            "output_token_allowance": output_tokens,
            "estimate_source": "provider_prompt" if callable(estimator) else "serialized_arguments"}


def budget_rejection(budget: ExecutionBudget, name: str, estimate: int,
                     holdback: dict | None = None) -> dict:
    held = holdback or {}
    return {"operation": name,
            "exhausted_dimensions": [key for key, amount in (
                ("model_calls", 1 + held.get("model_calls", 0)),
                ("token_usage", estimate + held.get("token_usage", 0)))
                                     if not budget.can_consume(**{key: amount})],
            "requested_tokens": estimate, "used_tokens": budget.token_usage,
            "remaining_tokens": max(0, budget.max_token_usage - budget.token_usage),
            "used_calls": budget.model_calls,
            "remaining_calls": max(0, budget.max_model_calls - budget.model_calls),
            "holdback": held,
            "available_tokens": max(0, budget.max_token_usage - budget.token_usage - held.get("token_usage", 0)),
            "available_calls": max(0, budget.max_model_calls - budget.model_calls - held.get("model_calls", 0))}


def coordination_fingerprint(state: dict) -> str:
    risk = {key: item for key, item in (state.get("cross_unit_risk") or {}).items()
            if key not in {"execution_status", "non_execution_reason"}}
    return stable_hash({
        "purpose": "cross-unit-runtime-v1-request-admission-v1", "task_id": state.get("task_id"),
        "unit_input_mode": settings.repoguardian_unit_input_mode,
        "request_profile": settings.repoguardian_model_request_profile.model_dump(mode="json"),
        "model_profiles": {key: value.model_dump(mode="json") for key, value in
                           settings.repoguardian_model_request_profiles.items()},
        "catalog_version": CATALOG_BATCH_VERSION,
        "request_contract_version": "coordination-canonical-evidence-allocation-v4-unit-diagnosis-holdback",
        "repository": (state.get("pr_info") or {}).get("clone_url"),
        "base_sha": state.get("base_sha"), "head_sha": state.get("head_sha"),
        "model": state.get("model") or settings.repoguardian_model,
        "provider": settings.repoguardian_provider,
        "versions": [settings.repoguardian_prompt_version, settings.repoguardian_rule_version,
                     settings.repoguardian_tool_schema_version, settings.repoguardian_review_policy_version,
                     settings.repoguardian_config_version, settings.openai_base_url,
                     (state.get("review_plan") or {}).get("planner_version")],
        "units": state.get("review_units") or [],
        "results": [{key: item for key, item in result.items() if key not in {"model_usages", "messages", "tool_events"}}
                    for result in state.get("review_unit_results") or []],
        "risk": risk,
    })


class CoordinationRuntime:
    def __init__(self, task_id: str, fingerprint: str, budget: ExecutionBudget,
                 repository: Any = None, lease: dict | None = None, progress: Any = None) -> None:
        self.task_id, self.fingerprint = task_id, fingerprint
        self.repository, self.lease, self.progress = repository, lease, progress
        self.lock = asyncio.Lock()
        self.expected_budget: dict | None = None
        self.data: dict[str, Any] = {
            "revision": -1, "status": "running", "budget": budget.model_dump(mode="json"),
            "calls": {}, "plan": None, "followups": {}, "batches": {}, "output": None,
            "base_metrics": {}, "resume_count": 0, "cache_hits": 0,
        }

    async def load(self) -> None:
        if self.repository is not None:
            saved, budget = await asyncio.to_thread(
                self.repository.load_coordination_runtime, self.task_id, self.fingerprint,
            )
            if saved:
                self.data = deepcopy(saved)
                self.data["resume_count"] += 1
            self.expected_budget = budget
            if budget:
                self.data["budget"] = budget

    @property
    def budget(self) -> ExecutionBudget:
        return ExecutionBudget.model_validate(self.data["budget"])

    async def persist(self) -> None:
        revision = self.data["revision"]
        payload = deepcopy({**self.data, "revision": revision + 1})
        if self.repository is not None:
            await asyncio.to_thread(self.repository.save_coordination_runtime,
                self.task_id, self.fingerprint, payload, revision, self.expected_budget, self.lease)
        self.data = payload
        self.expected_budget = deepcopy(payload["budget"])
        if self.progress is not None:
            self.progress(self.public_state())

    def metrics(self) -> CrossUnitRuntimeMetrics:
        calls = list(self.data["calls"].values())
        usages = [ModelUsage.model_validate(item["usage"]) for item in calls if item.get("usage")]
        attempts = sum(item.get("attempts", 1) for item in calls)
        issues = [raw for batch in self.data["batches"].values() for raw in batch.get("review_issues") or []]
        return CrossUnitRuntimeMetrics(
            model_calls=sum(item.get("attempts", 1) for item in calls),
            failed_calls=sum(item["status"] == "failed" for item in calls),
            unknown_calls=sum(item["status"] in {"running", "unknown"} for item in calls),
            cache_hits=self.data["cache_hits"], resume_count=self.data["resume_count"],
            estimated_tokens=sum(item["estimate"] * item.get("attempts", 1) for item in calls),
            actual_tokens=sum(item.actual_total_tokens or 0 for item in usages),
            usage_reported_calls=sum(item.usage_available for item in usages),
            cost_microusd=(sum(item.cost_microusd for item in usages)
                          if len(usages) == attempts and all(item.cost_microusd is not None for item in usages)
                          else None),
            latency_ms=sum(item.get("latency_ms", 0) for item in calls),
            completed_followups=sum(
                item.get("outcome") != "failed" and (item.get("unit_result") or {}).get("status") == "completed"
                for identity, item in self.data["followups"].items() if identity in self.data["batches"]
            ), candidate_count=len(issues),
            confirmed_count=sum(item["status"] == "confirmed" for item in issues),
        )

    def public_state(self) -> dict:
        plan = CrossUnitCoordinationPlan.model_validate(self.data["plan"] or {})
        plan = plan.model_copy(update={"execution_budget": self.budget,
            "runtime_fingerprint": self.fingerprint, "runtime_metrics": self.metrics()})
        return {"coordination_plan": plan.model_dump(mode="json"),
                "followup_results": list(self.data["followups"].values())}

    async def call(self, provider: Any, name: str, args: tuple, output_tokens: int,
                   *, holdback: dict | None = None) -> Any:
        held = holdback or {}
        payload = normalize(args)
        key = stable_hash({"operation": name, "input": payload,
                           "allocation": held.get("followup_id"), "phase": held.get("phase")})
        types = {"coordinate_cross_units": CrossUnitCoordinationPlan, "decide": AgentAction,
                 "review_unit": UnitReviewResponse, "verify_issue": IssueVerification}
        async with self.lock:
            existing = self.data["calls"].get(key)
            if existing:
                self.data["cache_hits"] += 1
                if existing["status"] == "completed":
                    await self.persist()
                    value = types[name].model_validate(existing["value"])
                    usage = ModelUsage.model_validate(existing["usage"]) if existing.get("usage") else None
                    return ModelCallResult(value, usage) if usage else value
                if existing["status"] == "running":
                    existing["status"] = "unknown"
                await self.persist()
                raise LLMProviderError(existing.get("error") or "previous_call_outcome_unknown")
            prepared = bool(getattr(provider, "supports_request_admission", False))
            reservation = ({"estimate": 0, "estimate_source": "provider_prompt",
                            "estimate_method": "prepared_request"}
                           if prepared else estimate_request(provider, name, args, output_tokens))
            estimate = reservation["estimate"]
            if not self.budget.can_consume(model_calls=1 + held.get("model_calls", 0),
                                           token_usage=estimate + held.get("token_usage", 0)):
                rejection = budget_rejection(self.budget, name, estimate, held)
                self.data.setdefault("budget_rejections", []).append(rejection)
                await self.persist()
                raise LLMProviderError("cross_unit_shared_budget_exhausted: " + json.dumps(rejection))
            if not prepared:
                self.data["budget"] = self.budget.consume(model_calls=1, token_usage=estimate).model_dump(mode="json")
            self.data["calls"][key] = {"status": "running", "operation": name,
                                       **reservation, "holdback": held, "attempts": 0 if prepared else 1,
                                       "correction_tokens": 0}
            await self.persist()  # 预留落盘成功后，才允许发起外部请求。
            started = time.monotonic()
            async def reserve_retry() -> None:
                if not self.budget.can_consume(model_calls=1 + held.get("model_calls", 0),
                                               token_usage=estimate + held.get("token_usage", 0)):
                    rejection = {**budget_rejection(self.budget, name, estimate, held), "transport_retry": True}
                    self.data.setdefault("budget_rejections", []).append(rejection)
                    await self.persist()
                    raise LLMProviderError("cross_unit_shared_budget_exhausted_before_transport_retry: "
                                           + json.dumps(rejection))
                self.data["budget"] = self.budget.consume(model_calls=1, token_usage=estimate).model_dump(mode="json")
                self.data["calls"][key]["attempts"] += 1
                await self.persist()
            outer_reserver = request_budget_reserver.get()
            async def reserve_prepared(metadata: dict) -> None:
                nonlocal estimate
                estimate = metadata["reserved_tokens"]
                if not self.budget.can_consume(model_calls=1 + held.get("model_calls", 0),
                                               token_usage=estimate + held.get("token_usage", 0)):
                    rejection = {**budget_rejection(self.budget, name, estimate, held),
                                 "transport_retry": self.data["calls"][key]["attempts"] > 0}
                    self.data.setdefault("budget_rejections", []).append(rejection)
                    await self.persist()
                    reason = ("cross_unit_shared_budget_exhausted_before_transport_retry"
                              if rejection["transport_retry"] else "cross_unit_shared_budget_exhausted")
                    raise LLMProviderError(reason + ": " + json.dumps(rejection))
                if outer_reserver is not None:
                    # A follow-up Unit also owns a local budget. Check both ledgers
                    # against this exact request, without masking the Unit hook.
                    await outer_reserver(metadata)
                self.data["budget"] = self.budget.consume(
                    model_calls=1, token_usage=estimate).model_dump(mode="json")
                self.data["calls"][key].update(estimate=estimate, request_admission=metadata,
                    # Keep the legacy diagnostic character estimate readable.
                    # Admission/accounting use reserved_tokens, never this field.
                    input_chars=metadata["prompt_chars"] + 512)
                self.data["calls"][key]["attempts"] += 1
                await self.persist()
            hook_token = model_request_budget_hook.set(reserve_retry)
            prepared_token = request_budget_reserver.set(reserve_prepared) if prepared else None
            usage = None
            try:
                raw = await getattr(provider, name)(*args)
                value, usage = unpack_model_call(raw)
                value = types[name].model_validate(value)
                self.data["calls"][key].update(status="completed", value=value.model_dump(mode="json"),
                    usage=usage.model_dump(mode="json") if usage else None)
                if usage and usage.actual_total_tokens is not None:
                    # 不退还已预留的失败/重试预算；实际用量高于估算时补记并阻止后续超额调用。
                    correction = max(0, usage.actual_total_tokens - estimate)
                    self.data["calls"][key]["correction_tokens"] = correction
                    self.data["budget"] = self.budget.model_copy(update={
                        "token_usage": self.budget.token_usage + correction,
                    }).model_dump(mode="json")
            except asyncio.CancelledError:
                self.data["calls"][key].update(status="unknown", error="cancelled_call_outcome_unknown")
                raise
            except Exception as exc:
                usage = getattr(exc, "usage", None) or usage
                self.data["calls"][key].update(status="failed", error=f"{type(exc).__name__}: {exc}",
                    usage=usage.model_dump(mode="json") if usage else None)
                if usage and usage.actual_total_tokens is not None:
                    correction = max(0, usage.actual_total_tokens - estimate)
                    self.data["calls"][key]["correction_tokens"] = correction
                    self.data["budget"] = self.budget.model_copy(update={
                        "token_usage": self.budget.token_usage + correction,
                    }).model_dump(mode="json")
                self.data["calls"][key]["latency_ms"] = int((time.monotonic() - started) * 1000)
                await self.persist()
                raise
            finally:
                if prepared_token is not None:
                    request_budget_reserver.reset(prepared_token)
                model_request_budget_hook.reset(hook_token)
                self.data["calls"][key]["latency_ms"] = int((time.monotonic() - started) * 1000)
            await self.persist()
            return raw
