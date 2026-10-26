import type { ReviewHistoryTask } from "../types/operations";
import { reviewSituation } from "./reviewSituation";

export function historyConclusion(task: ReviewHistoryTask) {
  if (!["completed", "completed_with_warnings", "failed", "cancelled"].includes(task.status)) {
    return { title: "尚未形成结论", tone: "info", description: "审查尚未结束，请查看执行状态。" };
  }
  const result = task.history_result;
  if (!result) return { title: "结论未知", tone: "warning", description: "历史记录未提供结论投影，请打开详情核对。" };
  const situation = reviewSituation({ outcome: result.outcome, digest: { merge_decision: result.merge_decision } })!;
  const titles: Record<string, string> = {
    failed: "审查中断", cancelled: "审查已停止", blockers: "存在高优先级问题",
evidence_gaps: "仍有事项需确认",
    scope_incomplete: "检查范围未完整覆盖", runtime_limits: "结果可靠性需核对",
    nonblocking: "有改进建议，暂无高优先级问题", clean: "未发现需在合并前处理的问题",
  };
  const historical = result.merge_decision.unresolved_reasons?.includes("historical_discussion_unresolved")
    && situation.key === "evidence_gaps";
  return { title: historical ? "历史问题经核验仍存在" : titles[situation.key] || "结论未知",
    tone: situation.tone, description: situation.title };
}
