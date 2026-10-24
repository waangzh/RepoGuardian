"""Supplemental history queue; its gaps never change the Diff goal denominator."""

import asyncio
from copy import deepcopy
from pathlib import PurePosixPath
import time

from app.models.pr_discussion import CONTRACT, DiscussionSnapshot, DiscussionRecheck
from app.models.review import ReviewToolScope
from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem, RuntimeReadArguments, RuntimeReadResponse
from app.review.tool_scope import list_git_tracked_files, is_sensitive_repository_path
from app.core.fingerprints import stable_hash
from app.services.discussions.pr_discussions import open_store, frozen_provider, metadata
from app.services.review.review_run_budget import current_run_budget
from app.services.review.review_runtime import TaskReviewRuntime
from app.services.review.review_task_executor import ReviewTaskExecutor
from app.services.review.review_task_step import InspectTaskStep
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.tools.git_tool import GitTool


class DiscussionCheckpointRepository:
    """Reuse the runtime's send/response recovery protocol in a separate namespace."""

    def __init__(self, store):
        self.store = store
        self._artifacts = store.artifacts

    def load_task_review_runtime(self, task_id):
        if task_id != self.store.task_id:
            raise ValueError("discussion checkpoint task mismatch")
        return deepcopy(self.store.data.get("history_checkpoint"))

    def save_task_review_runtime(self, task_id, payload, expected_revision, lease):
        existing = self.store.data.get("history_checkpoint")
        if task_id != self.store.task_id or lease != self.store.lease:
            raise ValueError("discussion checkpoint identity mismatch")
        if (existing or {}).get("revision", -1) != expected_revision:
            raise ValueError("discussion checkpoint revision mismatch")
        if existing and (existing["spec"] != payload["spec"] or existing["goals"] != payload["goals"]):
            raise ValueError("discussion checkpoint manifest changed")
        value = deepcopy(self.store.data)
        value["history_checkpoint"] = payload
        references = [*payload.get("sessions", {}).values(),
                      *(call["response"] for call in payload.get("calls", {}).values() if call.get("response"))]
        if payload.get("readable_scope"):
            references.append(payload["readable_scope"])
        for ref in references:
            if ref not in value["artifacts"]:
                value["artifacts"].append(ref)
        before = value["revision"]
        value["revision"] += 1
        try:
            if self.store.repository:
                self.store.repository.save_discussion_runtime(task_id, value, before, lease)
        except BaseException:
            self.store.invalidated = True
            raise
        self.store.data = value


def thread_path(thread, readable, renames=None):
    if thread.path in readable:
        return thread.path
    renamed = (renames or {}).get(thread.path)
    if renamed in readable:
        return renamed
    body = "\n".join(c.body for c in thread.comments)
    exact = [path for path in readable if path in body]
    if len(exact) == 1:
        return exact[0]
    filenames = {PurePosixPath(path).name for path in readable if PurePosixPath(path).name in body}
    matches = [path for path in readable if PurePosixPath(path).name in filenames]
    return matches[0] if len(matches) == 1 else None


def _record_completed_rechecks(store, snapshot, steps):
    """把已接受的核验结果写入日志；未完成项保持 not_checked，不投影为成功。"""
    for thread in snapshot.threads:
        step = steps.get("history-" + stable_hash(thread.id)[:32])
        if step is None:
            continue
        result = step.executor.projection.investigation
        if result:
            store.data["rechecks"][thread.id] = DiscussionRecheck(discussion_id=thread.id,
                path=thread.path,
                status=getattr(result, "resolution", "insufficient_evidence"), reason=result.reason,
                evidence_ids=result.evidence_ids, limitations=result.limitations,
                severity=getattr(result, "severity", None),
                affected_behavior=getattr(result, "affected_behavior", None),
                recommendation=getattr(result, "recommendation", None)).model_dump(mode="json")


