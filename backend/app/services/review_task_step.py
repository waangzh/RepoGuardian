"""P1 单 inspect 垂直切片；一次推进仅发送请求或执行一次只读动作。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from app.models.review import ReviewToolScope, UnitEvidenceStore
from app.models.review_runtime import RuntimeReadResponse
from app.services.evidence_store import put_chunks, store_chunks, evidence_snapshot
from app.services.review_run_budget import (
    ReviewRunBudget, activate_run_budget, run_budget_attempt_observer,
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

    async def advance(self) -> dict:
        if self._lock.locked():
            raise RuntimeError("work_item_already_running")
        async with self._lock:
            if self.pending_read is not None:
                return await self._read()
            executor = self.executor
            request = self.protocol.prepare(executor.work_item, list(executor.goals.values()),
                active_evidence_ids=executor.active_evidence_ids,
                evidence_catalog=executor.evidence_catalog)
            if self.repair_errors:
                import json
                from dataclasses import replace
                request = replace(request, prompt=request.prompt + "\n" + json.dumps({
                    "correction": self.repair_errors,
                    "instruction": "Only resubmit rejected items; accepted results are stored by server.",
                }, ensure_ascii=False))
                self.repair_errors = []

            async def bind_attempt(attempt, admission):
                executor.request_id = attempt.request_id
                executor.attempt_id = attempt.attempt_id

            observer = run_budget_attempt_observer.set(bind_attempt)
            try:
                with activate_run_budget(self.budget):
                    result = await self.provider.task_review_step(request)
            finally:
                run_budget_attempt_observer.reset(observer)
            # Provider has settled usage before JSON parse, including malformed responses.
            self.last_response = result.value
            self.usages.append(result.usage)
            parsed = executor.parse_and_apply(result.value)
            if isinstance(parsed.response, RuntimeReadResponse) and not parsed.errors:
                self.pending_read = parsed.response
            schema_errors = [asdict(error) for error in parsed.errors
                             if error.reason in {"invalid_json", "invalid_response_kind", "unknown_submit_field"}
                             or "validation error" in error.reason
                             or error.reason == "field_must_be_array"]
            if schema_errors and executor.work_item.format_corrections < 1:
                executor.work_item.format_corrections += 1
                self.repair_errors = schema_errors
            return {"action": "model", "errors": [asdict(error) for error in parsed.errors],
                    "pending_goals": sorted(executor.projection.pending_target_ids),
                    "correction_pending": bool(self.repair_errors)}

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
            executor.active_evidence_ids.add(identity)
        return {"action": "tool", "status": "completed", "evidence_ids": sorted(chunks)}
