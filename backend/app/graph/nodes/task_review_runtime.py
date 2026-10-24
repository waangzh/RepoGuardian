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
from app.review.tool_scope import (validate_repository_base_file, validate_repository_file,
                                  is_sensitive_repository_path, list_git_tracked_files)
from app.services.fingerprints import stable_hash
from app.services.model_request_budgeter import ModelRequestProfile, RequestAdmissionError
from app.services.review_run_budget import current_run_budget, ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review_runtime import TaskReviewRuntime
from app.services.review_task_derivation import (TaskReviewDerivation, build_task_risk_catalog,
                                                dangling_deleted_references)
from app.services.review_task_executor import ReviewTaskExecutor
from app.services.review_task_step import InspectTaskStep
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.services.task_context_pages import build_context_pages
from app.services.task_review_projection import project_task_runtime, task_runtime_issues, task_runtime_coverage
from app.tools.git_tool import GitTool
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_input_context import build_context_evidence


def fit_context_pages(executor, step, configuration):
    """仅因 RequestFit 容量分页；不使用运行消费余额。"""
    profile = ModelRequestProfile.model_validate(configuration["request_profile"])
    request = step.protocol.prepare(executor.work_item, executor.current_goals(),
        active_evidence_ids=executor.active_evidence_ids, evidence_catalog=executor.evidence_catalog,
        symbol_index=step.symbol_index, repository_graph=step.repository_graph)
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
                capacity_chars=capacity, chunk_evidence=executor.spec.prompt_version in {"task-review-prompt-v4", "task-review-prompt-v5", "task-review-prompt-v6", "task-review-prompt-v7", "task-review-prompt-v8"})
            for page in pages:
                goals = [executor.goals[value["goal_id"]].model_copy(update={
                    "start_line": value["start_line"], "end_line": value["end_line"]}) for value in page.ranges]
                item = executor.work_item.model_copy(update={"goal_ids": [goal.goal_id for goal in goals]})
                step.protocol.prepare(item, goals, active_evidence_ids=set(page.evidence_ids),
                                      evidence_catalog=catalog, symbol_index=step.symbol_index,
                                      repository_graph=step.repository_graph).estimate(profile)
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
        "output_policy": settings.review_output_policy(),
        "prompt_version": "task-review-prompt-v8",
        "stability_contract": "review-stability-v2",
        "discussion_contract": "pr-discussion-context-v1" if settings.repoguardian_pr_discussions_enabled else None,
        "publication_contract": "claim-publication-v1",
        "pr_intent_contract": "pr-intent-assessment-v6",
        # 新任务冻结具体关系规划；历史手工配置缺省为 legacy，恢复时不能静默换规划器。
        "risk_planner_version": "risk-planner-v8",
        "call_resolution_version": "import-evidence-v6",
        "readable_scope_version": "repository-tracked-v1",
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
    """初始分母来自完整规范 diff；复用、分页和派生任务不能修改它。"""
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
            if file.is_binary or not selected:
                goals.append(ReviewGoal(goal_id="file-" + stable_hash([unit.id, file.model_dump(mode="json")])[:32],
                    unit_id=unit.id, kind="inspect", target_ref=file.file_path, target_kind="file_change",
                    unsupported_reason="non_head_text_change"))
                continue
            if file.change_type == "deleted":
                # 整文件删除与文件清空只有 Base 侧文本；unidiff 会同样判为 deleted。
                # 范围按删除前行号，不构造 Head 零长度区间。
                for index, hunk in selected:
                    identity = DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
                    goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                        unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                        target_kind="base_source_range", start_line=max(1, hunk.old_start),
                        end_line=max(1, hunk.old_start + hunk.old_length - 1)))
                continue
            for index, hunk in selected:
                identity = DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
                if hunk.new_length == 0 or hunk.new_start < 1:
                    # 纯删除 hunk 没有 Head 行；改在 Base 侧核对被删原文。
                    goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                        unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                        target_kind="base_source_range", start_line=max(1, hunk.old_start),
                        end_line=max(1, hunk.old_start + hunk.old_length - 1)))
                    continue
                goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                    unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                    start_line=hunk.new_start, end_line=hunk.new_start + hunk.new_length - 1))
    # 排除文件仍可见，不能变成零分母的完整结果。
    for file in changed:
        if file.file_path not in owned:
            goals.append(ReviewGoal(goal_id="excluded-" + stable_hash(file.model_dump(mode="json"))[:32],
                unit_id="excluded", kind="inspect", target_ref=file.file_path, target_kind="file_change",
                unsupported_reason="excluded_or_unsupported_change"))
    return goals