def _mark_incomplete_rechecks(store, snapshot, steps):
    """外层超时/取消时，未产生结论的复核显式标记为未核验，不维持旧快照。"""
    for thread in snapshot.threads:
        step = steps.get("history-" + stable_hash(thread.id)[:32])
        if step is None or step.executor.projection.investigation is not None:
            continue
        current = store.data["rechecks"].get(thread.id) or {}
        if current.get("status") != "not_checked":
            continue
        limitations = list(dict.fromkeys([*(current.get("limitations") or []), "recheck_incomplete"]))[:20]
        store.data["rechecks"][thread.id] = DiscussionRecheck(discussion_id=thread.id,
            status="not_checked", reason="历史问题代码核验未完成；超时或取消后结果未知。",
            limitations=limitations).model_dump(mode="json")


async def recheck_discussions_node(state, runtime=None, config=None):
    configuration = state.get("task_runtime_configuration") or {}
    if configuration.get("discussion_contract") != CONTRACT:
        return {}
    store = await open_store(state, runtime, config)
    if not store.data.get("input"):
        raise ValueError("discussion frozen input missing")
    if (state.get("task_runtime_report") or {}).get("execution_status") != "completed":
        store.data["termination_reason"] = "main_review_not_completed"
        await store.save()
        return {"pr_discussion": await metadata(store)}
    if store.data.get("finished"):
        return await result_state(store, state)
    snapshot = DiscussionSnapshot.model_validate(await store.read(store.data["snapshot"]))
    inputs = await store.read(store.data["input"])
    context = (runtime.context or {}) if runtime else {}
    git = context.get("task_runtime_git") or state.get("_git_tool") or GitTool()
    readable = sorted(path for path in await asyncio.to_thread(list_git_tracked_files, state["repo_path"])
                      if not is_sensitive_repository_path(path))
    budget = current_run_budget()
    if budget is None:
        raise ValueError("discussion shared budget unavailable")
    from app.review.runtime_spec import frozen_runtime_spec
    spec: ReviewRunSpec = frozen_runtime_spec(state, configuration, budget, readable_files=readable)
    spec = spec.model_copy(update={"discussion_recheck": True})
    provider = frozen_provider(configuration, context.get("task_runtime_provider") or state.get("_provider"))
    changed = {c["file_path"] for c in state.get("changed_files") or []}
    items, steps, goals = [], {}, []
    by_id = {item["id"]: item for item in inputs["items"]}
    renames = {c["old_file_path"]: c["file_path"] for c in state.get("changed_files") or []
               if c.get("old_file_path")}
    for thread in sorted(snapshot.threads, key=lambda t: (t.path not in changed, t.id)):
        path = thread_path(thread, set(readable), renames)
        if path is None:
            store.data["rechecks"].setdefault(thread.id, DiscussionRecheck(discussion_id=thread.id,
                path=thread.path, status="not_checked",
                reason="无法将历史事项定位到允许读取的当前源码；路径删除不代表问题已修复。",
                limitations=["code_target_not_located"]).model_dump(mode="json"))
            continue
        identity = "history-" + stable_hash(thread.id)[:32]
        goal = ReviewGoal(goal_id=identity, unit_id=identity, kind="investigate",
                          target_ref=path, origin="derived")
        work = ReviewWorkItem(work_item_id=identity, kind="investigate", goal_ids=[identity],
            unit_id=identity, scope_files=[path], readable_scope_hash=spec.readable_scope_hash,
            scheduling_class="investigation", sequence=len(items),
            proposition="核验该历史讨论中的原始问题在当前固定代码中是否仍成立，不能依据线程状态判断。",
            evidence_requests=[RuntimeReadArguments(file_path=path, start_line=1, end_line=1)])
        executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids=set(),
                                      evidence_catalog={}, request_id="not-dispatched")
        scope = ReviewToolScope(review_unit_id=identity, commentable_files=set(), readable_files=set(readable),
            seed_files={path}, repository_root=state["repo_path"],
            max_lines_per_read=configuration["max_lines_per_read"],
            max_search_results=configuration["max_search_results"])
        step = InspectTaskStep(executor, provider, budget, scope, git_tool=git,
            changed_files=[], file_index=state.get("file_index"), symbol_index=state.get("symbol_index"),
            repository_graph=state.get("repository_graph"))
        entry = deepcopy(by_id[thread.id])
        entry["path"] = path
        step.protocol.discussion_inputs = [entry]
        step.discussion_store = store
        step.pending_read = RuntimeReadResponse(tool="file_read",
            arguments=RuntimeReadArguments(file_path=path, start_line=1,
                                           end_line=min(80, configuration["max_lines_per_read"])),
            purpose="读取历史事项当前实现；原始行号仅作线索")
        steps[identity] = step
        items.append(work)
        goals.append(goal)
        store.data["rechecks"].setdefault(thread.id, DiscussionRecheck(discussion_id=thread.id,
            path=thread.path, status="not_checked",
            reason="尚未完成历史问题代码核验。").model_dump(mode="json"))
    await store.save()
    queue = TaskReviewRuntime(items, concurrency=1)
    if steps:
        checkpoint = TaskRuntimeCheckpoint(queue, steps, DiscussionCheckpointRepository(store), lease=store.lease)
        await checkpoint.restore()
        from app.services.review.review_plan import remaining_review_seconds
        remaining = remaining_review_seconds(configuration, state["created_at"])
        if remaining <= 0:
            termination = "deadline"
        else:
            try:
                termination = await queue.run(steps, deadline_seconds=remaining)
            except BaseException:
                # 外层期限/取消可能在模型响应在途时打断本节点；已接受的核验结果
                # 仍需落盘，未完成项标记未核验。清理失败不能掩盖原始异常。
                _record_completed_rechecks(store, snapshot, steps)
                _mark_incomplete_rechecks(store, snapshot, steps)
                store.data.setdefault("limitations", []).append("history_recheck:cancelled")
                try:
                    await store.save()
                except BaseException:
                    pass
                raise
        _record_completed_rechecks(store, snapshot, steps)
        # Persist the evidence bodies behind task-scoped references, never in task responses.
        store.data["history_evidence"] = await store.put("history-evidence",
            {key: value for step in steps.values() for key, value in step.executor.evidence_catalog.items()})
    else:
        termination = "no_located_targets" if snapshot.threads else "plan_exhausted"
    if termination != "plan_exhausted":
        store.data.setdefault("limitations", []).append("history_recheck:" + termination)
    store.data.update(finished=True, termination_reason=termination, finished_at=time.time())
    await store.save()
    return await result_state(store, state)


