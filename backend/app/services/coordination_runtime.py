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
from app.services.review_run_budget import current_run_budget, run_budget_attempt_observer


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
    from app.services.model_request_budgeter import CONTEXT_BUDGET_VERSION
    resolved = settings.resolve_model_profile(state.get("model"))
    risk = {key: item for key, item in (state.get("cross_unit_risk") or {}).items()
            if key not in {"execution_status", "non_execution_reason"}}
    return stable_hash({
        "purpose": "cross-unit-runtime-v2-bound-contract-closure", "task_id": state.get("task_id"),
        "unit_input_mode": settings.repoguardian_unit_input_mode,
        "request_profile": resolved.profile.model_dump(mode="json"),
        "model_profile_source": resolved.source, "context_budget_version": CONTEXT_BUDGET_VERSION,
        "model_profiles": {key: value.model_dump(mode="json") for key, value in
                           settings.repoguardian_model_request_profiles.items()},
        "catalog_version": CATALOG_BATCH_VERSION,
        "request_contract_version": "coordination-canonical-evidence-v16-followup-evidence-polarity-tree",
        "repository": (state.get("pr_info") or {}).get("clone_url"),
        "base_sha": state.get("base_sha"), "head_sha": state.get("head_sha"),
        "model": state.get("model") or settings.repoguardian_model,
        "provider": settings.repoguardian_provider,
        "versions": [settings.repoguardian_prompt_version, settings.repoguardian_rule_version,
                     settings.repoguardian_tool_schema_version, settings.repoguardian_review_policy_version,
                     settings.repoguardian_config_version, settings.openai_base_url,
                     (state.get("review_plan") or {}).get("planner_version")],
        "units": state.get("review_units") or [],
        "results": [_review_result_identity(result) for result in state.get("review_unit_results") or []],
        "risk": risk,
    })


