"""确定性计划节点与 Review Unit 并发聚合节点。"""

from typing import Any

from app.agents.providers import build_provider
from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction, HumanReviewRequest, ReviewPlan, ReviewUnitResult, ReviewUnitStatus
from app.graph.checkpointer import get_checkpointer
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.review_repository import ReviewRepository


async def review_plan_node(state: ReviewState) -> ReviewState:
    planner: Any = state.get("_review_planner") or DeterministicReviewPlanner()
    plan = planner.plan(
        state.get("changed_files") or [],
        base_sha=state.get("base_sha") or "",
        head_sha=state.get("head_sha") or "",
        file_index=state.get("file_index") or [],
        symbol_index=state.get("symbol_index") or [],
        repository_graph=state.get("repository_graph") or {},
        model=state.get("model") or settings.repoguardian_model,
        provider=settings.repoguardian_provider,
    )
    return ReviewState(
        review_plan=plan.model_dump(mode="json"),
        review_units=[unit.model_dump(mode="json") for unit in plan.review_units],
        excluded_files=[item.model_dump(mode="json") for item in plan.excluded_files],
        warnings=list(state.get("warnings") or []) + plan.warnings,
        step_progress=append_step(
            state,
            "review_plan",
            "completed",
            f"生成 {len(plan.review_units)} 个 Review Unit，排除 {len(plan.excluded_files)} 个文件",
        ),
    )


async def review_units_node(state: ReviewState) -> ReviewState:
    plan = ReviewPlan.model_validate(state.get("review_plan") or {
        "planner_version": "unknown",
        "review_units": state.get("review_units") or [],
    })
    if not plan.review_units:
        return ReviewState(
            status="reviewing",
            review_unit_results=[],
            review_issues=[],
            step_progress=append_step(state, "review_units", "completed", "没有可审查的 Unit"),
        )

    executor: Any = state.get("_review_unit_executor")
    if executor is None:
        provider = state.get("_provider") or build_provider(
            settings.repoguardian_provider,
            settings.openai_api_key,
            settings.openai_base_url,
            settings.repoguardian_model,
        )
        executor = ReviewUnitExecutor(
            provider,
            concurrency=settings.repoguardian_review_unit_concurrency,
            timeout_seconds=settings.repoguardian_review_unit_timeout_seconds,
            checkpointer=(await get_checkpointer()) if state.get("_human_interrupt_enabled") else None,
        )
    previous = {
        item.review_unit_id: item
        for item in (
            ReviewUnitResult.model_validate(raw)
            for raw in state.get("review_unit_results") or []
        )
        if item.status == ReviewUnitStatus.completed
    }
    pending_units = [unit for unit in plan.review_units if unit.id not in previous]
    reusable: dict[str, ReviewUnitResult] = {}
    repository = ReviewRepository() if state.get("_human_interrupt_enabled") else None
    if repository:
        still_pending = []
        for unit in pending_units:
            cached = repository.find_reusable_unit(
                fingerprint=unit.fingerprint,
                model=state.get("model") or settings.repoguardian_model,
                provider=settings.repoguardian_provider,
                prompt_version=settings.repoguardian_prompt_version,
                rule_version=settings.repoguardian_rule_version,
                tool_schema_version=settings.repoguardian_tool_schema_version,
                review_policy_version=settings.repoguardian_review_policy_version,
            )
            if cached and cached.result_snapshot:
                result = ReviewUnitResult.model_validate(cached.result_snapshot)
                result = result.model_copy(update={"model_usages": []})
                reusable[unit.id] = result
                repository.record_unit_result(
                    task_id=str(state["task_id"]),
                    unit=unit,
                    result=result,
                    reused_from_id=cached.id,
                )
            else:
                still_pending.append(unit)
        pending_units = still_pending
    executed: list[ReviewUnitResult] = await executor.execute(pending_units, dict(state))
    if repository:
        units_by_id = {unit.id: unit for unit in pending_units}
        for result in executed:
            repository.record_unit_result(
                task_id=str(state["task_id"]),
                unit=units_by_id[result.review_unit_id],
                result=result,
            )
    by_id = {
        **previous,
        **reusable,
        **{item.review_unit_id: item for item in executed},
    }
    results = [by_id[unit.id] for unit in plan.review_units if unit.id in by_id]
    successful = [item for item in results if item.status == ReviewUnitStatus.completed]
    needs_human = [item for item in results if item.status == ReviewUnitStatus.needs_human]
    failed = [item for item in results if item.status != ReviewUnitStatus.completed]
    if needs_human:
        human_request = needs_human[0].human_request or HumanReviewRequest(
            missing_information=["Review Unit 需要人工提供业务规则。"],
            known_evidence=[needs_human[0].error or "Unit 无法安全自动判断。"],
            questions=["请提供继续审查所需的业务语义或处理选择。"],
            prohibited_operations=["收到回答前不得生成或应用修复。"],
        )
        action = AgentAction(
            action="request_human",
            reason=needs_human[0].error or "review unit requires human input",
            human_request=human_request,
        )
        return ReviewState(
            status="reviewing",
            next_action=action.model_dump(mode="json"),
            review_unit_results=[item.model_dump(mode="json") for item in results],
            review_issues=[
                issue.model_dump(mode="json") for item in successful for issue in item.issues
            ],
            context_snippets=[
                snippet.model_dump(mode="json")
                for item in successful for snippet in item.context_snippets
            ],
            step_progress=append_step(
                state, "review_units", "completed", "Review Unit 已暂停等待人工输入"
            ),
        )
    if failed and not successful:
        details = "; ".join(
            f"{item.review_unit_id}: {item.error or item.status.value}" for item in failed
        )
        return ReviewState(
            status="failed",
            error=f"all review units failed: {details}",
            review_unit_results=[item.model_dump(mode="json") for item in results],
            review_issues=[],
            context_snippets=[],
            agent_events=[
                *(state.get("agent_events") or []),
                *(event.model_dump(mode="json") for item in results for event in item.messages),
            ],
            step_progress=append_step(
                state, "review_units", "failed", "全部 Review Unit 执行失败"
            ),
        )

    issues = [issue for item in successful for issue in item.issues]
    snippets = [snippet for item in successful for snippet in item.context_snippets]
    events = [event for item in results for event in item.messages]
    warnings = list(state.get("warnings") or [])
    if failed:
        warnings.append(
            f"{len(failed)} 个 Review Unit 失败，其他 {len(successful)} 个 Unit 已完成"
        )
    return ReviewState(
        status="reviewing",
        review_unit_results=[item.model_dump(mode="json") for item in results],
        review_issues=[item.model_dump(mode="json") for item in issues],
        context_snippets=[item.model_dump(mode="json") for item in snippets],
        agent_events=[
            *(state.get("agent_events") or []),
            *(item.model_dump(mode="json") for item in events),
        ],
        warnings=warnings,
        step_progress=append_step(
            state,
            "review_units",
            "completed",
            f"完成 {len(successful)}/{len(results)} 个 Review Unit",
        ),
    )
