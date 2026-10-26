"""新引擎的受控检查点；通过 request/attempt 与唯一预算对账。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict

from app.models.review import UnitEvidenceStore
from app.models.review_runtime import (Candidate, ExaminationResult, ProposedLead, RuntimeReadResponse,
                                      InvestigationResult, VerificationResult)
from app.models.review_runtime import ReviewGoal, ReviewWorkItem
from app.models.review_runtime import DiscussionRuntimeReadResponse
from app.models.review_runtime import DiscussionInvestigationResult
from app.services.review.coordination_runtime import CoordinationLeaseLost
from app.services.review.review_run_budget import AttemptState
from app.services.review.runtime.review_task_executor import TaskProjection
from app.services.review.task_context_pages import ContextPage
from app.core.fingerprints import stable_hash


class TaskRuntimeCheckpoint:
    version = "task-review-runtime-v1"

    def __init__(self, runtime, steps, repository, *, lease=None):
        if not steps or set(runtime._items) != set(steps):
            raise ValueError("runtime step identities mismatch")
        specs = []
        for step in steps.values():
            spec = step.executor.spec.model_dump(mode="json", exclude={"readable_scope_files"})
            if spec["prompt_version"] == "task-review-prompt-v1":
                spec.pop("prompt_version")  # 既有检查点没有这个字段，按原模板恢复。
            if spec["publication_contract"] == "legacy":
                spec.pop("publication_contract")  # 旧检查点不升级发布契约。
            if step.executor.work_item.readable_scope_hash is not None:
                spec.update(readable_scope_hash=step.executor.spec.readable_scope_hash,
                            readable_file_count=len(step.executor.spec.readable_scope_files))
            specs.append(spec)
        if any(spec != specs[0] for spec in specs):
            raise ValueError("runtime requires one frozen run spec")
        budgets = {id(step.budget) for step in steps.values()}
        if len(budgets) != 1:
            raise ValueError("runtime requires one shared run budget")
        self.runtime, self.steps, self.repository, self.lease = runtime, steps, repository, lease
        self.spec = specs[0]
        self.run_id = self.spec["run_id"]
        self.budget = next(iter(steps.values())).budget
        if (self.budget.run_id != self.run_id or self.budget.max_calls != self.spec["max_calls"]
                or self.budget.max_tokens != self.spec["max_tokens"]):
            raise ValueError("runtime frozen budget mismatch")
        if lease and getattr(self.budget.store, "lease", None) != lease:
            raise ValueError("runtime and budget require the same worker lease")
        self._lock = asyncio.Lock()
        self.invalidated = False
        goals = {}
        for step in steps.values():
            for identity, goal in step.executor.goals.items():
                value = goal.model_dump(mode="json")
                if identity in goals and goals[identity] != value:
                    raise ValueError("conflicting frozen goal")
                goals[identity] = value
        self.data = {"version": self.version, "revision": -1, "spec": self.spec,
                     "goals": goals, "calls": {}, "sessions": {},
                     "pages": {identity: json.loads(json.dumps([asdict(page) for page in step.executor.pages]))
                               for identity, step in steps.items()}}
        self.data["derivation_config_hash"] = stable_hash(runtime.derivation.frozen_configuration()) if runtime.derivation else None
        self._committed = deepcopy(self.data)
        runtime.checkpoint = self
        for step in steps.values():
            step.checkpoint = self

    def _ensure_valid(self):
        if self.invalidated:
            raise CoordinationLeaseLost("task runtime requires reload")
        if any(step.budget is not self.budget for step in self.steps.values()):
            raise ValueError("runtime budget instance changed; restore a new checkpoint")

    @staticmethod
    def _session(step):
        executor = step.executor
        projection = executor.projection
        session = {
            "examinations": {k: v.model_dump(mode="json") for k, v in projection.examinations.items()},
            "candidates": {k: v.model_dump(mode="json") for k, v in projection.candidates.items()},
            "leads": [v.model_dump(mode="json") for v in projection.leads],
            "accepted": sorted(projection.accepted_target_ids),
            "pending": sorted(projection.pending_target_ids),
            "evidence": executor.evidence_catalog, "active": sorted(executor.active_evidence_ids),
            "request_id": executor.request_id, "attempt_id": executor.attempt_id,
            "pending_read": step.pending_read.model_dump(mode="json") if step.pending_read else None,
            "repair_errors": step.repair_errors, "store": step.store.model_dump(mode="json"),
            "page_index": executor.page_index,
            "page_examinations": {goal: {page: result.model_dump(mode="json") for page, result in fragments.items()}
                                  for goal, fragments in executor.page_examinations.items()},
            "last_observation": step.last_observation,
            "read_observations": step.read_observations,
            "usages": [usage.model_dump(mode="json") for usage in step.usages],
            "tool_observations": step.tool_observations,
            "dependency_paths": sorted(step.dependency_paths),
            "repository_discovery": step.repository_discovery,
            "investigation": projection.investigation.model_dump(mode="json") if projection.investigation else None,
            "verification": projection.verification.model_dump(mode="json") if projection.verification else None,
            "invalidated_results": projection.invalidated_results,
        }
        if executor.spec.prompt_version in {"task-review-prompt-v5", "task-review-prompt-v6", "task-review-prompt-v7", "task-review-prompt-v8"}:
            session["rejected_items"] = step.rejected_items
        if executor.spec.discussion_manifest_hash:
            # 重放 _apply_verification 时必须以首次请求相同的可见讨论集合校验。
            session["visible_discussions"] = getattr(executor, "visible_discussions", [])
        if executor.spec.stability_contract:
            session["stability"] = deepcopy(step.stability)
        return session

    @staticmethod
    def _restore_session(step, payload):
        executor = step.executor
        executor.projection = TaskProjection(
            examinations={k: ExaminationResult.model_validate(v) for k, v in payload["examinations"].items()},
            candidates={k: Candidate.model_validate(v) for k, v in payload["candidates"].items()},
            leads=[ProposedLead.model_validate(v) for v in payload["leads"]],
            accepted_target_ids=set(payload["accepted"]), pending_target_ids=set(payload["pending"]),
            investigation=((DiscussionInvestigationResult if "resolution" in payload["investigation"]
                else InvestigationResult).model_validate(payload["investigation"]))
                if payload.get("investigation") else None,
            verification=VerificationResult.model_validate(payload["verification"]) if payload.get("verification") else None,
            invalidated_results=payload.get("invalidated_results", []),
        )
        executor.evidence_catalog = payload["evidence"]
        executor.active_evidence_ids = set(payload["active"])
        executor.request_id, executor.attempt_id = payload["request_id"], payload["attempt_id"]
        # 重放校验必须使用与首次请求相同的可见讨论集合；旧检查点没有该字段时保持原行为。
        executor.visible_discussions = payload.get("visible_discussions", [])
        read = payload["pending_read"]
        schema = DiscussionRuntimeReadResponse if read and read.get("tool") == "discussion_read" else RuntimeReadResponse
        step.pending_read = schema.model_validate(read) if read else None
        step.repair_errors = payload["repair_errors"]
        step.rejected_items = payload.get("rejected_items", [])
        step.store = UnitEvidenceStore.model_validate(payload["store"])
        executor.page_index = payload["page_index"]
        executor.page_examinations = {
            goal: {page: ExaminationResult.model_validate(result) for page, result in fragments.items()}
            for goal, fragments in payload["page_examinations"].items()}
        step.last_observation = payload["last_observation"]
        step.read_observations = payload["read_observations"]
        from app.models.review import ModelUsage
        step.usages = [ModelUsage.model_validate(value) for value in payload.get("usages", [])]
        step.tool_observations = payload.get("tool_observations", [])
        step.dependency_paths = set(payload.get("dependency_paths", []))
        step.repository_discovery = payload.get("repository_discovery", True)
        if executor.spec.stability_contract:
            if not isinstance(payload.get("stability"), dict):
                raise ValueError("stability_checkpoint_missing")
            step.stability = deepcopy(payload["stability"])

    async def _put(self, kind, payload):
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        artifact = await asyncio.to_thread(self.repository._artifacts.put_text,
            task_id=self.run_id, kind="task-review-v1-" + kind, content=content)
        return asdict(artifact)

    async def _read(self, reference):
        content = await asyncio.to_thread(self.repository._artifacts.read_text, reference["uri"])
        if (hashlib.sha256(content.encode("utf-8")).hexdigest() != reference["sha256"]
                or len(content.encode("utf-8")) != reference["size_bytes"]):
            raise ValueError("task runtime artifact hash mismatch")
        return json.loads(content)

    async def _persist_locked(self):
        self._ensure_valid()
        before = self.data["revision"]
        payload = deepcopy(self.data)
        runtime = self.runtime
        payload.update(revision=before + 1, spec=self.spec, items=runtime.snapshot(),
            concurrency=runtime.concurrency, cancelled=runtime.cancelled,
            unit_timeout_seconds=runtime.unit_timeout_seconds, unit_deadlines=deepcopy(runtime.unit_deadlines),
            last_unit=runtime._last_unit, class_cursor=deepcopy(runtime._class_cursor),
            obligation_revision=runtime.obligation_revision, served=deepcopy(runtime._served), turn=runtime._turn)
        payload["derived_goals"] = {key: goal.model_dump(mode="json") for step in self.steps.values()
                                    for key, goal in step.executor.goals.items() if goal.origin == "derived"}
        payload["pages"].update({identity: json.loads(json.dumps([asdict(page) for page in step.executor.pages]))
                                for identity, step in self.steps.items()})
        # 在 await 前冻结；正文只写内容寻址 artifact，不进入 SideEffect CAS。
        sessions = {identity: deepcopy(self._session(step)) for identity, step in self.steps.items()}
        derivation_data = deepcopy(runtime.derivation.data) if runtime.derivation is not None else None
        try:
            first = next(iter(self.steps.values())).executor
            if first.work_item.readable_scope_hash is not None and not payload.get("readable_scope"):
                payload["readable_scope"] = await self._put("readable-scope", list(first.spec.readable_scope_files))
            for identity, session in sessions.items():
                payload["sessions"][identity] = await self._put("session", session)
            if derivation_data is not None:
                payload["derivation"] = await self._put("derivation", derivation_data)
            await asyncio.to_thread(self.repository.save_task_review_runtime, self.run_id,
                                    payload, before, self.lease)
        except BaseException:
            self.data = deepcopy(self._committed)
            self.invalidated = True
            raise
        self.data = payload
        self._committed = deepcopy(payload)

    async def persist(self):
        async with self._lock:
            await self._persist_locked()

    async def bind_attempt(self, step, attempt, admission):
        async with self._lock:
            self._ensure_valid()
            self.data["calls"][attempt.attempt_id] = {
                "work_item_id": step.executor.work_item.work_item_id,
                "request_id": attempt.request_id, "attempt_id": attempt.attempt_id,
                "request_hash": admission.get("request_hash"),
                "state": "reserved", "response": None, "applied": False,
            }
            await self._persist_locked()

    async def before_dispatch(self, step, attempt):
        async with self._lock:
            self._ensure_valid()
            if self.runtime.cancelled:
                if self.budget.attempts[attempt.attempt_id].sent:
                    await self.budget.settle(attempt.attempt_id, actual_tokens=None)
                else:
                    await self.budget.release_unsent(attempt.attempt_id, reason="runtime_cancelled")
                raise asyncio.CancelledError()
            self.data["calls"][attempt.attempt_id]["state"] = "dispatch_intent"
            # lease/CAS check immediately precedes budget mark_dispatching and SDK send.
            await self._persist_locked()
            if self.runtime.cancelled:
                # cancel() 可能在持久化 await 期间先设置标记，再等待检查点锁。
                # 发送标记之前可释放；发送标记之后不能证明未发送，保留 unknown。
                if self.budget.attempts[attempt.attempt_id].sent:
                    await self.budget.settle(attempt.attempt_id, actual_tokens=None)
                else:
                    await self.budget.release_unsent(attempt.attempt_id, reason="runtime_cancelled")
                raise asyncio.CancelledError()

    async def save_response(self, step, attempt, raw, usage):
        async with self._lock:
            self._ensure_valid()
            reference = await self._put("response", {"raw": raw, "usage": usage.model_dump(mode="json")})
            call = self.data["calls"][attempt.attempt_id]
            call.update(state="response_received", response=reference)
            await self._persist_locked()

    async def applied(self, step):
        async with self._lock:
            self._ensure_valid()
            self.data["calls"][step.executor.attempt_id].update(state="applied", applied=True)
            await self._persist_locked()

    async def restore(self):
        """预算必须用 recover_running=False 载入，由这里先对账再重建队列。"""
        self._ensure_valid()
        payload = await asyncio.to_thread(self.repository.load_task_review_runtime, self.run_id)
        if not payload:
            await self.persist()
            return
        initial_items = {v["work_item_id"] for v in payload["items"] if not v.get("generation")}
        expected_initial = self.runtime.derivation.initial_ids if self.runtime.derivation else set(self.steps)
        if (payload["version"] != self.version or payload["spec"] != self.spec
                or payload["goals"] != self.data["goals"]
                or initial_items != expected_initial
                or payload.get("unit_timeout_seconds") != self.runtime.unit_timeout_seconds
                or payload.get("derivation_config_hash") != self.data.get("derivation_config_hash")):
            raise ValueError("task runtime frozen manifest mismatch")
        self.data = deepcopy(payload)
        if payload.get("readable_scope"):
            paths = await self._read(payload["readable_scope"])
            if paths != list(next(iter(self.steps.values())).executor.spec.readable_scope_files):
                raise ValueError("task runtime frozen readable scope artifact mismatch")
        self._committed = deepcopy(payload)
        runtime = self.runtime
        runtime.cancelled = payload["cancelled"]
        runtime.concurrency = payload["concurrency"]
        runtime.unit_deadlines = payload.get("unit_deadlines", {})
        runtime._last_unit = payload["last_unit"]
        runtime._class_cursor = payload["class_cursor"]
        runtime._served = payload["served"]
        runtime._turn = payload["turn"]
        runtime.obligation_revision = payload["obligation_revision"]
        runtime._running.clear()
        derived_goals = {key: ReviewGoal.model_validate(value) for key, value in payload.get("derived_goals", {}).items()}
        for raw in payload["items"]:
            if raw["work_item_id"] not in self.steps:
                if runtime.derivation is None:
                    raise ValueError("derived runtime requires derivation adapter")
                runtime.derivation.restore_item(ReviewWorkItem.model_validate(raw), derived_goals)
        if payload.get("derivation"):
            runtime.derivation.data = await self._read(payload["derivation"])
        for raw in payload["items"]:
            item = runtime._items[raw["work_item_id"]]
            if any(raw.get(key) != getattr(item, key) for key in
                   ("goal_ids", "scope_files", "readable_scope_hash", "unit_id", "kind")):
                raise ValueError("task runtime frozen work item mismatch")
            restored = type(item).model_validate(raw)
            for name in type(item).model_fields:
                setattr(item, name, getattr(restored, name))
            self.steps[item.work_item_id].executor.pages = [ContextPage(
                page_id=p["page_id"], ranges=tuple(p["ranges"]), evidence_ids=tuple(p["evidence_ids"]))
                for p in payload["pages"][item.work_item_id]]
            await self._restore_step(item.work_item_id)
        # reserve 与 bind checkpoint 之间崩溃的 orphan 也必须对账。
        for attempt in list(self.budget.attempts.values()):
            if attempt.operation != "task_review_v1" or attempt.attempt_id in self.data["calls"]:
                continue
            if attempt.task_id in self.steps:
                if attempt.state == AttemptState.RESERVED and not attempt.sent:
                    await self.budget.release_unsent(attempt.attempt_id, reason="recovered_before_dispatch")
                elif not attempt.settled:
                    await self.budget.settle(attempt.attempt_id, actual_tokens=None)
                    item = runtime._items[attempt.task_id]
                    item.state = type(item.state)("unknown")
        if runtime.derivation is not None and not runtime.cancelled:
            runtime.derivation.refresh()
        await self.persist()

    async def _restore_step(self, identity):
        step = self.steps[identity]
        item = step.executor.work_item
        # 缺失 session 无法重建证据/合法历史，明确失败，不能默认为空结果重发。
        try:
            self._restore_session(step, await self._read(self.data["sessions"][identity]))
        except (OSError, ValueError, KeyError):
            item.state = type(item.state)("result_missing")
            item.reason = "session_artifact_missing_or_invalid"
            raise ValueError(item.reason)
        calls = [call for call in self.data["calls"].values() if call["work_item_id"] == identity]
        # 检查点 JSON 会按键排序；UUID 字典次序不是 attempt 的执行次序。
        calls.sort(key=lambda call: (self.budget.attempts[call["attempt_id"]].created_at
                                    if call["attempt_id"] in self.budget.attempts else "", call["attempt_id"]))
        missing_results = []
        available_applied = []
        for call in calls:
            record = self.budget.attempts.get(call["attempt_id"])
            if record is None or record.request_id != call["request_id"]:
                item.state = type(item.state)("unknown")
                item.reason = "attempt_ledger_inconsistent"
                return
            if record.state == AttemptState.RESERVED and not record.sent:
                # Provider invariant: dispatch marker is durably written before SDK invocation.
                await self.budget.release_unsent(record.attempt_id, reason="recovered_before_dispatch")
            response = None
            if call.get("response"):
                try:
                    response = await self._read(call["response"])
                except (OSError, ValueError):
                    call["lost_response"] = call.pop("response")
            if response is not None:
                usage = response["usage"]
                # ModelUsage actual total can be supplied by input/output even without total_tokens.
                from app.models.review import ModelUsage
                parsed_usage = ModelUsage.model_validate(usage)
                actual = parsed_usage.actual_total_tokens
                await self.budget.reconcile_unknown(record.attempt_id, actual_tokens=actual,
                                                    cost_microusd=parsed_usage.cost_microusd)
                if not any(value.id == parsed_usage.id for value in step.usages):
                    step.usages.append(parsed_usage)
                if not call["applied"]:
                    step.executor.request_id, step.executor.attempt_id = record.request_id, record.attempt_id
                    step.last_response = response["raw"]
                    step.repair_errors = []
                    step.apply_response(response["raw"])
                    # 在线路径在检查点写盘前登记；重放补齐崩溃窗口内的同口径统计。
                    await step.record_discussion_usage(response["raw"])
                    call.update(applied=True, state="applied")
                if call["applied"]:
                    available_applied.append(call)
            elif record.sent and not record.usage_available:
                if not record.settled:
                    await self.budget.settle(record.attempt_id, actual_tokens=None)
                # 有响应的后续传输 attempt 可以继续完成，同一个旧 attempt 不重发。
                if call is calls[-1]:
                    item.state = type(item.state)("unknown")
                    item.reason = "usage_unknown"
                    return
            elif record.sent and record.usage_available and not call["applied"]:
                missing_results.append(call)
        if missing_results:
            # 只有同一工作项后续已应用且可读取的成功结果覆盖全部缺口，才替代旧结果缺失。
            # 旧请求保持 applied=false 和原费用；不是把历史失败改为成功。
            projection = step.executor.projection
            complete = (not projection.pending_target_ids and not step.pending_read and not step.repair_errors
                        and not step.last_observation.get("errors"))
            if item.kind.value == "inspect":
                complete = complete and all(key in projection.accepted_target_ids for key in item.goal_ids)
            elif item.kind.value == "investigate":
                complete = complete and projection.investigation is not None
            elif item.kind.value == "verify":
                complete = complete and projection.verification is not None
            replacement = available_applied[-1] if available_applied else None
            for missing in missing_results:
                if (not complete or replacement is None
                        or calls.index(replacement) <= calls.index(missing)):
                    item.state = type(item.state)("result_missing")
                    item.reason = "response_artifact_missing"
                    return
                missing["superseded_by_attempt_id"] = replacement["attempt_id"]
            if item.reason == "response_artifact_missing":
                item.reason = None
                item.state = type(item.state)("completed")
        if item.state.value == "running":
            pending = bool(step.executor.projection.pending_target_ids)
            if (step.pending_read or step.repair_errors or step.last_observation.get("page_advanced")
                    or step.last_observation.get("action") == "tool"):
                state = "ready"
            elif (step.executor.spec.stability_contract and any(
                    error.get("reason") == "finalization_requires_submit"
                    for error in step.last_observation.get("errors", []))):
                state = "waiting_evidence"
                item.reason = "finalization_requires_submit"
            elif not pending:
                state = "completed"
            elif not calls or self.budget.attempts[calls[-1]["attempt_id"]].state == AttemptState.RELEASED:
                state = "ready"
            else:
                state = "failed"
                # 与在线推进保持同一诊断口径：模型输出未被接受而非执行故障。
                item.reason = "model_response_rejected"
            item.state = type(item.state)("cancelled" if self.runtime.cancelled and state == "ready" else state)