def frozen_runtime_spec(state, configuration, budget, *, readable_files=()):
    # 仅新配置启用；旧任务恢复不改变其冻结请求。上下文进入 Spec 哈希和同一预算请求包。
    intent_context = None
    intent_truncated = None
    contract = configuration.get("pr_intent_contract")
    if contract in {"pr-intent-context-v1", "pr-intent-assessment-v1", "pr-intent-assessment-v2",
                    "pr-intent-assessment-v3", "pr-intent-assessment-v4", "pr-intent-assessment-v5", "pr-intent-assessment-v6"}:
        pr = state.get("pr_info") or {}
        statement = "\n\n".join(value.strip() for value in (pr.get("title"), pr.get("body"))
                                 if isinstance(value, str) and value.strip())
        if statement:
            if contract in {"pr-intent-assessment-v2", "pr-intent-assessment-v3", "pr-intent-assessment-v4", "pr-intent-assessment-v5", "pr-intent-assessment-v6"}:
                intent_truncated = len(statement) > 4_000
            intent_context = statement[:3_999] + "…" if len(statement) > 4_000 else statement
    discussion_fields = {}
    if configuration.get("discussion_contract") == "pr-discussion-context-v1":
        from app.services.pr_discussions import select_discussions
        from app.services.model_request_budgeter import PreparedModelRequest
        metadata = state.get("pr_discussion") or {}
        if not metadata.get("manifest_hash"):
            raise ValueError("discussion frozen manifest missing")
        context_request = PreparedModelRequest(model=configuration["model"], operation="task_review_v1",
            system="", prompt="", output_tokens=4096)
        profile = ModelRequestProfile.model_validate(configuration["request_profile"])
        selected = select_discussions(state.get("_discussion_items") or [],
            set(readable_files), configuration["model"], profile, context_request)
        discussion_fields = {"discussion_manifest_hash": metadata["manifest_hash"], "discussion_context": selected,
                             "discussion_profile": configuration["request_profile"]}
    return ReviewRunSpec(run_id=state["task_id"], snapshot_head_sha=state["head_sha"],
        snapshot_base_sha=review_diff_base(state), diff_manifest_hash=stable_hash({
            "changed_files": state.get("changed_files") or [], "plan": state.get("review_plan") or {}}),
        model_provider=configuration["provider"], model_name=configuration["model"],
        policy_version=configuration["policy_version"], max_tokens=budget.max_tokens, max_calls=budget.max_calls,
        prompt_version=configuration.get("prompt_version", "task-review-prompt-v1"),
        publication_contract=configuration.get("publication_contract", "legacy"),
        stability_contract=configuration.get("stability_contract"),
        readable_scope_files=tuple(sorted(readable_files)),
        **discussion_fields, pr_intent_context=intent_context,
        pr_intent_context_truncated=intent_truncated,
        # 新 v6 配置允许同文件同侧的相交上下文；历史配置保留原冻结验收规则。
        intent_assessment_contract=(("structured-intent-v5" if contract == "pr-intent-assessment-v6"
            else "structured-intent-v4" if contract == "pr-intent-assessment-v5"
            else "structured-intent-v3" if contract == "pr-intent-assessment-v4"
            else "structured-intent-v2" if contract == "pr-intent-assessment-v3"
            else "structured-intent-v1") if intent_context and contract in {
                "pr-intent-assessment-v1", "pr-intent-assessment-v2", "pr-intent-assessment-v3",
                "pr-intent-assessment-v4", "pr-intent-assessment-v5", "pr-intent-assessment-v6"} else None),
        review_strategy={key: value for key, value in (configuration.get("effective_plan") or {}).items()
                         if key in {"depth", "focus"}})


