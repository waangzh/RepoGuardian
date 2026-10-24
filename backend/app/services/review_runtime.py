"""task_review_v1 唯一工作队列：确定性轮转、期限与预算暂缓恢复。"""

from __future__ import annotations

from typing import Any
import asyncio
import time


class ExperimentalUnitDeadline(TimeoutError):
    """仅用于评测的冻结 Unit 生命周期消融。"""


class EvidenceIdentityConflict(ValueError):
    """同一证据身份绑定了不一致的源码或快照。"""


class FrozenRound:
    """Reserve in queue order, settle after admission, apply in queue order."""

    def __init__(self, items):
        self.order = [item.work_item_id for item in items]
        self.admitted = {key: asyncio.Event() for key in self.order}
        self.finished = {key: asyncio.Event() for key in self.order}

    async def admission_turn(self, identity):
        index = self.order.index(identity)
        if index:
            await self.admitted[self.order[index - 1]].wait()

    def admit(self, identity):
        self.admitted[identity].set()

    async def before_settlement(self):
        await asyncio.gather(*(event.wait() for event in self.admitted.values()))

    async def application_turn(self, identity):
        index = self.order.index(identity)
        if index:
            await self.finished[self.order[index - 1]].wait()


class TaskReviewRuntime:
    """唯一工作队列；Unit 公平轮转，每个任务一次只推进一个动作。"""

    engine_version = "task_review_v1"

    def __init__(self, work_items: list[Any] | None = None, *, concurrency: int = 1,
                 unit_timeout_seconds: float | None = None) -> None:
        if not 1 <= concurrency <= 32:
            raise ValueError("invalid runtime concurrency")
        self.concurrency = concurrency
        if unit_timeout_seconds is not None and unit_timeout_seconds <= 0:
            raise ValueError("invalid unit deadline")
        self.unit_timeout_seconds = unit_timeout_seconds
        self.unit_deadlines: dict[str, float] = {}
        self._items: dict[str, Any] = {}
        self._running: set[str] = set()
        self._last_unit: str | None = None
        self._class_cursor: dict[str, int] = {}
        self._served: dict[str, int] = {}
        self._turn = 0
        self.cancelled = False
        self.obligation_revision = 0
        self.checkpoint = None
        self._round_lock = asyncio.Lock()
        self.derivation = None
        self.progress = None
        self.failures: list[dict] = []
        for item in work_items or []:
            self.enqueue(item)

    def enqueue(self, item: Any) -> None:
        item_id = getattr(item, "work_item_id", None)
        if not item_id or item_id in self._items:
            raise ValueError("duplicate or missing task_review_v1 work item")
        self._items[item_id] = item

    def ready(self) -> list[Any]:
        return sorted((item for item in self._items.values()
                       if getattr(item, "state", None).value == "ready"),
                       key=lambda item: (item.sequence, item.work_item_id))

    @staticmethod
    def _unit(item: Any) -> str:
        return item.unit_id or item.work_item_id

    def claim_next(self) -> Any | None:
        if self.cancelled:
            return None
        if len(self._running) >= self.concurrency:
            raise RuntimeError("task_review_v1 single writer capacity already running")
        items = self.ready()
        if not items:
            return None
        units = list(dict.fromkeys(self._unit(item) for item in items))
        if self._last_unit in units:
            offset = units.index(self._last_unit) + 1
            units = units[offset:] + units[:offset]
        unit = units[0]
        group = [item for item in items if self._unit(item) == unit]
        # 新调查不能抢过同组尚未执行过的基础检查；其他三类仍有界轮转。
        if any(item.scheduling_class == "basic" and not item.started for item in group):
            group = [item for item in group if item.scheduling_class != "investigation"]
        classes = ("basic", "continuation", "verification", "investigation")
        cursor = self._class_cursor.get(unit, 0)
        for offset in range(len(classes)):
            position = (cursor + offset) % len(classes)
            eligible = [item for item in group if item.scheduling_class == classes[position]]
            if eligible:
                item = min(eligible, key=lambda value: (
                    self._served.get(value.work_item_id, -1), value.sequence, value.work_item_id))
                self._class_cursor[unit] = (position + 1) % len(classes)
                break
        self._last_unit = unit
        self._turn += 1
        self._served[item.work_item_id] = self._turn
        item.state = type(item.state)("running")
        item.attempt_count += 1
        item.started = True
        self._running.add(item.work_item_id)
        return item

    def finish(self, work_item_id: str, *, state: str = "completed") -> Any:
        if work_item_id not in self._running:
            raise RuntimeError("work item is not owned by runtime writer")
        item = self._items[work_item_id]
        item.state = type(item.state)(state)
        item.revision += 1
        self._running.remove(work_item_id)
        return item

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                for item in sorted(self._items.values(), key=lambda value: value.work_item_id)]

    async def advance_one(self, steps: dict[str, Any], *, claimed=None, barrier=None) -> dict[str, Any] | None:
        """领取后持久化，工具/模型推进后保存结果，再更新队列。"""
        item = claimed if claimed is not None else self.claim_next()
        if item is None:
            return None
        step = steps.get(item.work_item_id)
        if step is None or step.executor.work_item is not item:
            self.finish(item.work_item_id, state="failed")
            raise ValueError("work_item_executor_binding_mismatch")
        step.runtime = self
        step.round_barrier = barrier
        try:
            remaining = None
            if self.unit_timeout_seconds is not None:
                deadline = self.unit_deadlines.setdefault(self._unit(item), time.time() + self.unit_timeout_seconds)
                remaining = deadline - time.time()
            if self.checkpoint is not None:
                await self.checkpoint.persist()
            if remaining is not None:
                if remaining <= 0:
                    raise ExperimentalUnitDeadline()
                timer = asyncio.timeout(remaining)
                try:
                    async with timer:
                        observation = await step.advance()
                except TimeoutError:
                    if timer.expired():
                        raise ExperimentalUnitDeadline() from None
                    raise
            else:
                observation = await step.advance()
        except ExperimentalUnitDeadline:
            if barrier:
                barrier.admit(item.work_item_id)
                await barrier.application_turn(item.work_item_id)
            self.finish(item.work_item_id, state="cancelled")
            item.reason = "experiment_unit_deadline"
            if self.checkpoint is not None:
                await self.checkpoint.persist()
            return {"action": "deadline", "reason": item.reason}
        except asyncio.CancelledError:
            self.finish(item.work_item_id, state="cancelled")
            # lease lost 不允许再尝试写回；合法恢复者负责在途请求对账。
            raise
        except Exception as exc:
            if barrier:
                barrier.admit(item.work_item_id)
                await barrier.application_turn(item.work_item_id)
            rejection = getattr(exc, "admission_rejection", {})
            if rejection.get("kind") == "budget" and rejection.get("can_defer"):
                item.last_checked_budget_revision = step.budget.revision
                item.last_checked_obligation_revision = self.obligation_revision
                item.reason = rejection.get("reason", "budget_rejected")
                self.finish(item.work_item_id, state="deferred_budget")
                if self.checkpoint is not None:
                    await self.checkpoint.persist()
                return {"action": "deferred", "reason": item.reason}
            attempts = [record for record in step.budget.attempts.values()
                        if record.request_id == step.executor.request_id]
            state = "unknown" if any(record.sent and not record.usage_available for record in attempts) else "failed"
            self.finish(item.work_item_id, state=state)
            item.reason = state
            if self.checkpoint is not None and not self.checkpoint.invalidated:
                await self.checkpoint.persist()
            raise
        if barrier:
            barrier.admit(item.work_item_id)
            await barrier.application_turn(item.work_item_id)
        pending = bool(step.executor.projection.pending_target_ids)
        if self.cancelled:
            state = "cancelled"
        elif (step.pending_read or step.repair_errors or observation.get("action") == "tool"
              or observation.get("page_advanced")):
            state = "ready"
        elif any(error.get("reason") in {"repeated_read_no_progress", "finalization_requires_submit"}
                 for error in observation.get("errors", [])):
            state = "waiting_evidence"
            item.reason = next(error["reason"] for error in observation["errors"]
                               if error.get("reason") in {"repeated_read_no_progress", "finalization_requires_submit"})
        elif observation.get("guarded_unresolved") == "citation_coverage_incomplete" and not pending:
            state = "completed"
            item.reason = "citation_coverage_incomplete"
        elif observation.get("errors") or pending:
            state = "failed"
            # 纠错机会已用尽仍被拒绝：区分“模型输出未通过验收”和执行故障。
            item.reason = "model_response_rejected"
        else:
            state = "completed"
        if state == "ready":
            item.scheduling_class = "continuation"
        self.finish(item.work_item_id, state=state)
        if (getattr(getattr(step.executor, "spec", None), "stability_contract", None)
                == "review-stability-v2" and state in {"completed", "failed", "cancelled"}):
            for obligation in list(step.budget.obligations.values()):
                if (obligation.owner_task_id == item.work_item_id
                        and obligation.reason == "initial_inspect_closing" and obligation.state.value == "active"):
                    await step.budget.release_obligation(obligation.obligation_id, abandoned=state != "completed")
        if self.derivation is not None and not self.cancelled and barrier is None:
            self.derivation.refresh()
        if self.checkpoint is not None:
            await self.checkpoint.persist()
        if self.progress is not None:
            self.progress()
        return observation

    def wake_deferred(self, budget_revision: int) -> int:
        if self.cancelled:
            return 0
        count = 0
        for item in self._items.values():
            if (item.state.value == "deferred_budget" and
                    (item.last_checked_budget_revision != budget_revision or
                     item.last_checked_obligation_revision != self.obligation_revision)):
                item.state = type(item.state)("ready")
                count += 1
        return count

    async def advance_round(self, steps: dict[str, Any]) -> list[Any]:
        """有界并发一轮；没有 Unit/Verifier 的私有 drain。"""
        if self._round_lock.locked():
            return []
        async with self._round_lock:
            return await self._advance_round(steps)

    async def _advance_round(self, steps: dict[str, Any]) -> list[Any]:
        if self.cancelled:
            return []
        budgets = {id(step.budget): step.budget for step in steps.values()}
        if len(budgets) != 1:
            raise ValueError("task runtime requires one shared run budget")
        budget = next(iter(budgets.values()))
        self.wake_deferred(budget.revision)
        count = min(len(self.ready()), self.concurrency - len(self._running))
        stable = any(getattr(getattr(step.executor, "spec", None), "stability_contract", None)
                     == "review-stability-v2" for step in steps.values())
        if stable:
            for item in sorted(self._items.values(), key=lambda value: (value.sequence, value.work_item_id)):
                if item.state.value == "ready":
                    await steps[item.work_item_id].protect_closing()
            # Freeze membership before any network completion can derive new work.
            items = [self.claim_next() for _ in range(count)]
            barrier = FrozenRound(items)
            budget.round_barrier = barrier

            async def advance(item):
                identity = item.work_item_id
                try:
                    await barrier.admission_turn(identity)
                    return await self.advance_one(steps, claimed=item, barrier=barrier)
                finally:
                    barrier.admit(identity)
                    barrier.finished[identity].set()
                    steps[identity].round_barrier = None

            try:
                results = await asyncio.gather(*(advance(item) for item in items), return_exceptions=True)
            finally:
                budget.round_barrier = None
            if self.derivation is not None and not self.cancelled:
                self.derivation.refresh()
            if self.checkpoint is not None:
                await self.checkpoint.persist()
        else:
            results = await asyncio.gather(*(self.advance_one(steps) for _ in range(count)), return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                self.record_failure(result, stage="task_advance")
        return [result for result in results if result is not None]

    def record_failure(self, exc: Exception, *, stage: str) -> None:
        # 不导出异常文本、局部变量或源码，避免泄漏外部输入/凭据。
        frames = []
        trace = exc.__traceback__
        while trace is not None:
            frames.append({"function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
        failure = {"stage": stage, "error_type": type(exc).__name__, "frames": frames,
                   "detail": "exception_text_omitted_for_safety"}
        from app.agents.providers import LLMProviderError, provider_failure_info
        if isinstance(exc, LLMProviderError):
            failure["provider_failure"] = provider_failure_info(exc)
            if exc.work_item_id in self._items:
                failure["work_item_id"] = exc.work_item_id
        else:
            failure["category"] = ("evidence_identity_conflict" if isinstance(exc, EvidenceIdentityConflict)
                                   else "runtime_internal_error")
        self.failures.append(failure)

    async def run(self, steps: dict[str, Any], *, deadline_seconds: float) -> str:
        """运行期限作用于唯一队列，不复制旧 Unit 生命周期。"""
        if deadline_seconds <= 0:
            raise ValueError("runtime deadline must be positive")
        try:
            async with asyncio.timeout(deadline_seconds):
                while not self.cancelled:
                    results = await self.advance_round(steps)
                    if any(isinstance(result, Exception) for result in results):
                        budget = next(iter(steps.values())).budget
                        for obligation in list(budget.obligations.values()):
                            if obligation.state.value == "active":
                                await budget.release_obligation(obligation.obligation_id, abandoned=True)
                        return "technical_failure"
                    if results:
                        continue
                    budget = next(iter(steps.values())).budget
                    released = False
                    # 只释放已经失去合法后续动作的保护，不抹掉未知请求预留。
                    for obligation in list(budget.obligations.values()):
                        owner = self._items.get(obligation.owner_task_id)
                        if (obligation.state.value == "active" and owner is not None
                                and owner.state.value in {"completed", "failed", "cancelled"}):
                            await budget.release_obligation(obligation.obligation_id, abandoned=True)
                            released = True
                    if released and self.wake_deferred(budget.revision):
                        continue
                    states = {item.state.value for item in self._items.values()}
                    if "deferred_budget" in states:
                        return "budget_exhausted"
                    if any(item.reason == "experiment_unit_deadline" for item in self._items.values()):
                        return "experiment_unit_deadline"
                    if states <= {"completed"}:
                        return "plan_exhausted"
                    if states & {"unknown", "result_missing"}:
                        return "technical_failure"
                    if "failed" in states:
                        rejected = [item for item in self._items.values()
                                    if item.state.value == "failed" and item.reason == "model_response_rejected"]
                        failed = [item for item in self._items.values() if item.state.value == "failed"]
                        # 全部失败项都只是模型输出未通过验收时，保留可诊断的终止原因。
                        if failed and len(rejected) == len(failed):
                            return "model_response_rejected"
                        return "technical_failure"
                    return "no_progress"
                return "cancelled"
        except TimeoutError:
            await self.cancel(steps)
            return "deadline"

    async def cancel(self, steps: dict[str, Any]) -> None:
        self.cancelled = True
        for item in self._items.values():
            if item.work_item_id not in self._running and item.state.value not in {
                    "completed", "unknown", "result_missing"}:
                item.state = type(item.state)("cancelled")
        budgets = {id(step.budget): step.budget for step in steps.values()}
        for budget in budgets.values():
            for obligation in list(budget.obligations.values()):
                if obligation.state.value == "active":
                    await budget.release_obligation(obligation.obligation_id, abandoned=True)
        if self.checkpoint is not None:
            await self.checkpoint.persist()

    async def retry_unit(self, unit_id: str, steps: dict[str, Any]) -> list[str]:
        if self.cancelled or self._running:
            raise RuntimeError("retry requires an inactive, uncancelled runtime")
        retried = []
        for item in self._items.values():
            if self._unit(item) != unit_id or item.state.value == "unknown":
                continue
            projection = steps[item.work_item_id].executor.projection
            step = steps[item.work_item_id]
            if (step.executor.spec.stability_contract and item.kind.value == "investigate"
                    and (projection.investigation is None
                         or projection.investigation.answer.value == "insufficient_evidence")):
                if projection.investigation is not None:
                    projection.invalidated_results.append({
                        "kind": "investigation", "result": projection.investigation.model_dump(mode="json"),
                        "reason": "explicit_investigation_retry"})
                projection.investigation = None
                projection.pending_target_ids.update(item.goal_ids)
                step.stability["attempted_reads"] = []
            blocked = {key for key, result in projection.examinations.items()
                       if result.disposition.value == "blocked"}
            gaps = projection.pending_target_ids | blocked
            if not gaps:
                continue
            for key in blocked:
                del projection.examinations[key]
                projection.accepted_target_ids.discard(key)
            projection.pending_target_ids.update(gaps)
            executor = steps[item.work_item_id].executor
            if executor.pages:
                executor.page_index = next((index for index, page in enumerate(executor.pages)
                    if any(value["goal_id"] in gaps and page.page_id not in
                           executor.page_examinations.get(value["goal_id"], {}) for value in page.ranges)),
                    executor.page_index)
                executor.active_evidence_ids = set(executor.pages[executor.page_index].evidence_ids)
            item.state = type(item.state)("ready")
            item.reason = None
            item.scheduling_class = "continuation"
            retried.append(item.work_item_id)
        if self.checkpoint is not None:
            await self.checkpoint.persist()
        return retried
