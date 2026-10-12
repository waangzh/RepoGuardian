"""P1 单 inspect 垂直切片；一次推进仅发送请求或执行一次只读动作。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

from app.models.review import ReviewToolScope, UnitEvidenceStore
from app.models.review_runtime import RuntimeReadResponse
from app.services.evidence_store import put_chunks, store_chunks, evidence_snapshot
from app.services.review_run_budget import (
    ReviewRunBudget, activate_run_budget, run_budget_attempt_observer,
    run_budget_dispatch_guard, run_budget_response_observer,
    run_budget_task_id,
)
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.tools.context_files import ScopedContextTool
from app.tools.git_tool import GitTool


class InspectTaskStep:
    """仅离线切片入口；复用现有 scope、EvidenceStore 和唯一 RunBudget。"""

    def __init__(self, executor: ReviewTaskExecutor, provider, budget: ReviewRunBudget,
                 scope: ReviewToolScope, *, git_tool=None, context_tool=None):
        spec = executor.spec
        if (budget.run_id != spec.run_id or budget.max_calls != spec.max_calls
                or budget.max_tokens != spec.max_tokens):
            raise ValueError("frozen_run_budget_mismatch")
        if (not getattr(provider, "supports_task_review_v1", False)
                or provider._provider_name != spec.model_provider):
            raise ValueError("task_review_v1_provider_unavailable")
        if scope.review_unit_id != executor.work_item.work_item_id:
            raise ValueError("work_item_scope_identity_mismatch")
        if set(executor.work_item.scope_files) != set(scope.readable_files):
            raise ValueError("work_item_scope_mismatch")
        self.executor = executor
        self.provider = provider
        self.budget = budget
        self.scope = scope
        self.git = git_tool or GitTool()
        self.tools = context_tool or ScopedContextTool()
        self.protocol = TaskReviewV1Protocol(spec)
        self.store = UnitEvidenceStore(review_unit_id=scope.review_unit_id,
                                      snapshot=evidence_snapshot(spec.snapshot_head_sha,
                                                                 spec.snapshot_base_sha))
        self.pending_read: RuntimeReadResponse | None = None
        self.repair_errors: list[dict] = []
        self.last_response: str | None = None
        self.usages = []
        self._lock = asyncio.Lock()
        self.checkpoint = None
        self.last_observation: dict = {}
        self.runtime = None
        self.read_observations: dict[str, dict] = {}

    async def advance(self) -> dict:
        if self._lock.locked():
            raise RuntimeError("work_item_already_running")
        async with self._lock:
            if self.pending_read is not None:
                read_key = json.dumps(self.pending_read.arguments.model_dump(), sort_keys=True)
                observation = await self._read()
                fingerprint = json.dumps(observation, sort_keys=True)
                previous = self.read_observations.get(read_key, {})
                repeats = previous.get("repeats", 0) + 1 if previous.get("fingerprint") == fingerprint else 1
                self.read_observations[read_key] = {"fingerprint": fingerprint, "repeats": repeats}
                self.last_observation = observation
                if self.checkpoint is not None:
                    await self.checkpoint.persist()
                return observation
            executor = self.executor
            current_goals = executor.current_goals()
            pending_item = executor.work_item.model_copy(update={
                "goal_ids": list(dict.fromkeys(goal.goal_id for goal in current_goals))})
            # 合法历史仅保存在服务端，续查不要求重写已经完成的目标。
            if not pending_item.goal_ids and not self.repair_errors:
                return {"action": "idle", "pending_goals": []}
            request = self.protocol.prepare(pending_item, current_goals,
                active_evidence_ids=executor.active_evidence_ids,
                evidence_catalog=executor.evidence_catalog)
            if self.repair_errors:
                from dataclasses import replace
                request = replace(request, prompt=request.prompt + "\n" + json.dumps({
                    "correction": self.repair_errors,
                    "instruction": "Only resubmit rejected items; accepted results are stored by server.",
                }, ensure_ascii=False))

            async def bind_attempt(attempt, admission):
                executor.request_id = attempt.request_id
                executor.attempt_id = attempt.attempt_id
                if self.checkpoint is not None:
                    await self.checkpoint.bind_attempt(self, attempt, admission)

            async def guard(attempt):
                if self.checkpoint is not None:
                    await self.checkpoint.before_dispatch(self, attempt)
                elif self.runtime is not None and self.runtime.cancelled:
                    if self.budget.attempts[attempt.attempt_id].sent:
                        await self.budget.settle(attempt.attempt_id, actual_tokens=None)
                    else:
                        await self.budget.release_unsent(attempt.attempt_id, reason="runtime_cancelled")
                    raise asyncio.CancelledError()

            async def save_response(attempt, raw, usage):
                if self.checkpoint is not None:
                    await self.checkpoint.save_response(self, attempt, raw, usage)

            observer = run_budget_attempt_observer.set(bind_attempt)
            dispatch_token = run_budget_dispatch_guard.set(guard)
            response_token = run_budget_response_observer.set(save_response)
            task_token = run_budget_task_id.set(executor.work_item.work_item_id)
            try:
                with activate_run_budget(self.budget):
                    result = await self.provider.task_review_step(request)
            finally:
                run_budget_attempt_observer.reset(observer)
                run_budget_dispatch_guard.reset(dispatch_token)
                run_budget_response_observer.reset(response_token)
                run_budget_task_id.reset(task_token)
            # Provider has settled usage before JSON parse, including malformed responses.
            self.last_response = result.value
            self.usages.append(result.usage)
            self.repair_errors = []
            observation = self.apply_response(result.value)
            if self.checkpoint is not None:
                await self.checkpoint.applied(self)
            return observation

    def apply_response(self, raw: str) -> dict:
        """恢复重放与在线结果使用同一解析/纠错入口。"""
        executor = self.executor
        parsed = executor.parse_and_apply(raw)
        if isinstance(parsed.response, RuntimeReadResponse) and not parsed.errors:
            read_key = json.dumps(parsed.response.arguments.model_dump(), sort_keys=True)
            if self.read_observations.get(read_key, {}).get("repeats", 0) >= 2:
                from app.services.review_task_executor import RuntimeParseError
                parsed.errors.append(RuntimeParseError("read", None, "repeated_read_no_progress"))
            else:
                self.pending_read = parsed.response
        schema_errors = [asdict(error) for error in parsed.errors
                         if error.reason in {"invalid_json", "invalid_response_kind", "unknown_submit_field"}
                         or "validation error" in error.reason
                         or error.reason == "field_must_be_array"]
        if schema_errors and executor.work_item.format_corrections < 1:
            executor.work_item.format_corrections += 1
            self.repair_errors = schema_errors
        page_advanced = executor.advance_page()
        self.last_observation = {"action": "model", "errors": [asdict(error) for error in parsed.errors],
                "pending_goals": sorted(executor.projection.pending_target_ids),
                "correction_pending": bool(self.repair_errors), "page_advanced": page_advanced}
        return self.last_observation

    async def _read(self) -> dict:
        read = self.pending_read
        self.pending_read = None
        executor = self.executor
        args = read.arguments
        if args.end_line - args.start_line + 1 > self.scope.max_lines_per_read:
            return {"action": "tool", "status": "rejected", "reason": "read_range_exceeds_scope"}
        actual_head = await asyncio.to_thread(self.git._run,
            [self.git._git, "-C", self.scope.repository_root, "rev-parse", "HEAD"])
        if actual_head.strip() != executor.spec.snapshot_head_sha:
            return {"action": "tool", "status": "rejected", "reason": "stale_repository_snapshot"}
        try:
            chunk = await self.tools.file_read(scope=self.scope, **args.model_dump())
        except (ValueError, OSError):
            return {"action": "tool", "status": "rejected", "reason": "controlled_read_rejected"}
        # 固定快照树与返回原文逐字核对；dirty head/symlink/路径拒绝不归档。
        content = await asyncio.to_thread(self.git.get_file_content_at_revision,
            self.scope.repository_root, executor.spec.snapshot_head_sha, args.file_path)
        if content is None or chunk.get("truncated") or not chunk.get("content"):
            return {"action": "tool", "status": "blocked", "reason": "source_unavailable_or_truncated"}
        expected = "\n".join(content.splitlines()[chunk["start_line"] - 1:chunk["end_line"]])
        if chunk["content"].rstrip("\r\n") != expected:
            return {"action": "tool", "status": "rejected", "reason": "snapshot_text_mismatch"}
        put_chunks(self.store, [chunk])
        chunks = store_chunks(self.store)
        for identity, item in chunks.items():
            executor.evidence_catalog[identity] = {**item, "file_path": item["file"]}
        visible = {identity for identity, item in chunks.items()
                   if item["file"] == args.file_path and item["start_line"] == chunk["start_line"]
                   and item["end_line"] == chunk["end_line"] and item["content"] == chunk["content"]}
        executor.active_evidence_ids.update(visible)
        return {"action": "tool", "status": "completed", "evidence_ids": sorted(visible)}