async def source_catalog(goals, spec, root, git, changed_files=None):
    catalog = {}
    bodies = {}
    changes = {}
    for file in changed_files or []:
        for index, hunk in enumerate(file.hunks):
            identity = DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
            changes[(file.file_path, identity)] = hunk
    for goal in goals:
        if goal.unsupported_reason:
            continue
        # Base 侧目标读取删除前的固定快照；工作树可能已无该路径，故走 tree 校验。
        revision = spec.snapshot_base_sha if goal.base_side else spec.snapshot_head_sha
        key = (goal.target_ref, revision)
        if key not in bodies:
            if goal.base_side:
                await asyncio.to_thread(validate_repository_base_file, root, goal.target_ref,
                    base_sha=spec.snapshot_base_sha, tree_entries=git.get_tree_entries)
            else:
                # realpath/tracked/sensitive 防线在 Git blob 读取之前执行。
                await asyncio.to_thread(validate_repository_file, root, goal.target_ref)
            bodies[key] = await asyncio.to_thread(git.get_file_content_at_revision,
                root, revision, goal.target_ref)
        body = bodies[key]
        if body is None or "\x00" in body:
            continue
        lines = body.splitlines()
        if goal.end_line > len(lines):
            continue
        content = "\n".join(lines[goal.start_line - 1:goal.end_line])
        if lines[goal.end_line - 1] == "":
            content += "\n"  # 保留末尾空白物理行，splitlines 不能少算一行。
        value = {"file_path": goal.target_ref, "start_line": goal.start_line, "end_line": goal.end_line,
            "head_sha": spec.snapshot_head_sha, "base_sha": spec.snapshot_base_sha,
            "side": "base" if goal.base_side else "head",
            "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "source": "fixed_snapshot", "hunk_id": goal.goal_id}
        for (path, identity), hunk in changes.items():
            if path == goal.target_ref and goal.goal_id == "goal-" + stable_hash([goal.unit_id, identity])[:32]:
                value["canonical_change"] = {"goal_id": goal.goal_id, "hunk_id": identity,
                    "hunk_hash": stable_hash(hunk.model_dump(mode="json")),
                    "old_start": hunk.old_start, "old_length": hunk.old_length,
                    "new_start": hunk.new_start, "new_length": hunk.new_length,
                    "lines": [line.model_dump(mode="json") for line in hunk.lines]}
                break
        if spec.prompt_version in {"task-review-prompt-v4", "task-review-prompt-v5", "task-review-prompt-v6", "task-review-prompt-v7", "task-review-prompt-v8"}:
            chunks, _ = build_context_evidence([{**value, "file": goal.target_ref}],
                                              spec.snapshot_head_sha, spec.snapshot_base_sha)
            for chunk in chunks:
                # 每块有自己的原文、范围和hash；保留规范diff的目标绑定。
                catalog[chunk["evidence_id"]] = {**chunk, "file_path": goal.target_ref,
                    "source": "fixed_snapshot", "hunk_id": goal.goal_id,
                    "canonical_change": value.get("canonical_change")}
        else:
            catalog["evidence-" + stable_hash(value)[:32]] = value
    return catalog


async def task_review_runtime_node(state: ReviewState, config: RunnableConfig = None,
                                   runtime: Runtime[ReviewRunContext] = None) -> ReviewState:
    if state.get("engine") != "task_review_v1":
        raise ValueError("task runtime requires frozen engine")
    configuration = state.get("task_runtime_configuration")
    if not configuration:
        raise ValueError("task runtime frozen configuration missing")
    risk_planner_version = configuration.get("risk_planner_version", "legacy")
    if risk_planner_version not in {"legacy", "risk-planner-v2", "risk-planner-v3", "risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
        raise ValueError("unsupported risk planner version")
    expected_resolution = {"risk-planner-v5": "import-evidence-v4", "risk-planner-v6": "import-evidence-v5",
                           "risk-planner-v7": "import-evidence-v5", "risk-planner-v8": "import-evidence-v6"}
    if (risk_planner_version in expected_resolution
            and configuration.get("call_resolution_version") != expected_resolution[risk_planner_version]):
        raise ValueError("risk planner call resolution version mismatch")
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
    expanded_scope = configuration.get("readable_scope_version") == "repository-tracked-v1"
    if expanded_scope:
        tracked = await asyncio.to_thread(list_git_tracked_files, state["repo_path"])
        readable = {path for path in tracked if not is_sensitive_repository_path(path)}
    else:
        # 已创建的 P4 任务继续使用原权限，不在恢复时静默扩权。
        readable = set(sorted({value["path"] for value in state.get("file_index") or []
            if value.get("path") and not is_sensitive_repository_path(value["path"])})[:40])
    discussion_store = None
    if configuration.get("discussion_contract") == "pr-discussion-context-v1":
        from app.services.pr_discussions import open_store
        discussion_store = await open_store(state, runtime, config)
        if not discussion_store.data.get("input"):
            raise ValueError("discussion frozen input missing")
        frozen_discussions = await discussion_store.read(discussion_store.data["input"])
        state = {**state, "_discussion_items": frozen_discussions["items"]}
    spec = frozen_runtime_spec(state, configuration, budget, readable_files=readable)
    if not expanded_scope:
        spec = spec.model_copy(update={"readable_scope_files": ()})
    if (configuration.get("experiment_expected_scope_hash") is not None
            and spec.readable_scope_hash != configuration["experiment_expected_scope_hash"]):
        raise ValueError("experiment_readable_scope_mismatch_before_dispatch")
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
    from app.services.incremental_review import (
        reuse_coverage, reused_unit_snapshots, runtime_dependencies, validate_binding,
        same_snapshot_configuration_matches,
    )

    binding = validate_binding(configuration, head_sha=state.get("head_sha") or "",
                               effective_base_sha=review_diff_base(state))
    previous = None
    reused = {}
    reused_ids = set()
    if binding is not None:
        repository = context.get("coordination_repository") or state.get("_coordination_repository")
        if repository is None:
            raise ValueError("incremental review requires persisted previous task")
        previous = await asyncio.to_thread(repository.get_task, binding.previous_task_id)
        if (binding.previous_head_sha == binding.new_head_sha
                and (previous is None or not same_snapshot_configuration_matches(
                    previous.task_runtime_configuration, configuration))):
            raise ValueError("same_snapshot_configuration_changed")
        reused_units = reused_unit_snapshots(configuration)
        expected = {item.unit_id for item in binding.reused_units}
        if {unit.id for unit in reused_units} != expected:
            raise ValueError("incremental_reused_units_changed")
        plan = plan.model_copy(update={"review_units": [
            unit for unit in plan.review_units if unit.id not in expected] + reused_units})
        if not await asyncio.to_thread(
                git.is_ancestor, state["repo_path"], binding.previous_head_sha, binding.new_head_sha):
            raise ValueError("incremental_history_changed")
        delta = await asyncio.to_thread(
            git.changed_paths, state["repo_path"], binding.previous_head_sha, binding.new_head_sha)
        reused = reuse_coverage(binding, previous, changed, plan.review_units,
            delta_paths=delta, repository_graph=state.get("repository_graph"))
        reused_ids = expected
    goals = plan_runtime_goals(changed, plan)
    active_goals = [goal for goal in goals if goal.unit_id not in reused_ids]
    catalog = await source_catalog(active_goals, spec, state["repo_path"], git,
                                   changed_files=changed if expanded_scope else None)
    steps, items = {}, []
    for unit in plan.review_units:
        if unit.id in reused_ids:
            # 未变更 Unit 已由前序任务完成；不创建新的模型工作项。
            continue
        assigned = [goal for goal in goals if goal.unit_id == unit.id and not goal.unsupported_reason]
        for start in range(0, len(assigned), 40):
            selected = assigned[start:start + 40]
            paths = sorted({goal.target_ref for goal in selected})
            paths += [path for path in sorted(unit.related_files)
                      if (not expanded_scope or path in readable) and path not in paths][:max(0, 40 - len(paths))]
            if not expanded_scope:
                paths += [path for path in sorted(readable) if path not in paths][:max(0, 40 - len(paths))]
            paths = paths[:40]
            # 删除路径只作 Base 取证种子，不在模型可读/可评论范围内。
            base_targets = {goal.target_ref for goal in selected if goal.base_side}
            readable_paths = set(readable if expanded_scope else paths) - base_targets
            seed_paths = set(paths) - base_targets
            identity = "inspect-" + stable_hash([unit.id, [goal.goal_id for goal in selected]])[:32]
            item = ReviewWorkItem(work_item_id=identity, unit_id=unit.id, kind="inspect",
                goal_ids=[goal.goal_id for goal in selected], scope_files=sorted(seed_paths), sequence=len(items),
                readable_scope_hash=spec.readable_scope_hash if expanded_scope else None)
            initial = {key: deepcopy(value) for key, value in catalog.items()
                       if any(value["hunk_id"] == goal.goal_id for goal in selected)}
            executor = ReviewTaskExecutor(spec, item, selected, active_evidence_ids=set(initial),
                                          evidence_catalog=initial, request_id="not-dispatched")
            scope = ReviewToolScope(review_unit_id=identity,
                # 删除行无法在 Head 侧做行内评论定位；结论按 summary 呈现，不写入 commentable。
                commentable_files={goal.target_ref for goal in selected} - base_targets,
                readable_files=readable_paths, seed_files=seed_paths, repository_root=state["repo_path"],
                max_lines_per_read=configuration["max_lines_per_read"],
                max_search_results=configuration["max_search_results"])
            steps[identity] = InspectTaskStep(executor, provider, budget, scope, git_tool=git,
                changed_files=changed, file_index=state.get("file_index"), symbol_index=state.get("symbol_index"),
                repository_graph=state.get("repository_graph"))
            steps[identity].discussion_store = discussion_store
            # 无原文的目标继续显示缺口；不能用空 catalog 假装 examined。
            if all(executor._covers(goal, list(initial)) for goal in selected):
                fit_context_pages(executor, steps[identity], configuration)
            items.append(item)
    experiment = configuration.get("experiment") or {}
    strategy = configuration.get("effective_plan") or {}
    from app.services.review_plan import DEPTHS
    queue = TaskReviewRuntime(items, concurrency=configuration["concurrency"],
                             unit_timeout_seconds=experiment.get("unit_timeout_seconds"))
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    if writer:
        def progress():
            current = project_task_runtime(queue, steps, goals, termination_reason="running",
                                           budget=budget, reused_coverage=reused)
            writer({"kind": "task_runtime_progress", "state": {
                "task_runtime_report": current.model_dump(mode="json"),
                "review_coverage": task_runtime_coverage(current, changed)}})
        queue.progress = progress
    if steps:
        TaskReviewDerivation(queue, steps, changed_files=changed,
            risk_planner_version=risk_planner_version,
            investigations_enabled=(strategy.get("depth") != "quick" and experiment.get("investigations_enabled", True)),
            max_derived_items=DEPTHS.get(strategy.get("depth", "standard"), DEPTHS["standard"])[2],
            rule_ids={unit.id: unit.rule_ids for unit in plan.review_units},
            risk_catalog=build_task_risk_catalog([goal for goal in active_goals if not goal.unsupported_reason],
                                               items, state.get("repository_graph") or {},
                                               planner_version=risk_planner_version,
                                               max_lines_per_read=configuration["max_lines_per_read"],
                                               dangling_references=dangling_deleted_references(
                                                   [file.file_path for file in changed
                                                    if file.change_type == "deleted"],
                                                   state.get("file_index"))))
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
        if strategy:
            from app.services.review_plan import remaining_review_seconds
            remaining = min(remaining, remaining_review_seconds(configuration, state["created_at"]))
        retry_id = state.get("task_runtime_retry_unit_id")
        if retry_id:
            from app.services.review_continuation import ALL_PENDING
            all_pending = retry_id == ALL_PENDING and spec.stability_contract == "review-stability-v2"
            if not all_pending and not any(item.unit_id == retry_id for item in items):
                raise KeyError(retry_id)
            for identity in (sorted({item.unit_id for item in items}) if all_pending else [retry_id]):
                await queue.retry_unit(identity, steps)
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
        except Exception as exc:
            queue.record_failure(exc, stage="task_execution")
            termination = "technical_failure"
        if not queue.cancelled and not queue.failures:
            try:
                queue.derivation.refresh()
            except Exception as exc:
                queue.record_failure(exc, stage="task_derivation")
                termination = "technical_failure"
        if queue.failures:
            termination = "technical_failure"
    else:
        termination = "plan_exhausted"
    report = project_task_runtime(queue, steps, goals, termination_reason=termination, budget=budget,
                                  reused_coverage=reused)
    dependencies = runtime_dependencies(plan, steps, state.get("repository_graph"))
    if previous is not None:
        from app.models.review_runtime import UnitReuseDependencies
        dependencies.update({identity: UnitReuseDependencies.model_validate(
            previous.task_runtime_report["reuse_dependencies"][identity]) for identity in reused_ids})
    metadata = {"reuse_dependencies": dependencies}
    if binding is not None and binding.previous_head_sha == binding.new_head_sha:
        def reused_records(name):
            reused_goals = {key for key, value in reused.items() if value.get("unit_id") in reused_ids}
            return [{**deepcopy(item), "reused_from_task_id": previous.id,
                     "reused_from_head_sha": binding.previous_head_sha,
                     **({"origin_review_id": item.get("origin_review_id", previous.id)} if name == "findings" else {})}
                    for item in previous.task_runtime_report.get(name, [])
                    if item.get("unit_id") in reused_ids
                    or (name == "findings" and (item.get("candidate") or {}).get("primary_anchor") in reused_goals)
                    or (name == "findings" and (not steps
                        or (item.get("issue") or {}).get("review_unit_id") in reused_ids))
                    or (name == "relationship_hints" and not steps)]
        metadata["admitted_investigations"] = [
            *report.admitted_investigations, *reused_records("admitted_investigations")]
        metadata["relationship_hints"] = [*report.relationship_hints, *reused_records("relationship_hints")]
        if configuration.get("stability_contract") == "review-stability-v2":
            metadata["findings"] = [*report.findings, *reused_records("findings")]
    if not steps:
        metadata.update(
            pr_intent_context=spec.pr_intent_context,
            pr_intent_context_truncated=spec.pr_intent_context_truncated,
            readable_scope_hash=spec.readable_scope_hash if spec.readable_scope_files else None,
            readable_file_count=len(spec.readable_scope_files) if spec.readable_scope_files else None)
    report = report.model_copy(update=metadata)
    issues = task_runtime_issues(report)
    return ReviewState(task_runtime_report=report.model_dump(mode="json"),
        task_runtime_configuration=configuration, task_runtime_retry_unit_id=None,
        review_issues=issues, review_unit_results=[],
        status=("reviewing" if discussion_store is not None and report.execution_status == "completed"
                else report.execution_status), review_coverage=task_runtime_coverage(report, changed),
        run_budget=budget.snapshot(),
        model_usages=[usage.model_dump(mode="json") for step in steps.values() for usage in step.usages],
        step_progress=append_step(state, "task_review_runtime", "completed", "新引擎运行结束：" + termination))
