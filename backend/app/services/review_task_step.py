"""P1 单 inspect 垂直切片；一次推进仅发送请求或执行一次只读动作。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

from app.models.review import ReviewToolScope, UnitEvidenceStore, ContextRetrievalPlan, ChangedFile
from app.agents.providers import LLMProviderError, provider_failure_info
from app.models.review_runtime import RuntimeReadResponse
from app.services.evidence_store import put_chunks, store_chunks, evidence_snapshot
from app.services.review_input_context import build_context_evidence
from app.services.review_run_budget import (
    ReviewRunBudget, activate_run_budget, run_budget_attempt_observer,
    run_budget_dispatch_guard, run_budget_response_observer,
    run_budget_task_id,
)
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.tools.context_files import ScopedContextTool
from app.tools.git_tool import GitTool
from app.tools.code_search import CodeSearchTool
from app.review.run_audit import emit_run_audit


class InspectTaskStep:
    """仅离线切片入口；复用现有 scope、EvidenceStore 和唯一 RunBudget。"""

    def __init__(self, executor: ReviewTaskExecutor, provider, budget: ReviewRunBudget,
                 scope: ReviewToolScope, *, git_tool=None, context_tool=None, search_tool=None,
                 changed_files=None, file_index=None, symbol_index=None, repository_graph=None):
        spec = executor.spec
        if (budget.run_id != spec.run_id or budget.max_calls != spec.max_calls
                or budget.max_tokens != spec.max_tokens):
            raise ValueError("frozen_run_budget_mismatch")
        if (not getattr(provider, "supports_task_review_v1", False)
                or provider._provider_name != spec.model_provider):
            raise ValueError("task_review_v1_provider_unavailable")
        if scope.review_unit_id != executor.work_item.work_item_id:
            raise ValueError("work_item_scope_identity_mismatch")
        if executor.readable_files != set(scope.readable_files):
            raise ValueError("work_item_scope_mismatch")
        self.executor = executor
        self.provider = provider
        self.budget = budget
        self.scope = scope
        self.git = git_tool or GitTool()
        self.tools = context_tool or ScopedContextTool()
        self.search_tool = search_tool or CodeSearchTool()
        self.changed_files = changed_files or []
        self.file_index = file_index or []
        self.symbol_index = symbol_index or []
        self.repository_graph = repository_graph or {}
        self.protocol = TaskReviewV1Protocol(spec)
        self.store = UnitEvidenceStore(review_unit_id=scope.review_unit_id,
                                      snapshot=evidence_snapshot(spec.snapshot_head_sha,
                                                                 spec.snapshot_base_sha))
        self.pending_read: RuntimeReadResponse | None = None
        self.repair_errors: list[dict] = []
        self.rejected_items: list[dict] = []
        self.last_response: str | None = None
        self.usages = []
        self._lock = asyncio.Lock()
        self.checkpoint = None
        self.last_observation: dict = {}
        self.runtime = None
        self.tool_observations: list[dict] = []
        self.read_observations: dict[str, dict] = {}

    async def advance(self) -> dict:
        if self._lock.locked():
            raise RuntimeError("work_item_already_running")
        async with self._lock:
            if self.pending_read is not None:
                read = self.pending_read
                read_key = json.dumps([self.pending_read.tool, self.pending_read.arguments.model_dump()], sort_keys=True)
                observation = await self._read()
                emit_run_audit("tool", operation=read.tool, work_item_id=self.executor.work_item.work_item_id,
                    arguments=read.arguments.model_dump(mode="json"), observation=observation)
                fingerprint = json.dumps(observation, sort_keys=True)
                previous = self.read_observations.get(read_key, {})
                repeats = previous.get("repeats", 0) + 1 if previous.get("fingerprint") == fingerprint else 1
                self.read_observations[read_key] = {"fingerprint": fingerprint, "repeats": repeats}
                self.last_observation = observation
                self.tool_observations.append(observation)
                self.tool_observations = self.tool_observations[-4:]
                if observation.get("status") == "completed" and self.executor.work_item.kind.value == "investigate":
                    from app.services.review_task_derivation import TaskReviewDerivation
                    request = next((request for request in self.executor.work_item.evidence_requests
                        if not TaskReviewDerivation._request_covered(request, self.executor.evidence_catalog)), None)
                    if request is not None:
                        self.pending_read = RuntimeReadResponse(tool="file_read", arguments=request,
                                                               purpose="准备尚未读取的明确证据")
                if self.checkpoint is not None:
                    await self.checkpoint.persist()
                return observation
            executor = self.executor
            current_goals = executor.current_goals()
            current_ids = {goal.goal_id for goal in current_goals}
            related_ids = [identity for identity in executor.goals if identity not in current_ids]
            pending_item = executor.work_item.model_copy(update={
                "goal_ids": list(dict.fromkeys(goal.goal_id for goal in current_goals)),
                "related_goal_ids": related_ids})
            # 合法历史仅保存在服务端，续查不要求重写已经完成的目标。
            if not pending_item.goal_ids and not self.repair_errors:
                return {"action": "idle", "pending_goals": []}
            request = self.protocol.prepare(pending_item, current_goals,
                active_evidence_ids=executor.active_evidence_ids,
                evidence_catalog=executor.evidence_catalog,
                related_goals=[executor.goals[identity] for identity in related_ids])
            if self.tool_observations:
                from dataclasses import replace
                request = replace(request, prompt=request.prompt + "\n" + json.dumps({
                    "recent_tool_observations": self.tool_observations}, ensure_ascii=False))
            if self.repair_errors:
                from dataclasses import replace
                feedback = {
                    "correction": self.repair_errors,
                    "instruction": "Only resubmit rejected items; accepted results are stored by server.",
                }
                if executor.spec.prompt_version in {"task-review-prompt-v5", "task-review-prompt-v6"}:
                    feedback.update(rejected_items=self.rejected_items,
                        accepted_target_ids=sorted(executor.projection.accepted_target_ids))
                request = replace(request, prompt=request.prompt + "\n" + json.dumps(feedback, ensure_ascii=False))

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
            except LLMProviderError as exc:
                exc.work_item_id = executor.work_item.work_item_id
                if exc.usage is not None:
                    self.usages.append(exc.usage.model_copy(update={"response_metadata": {
                        "provider_failure": provider_failure_info(exc)}}))
                raise
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
            read_key = json.dumps([parsed.response.tool, parsed.response.arguments.model_dump()], sort_keys=True)
            if self.read_observations.get(read_key, {}).get("repeats", 0) >= 2:
                from app.services.review_task_executor import RuntimeParseError
                parsed.errors.append(RuntimeParseError("read", None, "repeated_read_no_progress"))
            else:
                self.pending_read = parsed.response
        schema_errors = [asdict(error) for error in parsed.errors
                         if error.reason in {"invalid_json", "invalid_response_kind", "unknown_submit_field"}
                         or "validation error" in error.reason
                         or error.reason == "field_must_be_array"
                         or (error.reason == "incomplete_investigation_evidence"
                             and executor.investigation_citation_readiness()["status"] == "ready")]
        if schema_errors and executor.work_item.format_corrections < 1:
            executor.work_item.format_corrections += 1
            self.repair_errors = schema_errors
        self.rejected_items = self._rejected_items(raw, schema_errors) if self.repair_errors else []
        page_advanced = executor.advance_page() if not self.repair_errors else False
        self.last_observation = {"action": "model", "errors": [asdict(error) for error in parsed.errors],
                "response_kind": parsed.response.kind if parsed.response is not None else None,
                "pending_goals": sorted(executor.projection.pending_target_ids),
                "correction_pending": bool(self.repair_errors), "page_advanced": page_advanced}
        return self.last_observation

    def _rejected_items(self, raw, errors) -> list[dict]:
        """保留本次被拒项的最小原文，供一次纠错只重交这些项。"""
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(value, dict):
            return []
        rejected = []
        for field in ("examinations", "candidates", "leads"):
            indices = {item.get("index") for item in errors
                       if item.get("item_type") == field and isinstance(item.get("index"), int)}
            for index in sorted(indices):
                items = value.get(field)
                if isinstance(items, list) and 0 <= index < len(items):
                    rejected.append({"item_type": field, "index": index, "item": items[index]})
        for field in ("investigation", "verification", "read"):
            if any(item["item_type"] == field for item in errors):
                rejected.append({"item_type": field, "index": None,
                    "item": value if field == "read" else value.get(field)})
        return rejected

    async def _read(self) -> dict:
        read = self.pending_read
        self.pending_read = None
        executor = self.executor
        args = read.arguments
        if read.tool != "file_read":
            return await self._other_read(read)
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
        canonical, _ = build_context_evidence([chunk], executor.spec.snapshot_head_sha,
                                             executor.spec.snapshot_base_sha)
        put_chunks(self.store, canonical)
        chunks = store_chunks(self.store)
        for identity, item in chunks.items():
            previous = executor.evidence_catalog.get(identity, {})
            executor.evidence_catalog[identity] = {**item, "file_path": item["file"],
                **{key: previous[key] for key in ("hunk_id", "canonical_change") if key in previous}}
        visible = {item["evidence_id"] for item in canonical}
        executor.active_evidence_ids.update(visible)
        return {"action": "tool", "status": "completed", "evidence_ids": sorted(visible)}

    async def _other_read(self, read):
        args = read.arguments
        executor = self.executor
        actual_head = await asyncio.to_thread(self.git._run,
            [self.git._git, "-C", self.scope.repository_root, "rev-parse", "HEAD"])
        if actual_head.strip() != executor.spec.snapshot_head_sha:
            return {"action": "tool", "status": "rejected", "reason": "stale_repository_snapshot"}
        try:
            if read.tool == "file_find":
                result = await self.tools.file_find_page(scope=self.scope, **args.model_dump())
                return {"action": "tool", "status": "completed", "tool": read.tool, "result": result}
            if read.tool == "file_read_diff":
                if not self.changed_files:
                    return {"action": "tool", "status": "blocked", "reason": "canonical_diff_unavailable"}
                chunk = await self.tools.file_read_diff(scope=self.scope, changed_files=self.changed_files,
                                                       **args.model_dump())
                return {"action": "tool", "status": "completed", "tool": read.tool, "result": chunk}
            plan = ContextRetrievalPlan(reason=read.purpose[:500],
                search_terms=[args.query] if args.relation == "text" else [],
                target_symbols=[] if args.relation == "text" else [args.query],
                relevance_types=[args.relation], include_callers=args.relation == "caller",
                include_callees=args.relation == "callee", include_tests=args.relation == "test",
                max_results=args.max_results)
            chunks = await self.search_tool.retrieve_context(changed_files=[
                value.model_dump(mode="json") if isinstance(value, ChangedFile) else value for value in self.changed_files],
                file_index=self.file_index, symbol_index=self.symbol_index,
                repo_path=self.scope.repository_root, plan=plan, scope=self.scope,
                repository_graph=self.repository_graph)
            accepted = []
            for chunk in chunks:
                if chunk.get("file") not in executor.readable_files or chunk.get("truncated"):
                    continue
                content = await asyncio.to_thread(self.git.get_file_content_at_revision,
                    self.scope.repository_root, executor.spec.snapshot_head_sha, chunk["file"])
                if content is None:
                    continue
                expected = "\n".join(content.splitlines()[chunk["start_line"] - 1:chunk["end_line"]])
                if chunk["content"].rstrip("\r\n") == expected:
                    accepted.append(chunk)
            canonical, _ = build_context_evidence(accepted, executor.spec.snapshot_head_sha,
                                                 executor.spec.snapshot_base_sha)
            put_chunks(self.store, canonical)
            evidence = store_chunks(self.store)
            active = []
            for identity, value in evidence.items():
                executor.evidence_catalog[identity] = {**value, "file_path": value["file"]}
                if identity in {chunk["evidence_id"] for chunk in canonical}:
                    executor.active_evidence_ids.add(identity)
                    active.append(identity)
            return {"action": "tool", "status": "completed", "tool": read.tool,
                    "evidence_ids": sorted(active), "rejected_chunks": len(chunks) - len(accepted)}
        except (ValueError, OSError):
            return {"action": "tool", "status": "rejected", "reason": "controlled_read_rejected"}