def _review_result_identity(result):
    """协调只依赖当前记录与活动证据；归档正文及选择操作不进入其输入身份。"""
    identity = {key: item for key, item in result.items() if key not in {
        "model_usages", "messages", "tool_events", "evidence_store", "context_selection"}}
    if "diff_batches" in identity:
        identity["diff_batches"] = [{**batch, "result": _review_result_identity(batch["result"])
            if batch.get("result") else None} for batch in identity["diff_batches"]]
    return identity


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
        self._committed = deepcopy(self.data)
        self._invalidated = False

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
        self._committed = deepcopy(self.data)
        self._invalidated = False

    @property
    def budget(self) -> ExecutionBudget:
        run_budget = current_run_budget()
        if run_budget is not None:
            return ExecutionBudget(
                max_model_calls=run_budget.max_calls, max_token_usage=run_budget.max_tokens,
                model_calls=run_budget.calls_consumed,
                token_usage=run_budget.settled_actual + run_budget.in_flight_reserved + run_budget.unknown_reserved,
            )
        return ExecutionBudget.model_validate(self.data["budget"])

    async def persist(self) -> None:
        if self._invalidated:
            raise CoordinationLeaseLost("coordination runtime requires reload")
        run_budget = current_run_budget()
        if run_budget is not None:
            self.data["budget"] = self.budget.model_dump(mode="json")
            attempts = run_budget.snapshot()["attempts"]
            for call in self.data["calls"].values():
                if "attempt_ids" in call:
                    call["attempt_records"] = [attempts[key] for key in call["attempt_ids"] if key in attempts]
        revision = self.data["revision"]
        payload = deepcopy({**self.data, "revision": revision + 1})
        try:
            if self.repository is not None:
                await asyncio.to_thread(self.repository.save_coordination_runtime,
                    self.task_id, self.fingerprint, payload, revision, self.expected_budget, self.lease)
        except BaseException:
            self.data = deepcopy(self._committed)
            self._invalidated = True
            raise
        self.data = payload
        self._committed = deepcopy(payload)
        self.expected_budget = deepcopy(payload["budget"])
        if self.progress is not None:
            self.progress(self.public_state())

    def metrics(self) -> CrossUnitRuntimeMetrics:
        calls = list(self.data["calls"].values())
        run_budget = current_run_budget()
        live = run_budget.snapshot()["attempts"] if run_budget is not None else {}
        records = [live.get(raw["attempt_id"], raw) for call in calls
                   for raw in call.get("attempt_records", [])]
        legacy = [call for call in calls if "attempt_records" not in call]
        costs = [item.get("actual_cost_microusd") for item in records if item["sent"]]
        costs.extend(ModelUsage.model_validate(item["usage"]).cost_microusd
                     if item.get("usage") else None for item in legacy)
        known_cost = (sum(costs) if all(item is not None for item in costs)
                      and len(costs) == sum(item["sent"] for item in records)
                      + sum(item.get("attempts", 1) for item in legacy) else None)
        issues = [raw for batch in self.data["batches"].values() for raw in batch.get("review_issues") or []]
        return CrossUnitRuntimeMetrics(
            model_calls=(sum(item.get("attempts", 1) for item in legacy)
                         + sum(item["sent"] for item in records)),
            failed_calls=sum(item["status"] == "failed" and item.get("attempts", 1) > 0 for item in calls),
            unknown_calls=(sum(item["status"] in {"running", "unknown"} for item in legacy)
                           + sum(item["state"] == "unknown" for item in records)),
            cache_hits=self.data["cache_hits"], resume_count=self.data["resume_count"],
            estimated_tokens=(sum(item["estimate"] * item.get("attempts", 1) for item in legacy)
                              + sum(item["estimated_tokens"] for item in records if item["sent"])),
            actual_tokens=(sum(item.get("actual_tokens") or 0 for item in records)
                           + sum(ModelUsage.model_validate(item["usage"]).actual_total_tokens or 0
                                 for item in legacy if item.get("usage"))),
            usage_reported_calls=(sum(item["usage_available"] for item in records)
                                  + sum(ModelUsage.model_validate(item["usage"]).usage_available
                                        for item in legacy if item.get("usage"))),
            cost_microusd=known_cost,
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

    def budget_signature(self, holdback: dict | None = None) -> str:
        run = current_run_budget()
        return stable_hash({"budget": run.available() if run else self.budget.model_dump(mode="json"),
                            "revision": run.revision if run else None,
                            "holdback": holdback or {}})

    def deferred_error(self, reason: str, *, holdback=None, retry=False) -> LLMProviderError:
        error = LLMProviderError(reason)
        run = current_run_budget()
        error.admission_rejection = {"kind": "budget", "reason": reason,
            "can_defer": not retry and (run is not None or bool(holdback and (
                holdback.get("token_usage") or holdback.get("model_calls")))),
            "transport_retry": retry,
            "budget_revision": run.revision if run else self.data["revision"]}
        return error

    def _settle_usage(self, key: str, usage: ModelUsage | None, estimate: int) -> None:
        if current_run_budget() is not None:
            self.data["budget"] = self.budget.model_dump(mode="json")
            return
        if usage is None or usage.actual_total_tokens is None:
            return
        call = self.data["calls"][key]
        settled = call.setdefault("settled_usage_ids", [])
        if usage.id in settled:
            return
        settled.append(usage.id)
        delta = usage.actual_total_tokens - estimate
        call["correction_tokens"] = max(0, delta)
        call["released_tokens"] = max(0, -delta)
        self.data["budget"] = self.budget.model_copy(update={
            "token_usage": self.budget.token_usage + delta,
        }).model_dump(mode="json")

    async def call(self, provider: Any, name: str, args: tuple, output_tokens: int,
                   *, holdback: dict | None = None) -> Any:
        from app.services.model_request_budgeter import CONTEXT_BUDGET_VERSION

        held = holdback or {}
        run_budget = current_run_budget()
        payload = normalize(args)
        if name == "verify_issue" and isinstance(payload[0], dict):
            # Scheduling counters neither change the evidence nor the verifier
            # decision. Keep output/context limits in the semantic cache key.
            verifier_budget = payload[0].get("budget")
            if isinstance(verifier_budget, dict):
                verifier_budget.pop("remaining_calls", None)
        resolver = getattr(provider, "resolve_request_profile", None)
        model = args[3] if name in {"review_unit", "review_step"} else args[1]
        resolved = resolver(model) if callable(resolver) else None
        key = stable_hash({"operation": name, "input": payload, "output_tokens": output_tokens,
                           "context_budget_version": CONTEXT_BUDGET_VERSION,
                           "profile": resolved.profile.model_dump(mode="json") if resolved else None,
                           "model_profile_source": resolved.source if resolved else None,
                           "allocation": held.get("followup_id"), "phase": held.get("phase")})
        types = {"coordinate_cross_units": CrossUnitCoordinationPlan, "repair_cross_unit_plan": CrossUnitCoordinationPlan, "decide": AgentAction,
                 "review_unit": UnitReviewResponse, "review_step": UnitReviewResponse,
                 "verify_issue": IssueVerification}
        async with self.lock:
            existing = self.data["calls"].get(key)
            if existing and existing["status"] == "deferred_budget":
                if existing.get("budget_signature") != self.budget_signature(held):
                    self.data["calls"].pop(key)
                    existing = None
                else:
                    error = LLMProviderError(existing["error"])
                    error.admission_rejection = existing["admission_rejection"]
                    raise error
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
                error = LLMProviderError(existing.get("error") or "previous_call_outcome_unknown",
                    usage=ModelUsage.model_validate(existing["usage"]) if existing.get("usage") else None)
                error.coordination_error_code = existing.get("coordination_error_code")
                error.output_schema_errors = existing.get("output_schema_errors", [])
                raise error
            prepared = bool(getattr(provider, "supports_request_admission", False))
            reservation = ({"estimate": 0, "estimate_source": "provider_prompt",
                            "estimate_method": "prepared_request"}
                           if prepared else estimate_request(provider, name, args, output_tokens))
            estimate = reservation["estimate"]
            if run_budget is None and not self.budget.can_consume(model_calls=1 + held.get("model_calls", 0),
                                           token_usage=estimate + held.get("token_usage", 0)):
                rejection = budget_rejection(self.budget, name, estimate, held)
                self.data.setdefault("budget_rejections", []).append(rejection)
                await self.persist()
                raise self.deferred_error("cross_unit_shared_budget_exhausted: " + json.dumps(rejection),
                                          holdback=held)
            if not prepared and run_budget is None:
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
                    raise self.deferred_error(reason + ": " + json.dumps(rejection),
                                              holdback=held, retry=rejection["transport_retry"])
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
            # RunBudget/Provider owns each prepared attempt; compatibility
            # hooks are only installed when no unified ledger is active.
            hook_token = model_request_budget_hook.set(reserve_retry) if run_budget is None else None
            prepared_token = (request_budget_reserver.set(reserve_prepared)
                              if prepared and run_budget is None else None)
            outer_observer = run_budget_attempt_observer.get()
            async def observe_attempt(record, metadata):
                call = self.data["calls"][key]
                ids = call.setdefault("attempt_ids", [])
                if record.attempt_id in ids:
                    return
                ids.append(record.attempt_id)
                call.update(attempts=len(call["attempt_ids"]), estimate=metadata["reserved_tokens"],
                            request_admission=metadata)
                self.data["budget"] = self.budget.model_dump(mode="json")
                await self.persist()
                if outer_observer is not None:
                    await outer_observer(record, metadata)

            attempt_token = run_budget_attempt_observer.set(observe_attempt) if run_budget is not None else None
            usage = None
            try:
                raw = await getattr(provider, name)(*args)
                value, usage = unpack_model_call(raw)
                value = types[name].model_validate(value)
                self.data["calls"][key].update(status="completed", value=value.model_dump(mode="json"),
                    usage=usage.model_dump(mode="json") if usage else None)
                self._settle_usage(key, usage, estimate)
            except asyncio.CancelledError:
                self.data["calls"][key].update(status="unknown", error="cancelled_call_outcome_unknown")
                raise
            except Exception as exc:
                rejection = getattr(exc, "admission_rejection", None)
                usage = getattr(exc, "usage", None) or usage
                self.data["calls"][key].update(status="failed", error=f"{type(exc).__name__}: {exc}",
                    usage=usage.model_dump(mode="json") if usage else None)
                if getattr(exc, "coordination_error_code", None):
                    self.data["calls"][key].update(coordination_error_code=exc.coordination_error_code,
                        output_schema_errors=getattr(exc, "output_schema_errors", []))
                if rejection and rejection.get("can_defer") and not self.data["calls"][key].get("attempt_ids"):
                    if self.data["calls"][key].get("attempts", 0) == 0:
                        self.data["calls"][key].update(status="deferred_budget",
                            admission_rejection=rejection, budget_signature=self.budget_signature(held),
                            budget_revision=run_budget.revision if run_budget else self.data["revision"])
                self._settle_usage(key, usage, estimate)
                self.data["calls"][key]["latency_ms"] = int((time.monotonic() - started) * 1000)
                await self.persist()
                raise
            finally:
                if attempt_token is not None:
                    run_budget_attempt_observer.reset(attempt_token)
                    self.data["budget"] = self.budget.model_dump(mode="json")
                if self.budget.token_usage > self.budget.max_token_usage:
                    self.data["budget_overrun"] = {"actual_accounted_tokens": self.budget.token_usage,
                        "max_token_usage": self.budget.max_token_usage}
                if prepared_token is not None:
                    request_budget_reserver.reset(prepared_token)
                if hook_token is not None:
                    model_request_budget_hook.reset(hook_token)
                self.data["calls"][key]["latency_ms"] = int((time.monotonic() - started) * 1000)
            await self.persist()
            return raw