async def result_state(store, state):
    budget = current_run_budget()
    usages = list(state.get("model_usages") or [])
    for call in store.data["calls"].values():
        if call.get("response"):
            usages.append((await store.read(call["response"]))["usage"])
    checkpoint = store.data.get("history_checkpoint") or {}
    for ref in checkpoint.get("sessions", {}).values():
        usages.extend((await store.read(ref)).get("usages", []))
    unique = {u["id"]: u for u in usages}
    report = deepcopy(state.get("task_runtime_report"))
    if report is not None and budget is not None:
        # 历史复核与主审查共享预算，但计量归属独立：主报告只统计主审查调用，
        # 讨论补充阶段的未知用量记录到讨论统计，不把已完成的主审查降级为 unknown。
        from app.services.review.review_run_budget import attempt_accounting, is_discussion_attempt
        primary = [a for a in budget.attempts.values() if not is_discussion_attempt(a)]
        discussion = [a for a in budget.attempts.values() if is_discussion_attempt(a)]
        report.update(attempt_accounting(primary, prior_integrity=report.get("usage_integrity")))
        if discussion:
            from app.models.pr_discussion import DiscussionUsage
            report["discussion_usage"] = DiscussionUsage(
                **attempt_accounting(discussion)).model_dump(mode="json")
        else:
            report.pop("discussion_usage", None)
    return {"pr_discussion": await metadata(store), "model_usages": list(unique.values()),
            "run_budget": budget.snapshot() if budget else state.get("run_budget"),
            "task_runtime_report": report, "status": report["execution_status"] if report else state.get("status")}
