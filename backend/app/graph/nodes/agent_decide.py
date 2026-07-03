import logging
from typing import Any

from app.agents.providers import LLMProviderError, build_provider
from app.core.config import settings
from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction

logger = logging.getLogger("RepoGuardian.Node")


async def agent_decide_node(state: ReviewState) -> ReviewState:
    """Agent 决策节点：核心大脑，每次循环调用 LLM 决定下一步做什么。

    安全防护：
        1. agent_loop_count > max_agent_loops → 强制 finish_report
        2. fix_iteration >= max_fix_iterations 且测试仍失败 → 强制 finish_report
        3. LLM 连续返回无效 JSON 两次 → 强制 finish_report
        4. 单次 LLM 异常 → 回退到 review_code
    """
    loop_count = int(state.get("agent_loop_count") or 0) + 1
    max_loops = int(state.get("max_agent_loops") or 6)
    fix_iteration = int(state.get("fix_iteration") or 0)
    max_fix_iterations = int(state.get("max_fix_iterations") or 3)

    logger.info(
        "🧠 [决策] 第 %d/%d 轮 | 修复迭代 %d/%d | 已有问题 %d | patches %d",
        loop_count,
        max_loops,
        fix_iteration,
        max_fix_iterations,
        len(state.get("review_issues") or []),
        len(state.get("patches") or []),
    )

    # ---- 安全防护 1: Agent 循环次数超限 ----
    if loop_count > max_loops:
        logger.warning("🛑 [决策] Agent 循环次数已达上限 %d，强制生成报告", max_loops)
        action = AgentAction(
            action="finish_report",
            reason=f"Agent loop limit reached ({max_loops}); finishing report.",
        )
        return _with_action(state, action, loop_count, "completed", "Agent loop limit reached")

    # ---- 安全防护 2: 修复重试超限且测试仍失败 ----
    if fix_iteration >= max_fix_iterations and _has_failed_tests(state):
        logger.warning("🛑 [决策] 修复重试已达上限 %d 且测试仍失败，强制生成报告", max_fix_iterations)
        action = AgentAction(
            action="finish_report",
            reason=f"Fix retry limit reached ({max_fix_iterations}); finishing report.",
        )
        return _with_action(state, action, loop_count, "completed", "Fix retry limit reached")

    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )

    # ---- 调用 LLM 做决策 ----
    try:
        logger.debug("🧠 [决策] 调用 Provider.decide() ...")
        action = await provider.decide(dict(state), state.get("model"))
        invalid_action_count = int(state.get("invalid_action_count") or 0)
        logger.info("🧠 [决策] LLM 选择: %s（理由: %s）", action.action.value, action.reason)
    except (LLMProviderError, ValueError) as exc:
        invalid_action_count = int(state.get("invalid_action_count") or 0) + 1
        logger.error("❌ [决策] LLM 返回无效，第 %d/2 次失败: %s", invalid_action_count, exc)
        if invalid_action_count >= 2:
            logger.warning("🛑 [决策] 连续两次无效输出，强制 finish_report")
            action = AgentAction(
                action="finish_report",
                reason="LLM action output was invalid twice; finishing report.",
            )
        else:
            action = AgentAction(
                action="review_code",
                reason=f"LLM action output invalid; fallback to review. Error: {exc}",
            )
        result = _with_action(state, action, loop_count, "failed", str(exc))
        result["invalid_action_count"] = invalid_action_count
        return ReviewState(**result)

    result = _with_action(state, action, loop_count, "selected", action.reason)
    result["invalid_action_count"] = invalid_action_count
    return ReviewState(**result)


def _with_action(
    state: ReviewState,
    action: AgentAction,
    loop_count: int,
    status: str,
    message: str,
) -> dict[str, Any]:
    """将 LLM 决策结果写入状态：next_action + 事件日志 + 进度步骤。"""
    return {
        "next_action": action.model_dump(mode="json"),
        "agent_loop_count": loop_count,
        "agent_events": append_event(state, action.action, action.reason, status, message),
        "step_progress": append_step(
            state,
            f"agent:{action.action.value}",
            "completed",
            action.reason,
        ),
    }


def _has_failed_tests(state: ReviewState) -> bool:
    return any(not result.get("passed", False) for result in state.get("test_results") or [])
