"""固定快照、原始范围与唯一任务运行时的生产适配。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from copy import deepcopy, copy
from langgraph.runtime import Runtime
from langgraph.config import get_stream_writer
from langchain_core.runnables import RunnableConfig

from app.agents.providers import OpenAICompatibleProvider
from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState, ReviewRunContext
from app.models.review import ChangedFile, ReviewPlan, ReviewToolScope
from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem
from app.review.diff_base import review_diff_base
from app.review.tool_scope import validate_repository_file, is_sensitive_repository_path
from app.services.fingerprints import stable_hash
from app.services.model_request_budgeter import ModelRequestProfile, RequestAdmissionError
from app.services.review_run_budget import current_run_budget, ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review_runtime import TaskReviewRuntime
from app.services.review_task_derivation import TaskReviewDerivation, build_task_risk_catalog
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_step import InspectTaskStep
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.services.task_context_pages import build_context_pages
from app.services.task_review_projection import project_task_runtime, task_runtime_issues, task_runtime_coverage
from app.tools.git_tool import GitTool
from app.services.review_planner import DeterministicReviewPlanner


def fit_context_pages(executor, step, configuration):
    """仅因 RequestFit 容量分页；不使用运行消费余额。"""
    profile = ModelRequestProfile.model_validate(configuration["request_profile"])
    request = step.protocol.prepare(executor.work_item, executor.current_goals(),
        active_evidence_ids=executor.active_evidence_ids, evidence_catalog=executor.evidence_catalog)
    try:
        request.estimate(profile)
        return
    except RequestAdmissionError:
        pass
    original = deepcopy(executor.evidence_catalog)
    capacity = max(1, sum(len(value["content"]) for value in original.values()) // 2)
    while capacity >= 1:
        catalog = deepcopy(original)
        try:
            pages = build_context_pages(executor.work_item, list(executor.goals.values()), catalog,
                                         capacity_chars=capacity)
            for page in pages:
                goals = [executor.goals[value["goal_id"]].model_copy(update={
                    "start_line": value["start_line"], "end_line": value["end_line"]}) for value in page.ranges]
                item = executor.work_item.model_copy(update={"goal_ids": [goal.goal_id for goal in goals]})
                step.protocol.prepare(item, goals, active_evidence_ids=set(page.evidence_ids),
                                      evidence_catalog=catalog).estimate(profile)
            executor.evidence_catalog = catalog
            executor.bind_pages(pages)
            return
        except (RequestAdmissionError, ValueError):
            capacity //= 2
    raise ValueError("task_context_unsplittable_capacity")


def task_runtime_configuration(model=None, *, provider=None):
    provider_name = getattr(provider, "_provider_name", settings.repoguardian_provider)
    selected_model = model or settings.repoguardian_model
    resolver = getattr(provider, "resolve_request_profile", None)
    profile = resolver(selected_model) if callable(resolver) else settings.resolve_model_profile(selected_model)
    return {"provider": provider_name, "model": selected_model,
        "endpoint": getattr(provider, "_base_url", settings.openai_base_url),
        "limits": settings.run_budget_limits(), "policy_version": settings.repoguardian_review_policy_version,
        "concurrency": min(32, max(1, settings.repoguardian_review_unit_concurrency)),
        "deadline_seconds": settings.repoguardian_task_review_timeout_seconds,
        "request_attempts": settings.repoguardian_model_request_attempts,
        "request_timeout_seconds": settings.repoguardian_model_request_timeout_seconds,
        "max_lines_per_read": settings.repoguardian_review_unit_max_lines_per_read,
        "max_search_results": settings.repoguardian_review_unit_max_search_results,
        "request_profile": profile.profile.model_dump(mode="json")}


def plan_runtime_goals(changed, plan):
    """初始分母来自完整规范 diff；页和派生任务不能修改它。"""
    goals = []
    owned = set()
    for unit in plan.review_units:
        for file in changed:
            if file.file_path not in unit.primary_files:
                continue
            owned.add(file.file_path)
            selected = [(index, hunk) for index, hunk in enumerate(file.hunks)
                        if not unit.diff_hunk_ids or DeterministicReviewPlanner.hunk_id(
                            file.file_path, index, hunk.model_dump(mode="json")) in unit.diff_hunk_ids]
            if not selected or file.is_binary or file.change_type == "deleted":
                goals.append(ReviewGoal(goal_id="file-" + stable_hash([unit.id, file.model_dump(mode="json")])[:32],
                    unit_id=unit.id, kind="inspect", target_ref=file.file_path, target_kind="file_change",
                    unsupported_reason="non_head_text_change"))
                continue
            for index, hunk in selected:
                identity = DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
                goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                    unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                    start_line=max(1, hunk.new_start), end_line=max(1, hunk.new_start + hunk.new_length - 1),
                    unsupported_reason="no_head_lines" if hunk.new_length == 0 else None))
    # 排除文件仍可见，不能变成零分母的完整结果。
    for file in changed:
        if file.file_path not in owned:
            goals.append(ReviewGoal(goal_id="excluded-" + stable_hash(file.model_dump(mode="json"))[:32],
                unit_id="excluded", kind="inspect", target_ref=file.file_path, target_kind="file_change",
                unsupported_reason="excluded_or_unsupported_change"))
    return goals


def frozen_runtime_spec(state, configuration, budget):
    return ReviewRunSpec(run_id=state["task_id"], snapshot_head_sha=state["head_sha"],
        snapshot_base_sha=review_diff_base(state), diff_manifest_hash=stable_hash({
            "changed_files": state.get("changed_files") or [], "plan": state.get("review_plan") or {}}),
        model_provider=configuration["provider"], model_name=configuration["model"],
        policy_version=configuration["policy_version"], max_tokens=budget.max_tokens, max_calls=budget.max_calls)


async def source_catalog(goals, spec, root, git):
    catalog = {}
    bodies = {}
    for goal in goals:
        if goal.unsupported_reason:
            continue
        if goal.target_ref not in bodies:
            # realpath/tracked/sensitive 防线在 Git blob 读取之前执行。
            await asyncio.to_thread(validate_repository_file, root, goal.target_ref)
            bodies[goal.target_ref] = await asyncio.to_thread(git.get_file_content_at_revision,
                root, spec.snapshot_head_sha, goal.target_ref)
        body = bodies[goal.target_ref]
        if body is None or "\x00" in body:
            continue
        lines = body.splitlines()
        if goal.end_line > len(lines):
            continue
        content = "\n".join(lines[goal.start_line - 1:goal.end_line])
        value = {"file_path": goal.target_ref, "start_line": goal.start_line, "end_line": goal.end_line,
            "head_sha": spec.snapshot_head_sha, "base_sha": spec.snapshot_base_sha,
            "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "source": "fixed_snapshot", "hunk_id": goal.goal_id}
        catalog["evidence-" + stable_hash(value)[:32]] = value
    return catalog


async def task_review_runtime_node(state: ReviewState, config: RunnableConfig = None,
                                   runtime: Runtime[ReviewRunContext] = None) -> ReviewState:
    if state.get("engine") != "task_review_v1":
        raise ValueError("task runtime requires frozen engine")
    configuration = state.get("task_runtime_configuration")
    if not configuration:
        raise ValueError("task runtime frozen configuration missing")
    context = (runtime.context or {}) if runtime else {}
    repository = context.get("coordination_repository") or state.get("_coordination_repository")
    if state.get("_persistence_enabled") and repository is None:
        raise ValueError("task runtime persistence unavailable")
    lease = (config or {}).get("configurable", {}).get("review_job_lease")
    budget = current_run_budget()
    if budget is None:
        budget = await ReviewRunBudget.load_or_create(state["task_id"], **configuration["limits"],
            store=ReviewRepositoryRunBudgetStore(repository, lease) if repository else None,
            recover_running=False)
    spec = frozen_runtime_spec(state, configuration, budget)
    git = context.get("task_runtime_git") or state.get("_git_tool") or GitTool()
    actual_head = await asyncio.to_thread(git._run, [git._git, "-C", state["repo_path"], "rev-parse", "HEAD"])
    if actual_head.strip() != spec.snapshot_head_sha:
        raise ValueError("task runtime stale snapshot")
    provider = context.get("task_runtime_provider") or state.get("_provider")
    if (provider is None or getattr(provider, "_provider_name", None) != configuration["provider"]
            or getattr(provider, "_base_url", "").rstrip("/") != configuration["endpoint"].rstrip("/")):
        provider = OpenAICompatibleProvider(settings.openai_api_key, configuration["endpoint"],
            configuration["model"], provider_name=configuration["provider"],
            disable_thinking=configuration["provider"] == "deepseek",
            request_attempts=configuration["request_attempts"],
            request_timeout_seconds=configuration["request_timeout_seconds"],
            request_profile=ModelRequestProfile.model_validate(configuration["request_profile"]))
    else:
        provider = copy(provider)
        target = provider
        # 评测观察器只是代理，真正的 prepare/send 在它包装的 Provider 上执行。
        wrapped = vars(provider).get("provider")
        if isinstance(wrapped, OpenAICompatibleProvider):
            provider.provider = copy(wrapped)
            target = provider.provider
        target._request_attempts = configuration["request_attempts"]
        target._request_timeout_seconds = configuration["request_timeout_seconds"]
        target._profile_override = ModelRequestProfile.model_validate(configuration["request_profile"])
    changed = [ChangedFile.model_validate(value) for value in state.get("changed_files") or []]
    plan = ReviewPlan.model_validate(state["review_plan"])
    goals = plan_runtime_goals(changed, plan)
    catalog = await source_catalog(goals, spec, state["repo_path"], git)
    readable = sorted({value["path"] for value in state.get("file_index") or []
                       if value.get("path") and not is_sensitive_repository_path(value["path"])})[:40]
    steps, items = {}, []
    for unit in plan.review_units:
        assigned = [goal for goal in goals if goal.unit_id == unit.id and not goal.unsupported_reason]
        for start in range(0, len(assigned), 40):
            selected = assigned[start:start + 40]
            paths = sorted({goal.target_ref for goal in selected})
            paths += [path for path in sorted(unit.related_files) if path not in paths][:max(0, 40 - len(paths))]
            paths += [path for path in readable if path not in paths][:max(0, 40 - len(paths))]
            paths = paths[:40]
            identity = "inspect-" + stable_hash([unit.id, [goal.goal_id for goal in selected]])[:32]
            item = ReviewWorkItem(work_item_id=identity, unit_id=unit.id, kind="inspect",
                goal_ids=[goal.goal_id for goal in selected], scope_files=paths, sequence=len(items))
            initial = {key: deepcopy(value) for key, value in catalog.items()
                       if any(value["file_path"] == goal.target_ref and value["start_line"] == goal.start_line
                              and value["end_line"] == goal.end_line for goal in selected)}
            executor = ReviewTaskExecutor(spec, item, selected, active_evidence_ids=set(initial),
                                          evidence_catalog=initial, request_id="not-dispatched")
            scope = ReviewToolScope(review_unit_id=identity, commentable_files={goal.target_ref for goal in selected},
                readable_files=set(paths), seed_files=set(paths), repository_root=state["repo_path"],
                max_lines_per_read=configuration["max_lines_per_read"],
                max_search_results=configuration["max_search_results"])
            steps[identity] = InspectTaskStep(executor, provider, budget, scope, git_tool=git,
                changed_files=changed, file_index=state.get("file_index"), symbol_index=state.get("symbol_index"),
                repository_graph=state.get("repository_graph"))
            # 无原文的目标继续显示缺口；不能用空 catalog 假装 examined。
            if len(initial) == len(selected):
                fit_context_pages(executor, steps[identity], configuration)
            items.append(item)
    queue = TaskReviewRuntime(items, concurrency=configuration["concurrency"])
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    if writer:
        def progress():
            current = project_task_runtime(queue, steps, goals, termination_reason="running", budget=budget)
            writer({"kind": "task_runtime_progress", "state": {
                "task_runtime_report": current.model_dump(mode="json"),
                "review_coverage": task_runtime_coverage(current, changed)}})
        queue.progress = progress
    if steps:
        TaskReviewDerivation(queue, steps, changed_files=changed,
            rule_ids={unit.id: unit.rule_ids for unit in plan.review_units},
            risk_catalog=build_task_risk_catalog([goal for goal in goals if not goal.unsupported_reason],
                                               items, state.get("repository_graph") or {}))
        checkpoint = TaskRuntimeCheckpoint(queue, steps, repository, lease=lease) if repository else None
        if checkpoint:
            await checkpoint.restore()
            deadline_at = checkpoint.data.get("deadline_at")
            if deadline_at is None:
                deadline_at = time.time() + configuration["deadline_seconds"]
                checkpoint.data["deadline_at"] = deadline_at
                await checkpoint.persist()
            remaining = deadline_at - time.time()
        else:
            remaining = configuration["deadline_seconds"]
        retry_id = state.get("task_runtime_retry_unit_id")
        if retry_id:
            if not any(item.unit_id == retry_id for item in items):
                raise KeyError(retry_id)
            await queue.retry_unit(retry_id, steps)
        try:
            if remaining <= 0:
                if all(item.state.value == "completed" for item in queue._items.values()):
                    termination = "plan_exhausted"
                else:
                    await queue.cancel(steps)
                    termination = "deadline"
            else:
                termination = await queue.run(steps, deadline_seconds=remaining)
        except asyncio.CancelledError:
            # 失去 lease 不发布；用户取消由 Repository 的取消事务保存对账视图。
            if checkpoint is None:
                await queue.cancel(steps)
            raise
        if not queue.cancelled:
            queue.derivation.refresh()
    else:
        termination = "plan_exhausted"
    report = project_task_runtime(queue, steps, goals, termination_reason=termination, budget=budget)
    return ReviewState(task_runtime_report=report.model_dump(mode="json"),
        task_runtime_configuration=configuration, task_runtime_retry_unit_id=None,
        review_issues=task_runtime_issues(report), review_unit_results=[],
        status=report.execution_status, review_coverage=task_runtime_coverage(report, changed),
        run_budget=budget.snapshot(),
        model_usages=[usage.model_dump(mode="json") for step in steps.values() for usage in step.usages],
        step_progress=append_step(state, "task_review_runtime", "completed", "新引擎运行结束：" + termination))
