import type { ReviewTask, TaskRuntimeReport } from "../types/review";

const statusLabels: Record<string, string> = {
  examined: "已检查", blocked: "证据阻塞", not_examined: "尚未检查", unsupported: "不支持检查",
  answered: "已有答案", insufficient_evidence: "证据不足", not_executed: "未执行",
  confirmed: "已确认", dismissed: "已驳回", candidate: "待验证候选", unresolved: "未确认",
  supported: "支持命题", refuted: "反驳命题", complete: "完整", unknown: "未知", inconsistent: "不一致",
  plan_exhausted: "计划处理结束", budget_exhausted: "预算耗尽", deadline: "运行超时",
  no_progress: "无进展停止", technical_failure: "技术失败", cancelled: "已取消", running: "运行中",
};

export function runtimeLabel(value: unknown): string {
  const key = typeof value === "string" ? value : "unknown";
  return statusLabels[key] || key;
}

export function runtimeReport(task: Pick<ReviewTask, "engine" | "task_runtime_report">): TaskRuntimeReport | null {
  return task.engine === "task_review_v1" && task.task_runtime_report?.coverage_version === "task-goals-v1"
    ? task.task_runtime_report : null;
}

export function retryableRuntimeUnits(report: TaskRuntimeReport): string[] {
  const gaps = new Set(report.initial_scope_coverage.filter(value => ["blocked", "not_examined"].includes(String(value.status)))
    .map(value => String(value.goal_id)));
  return [...new Set(report.work_items.filter(item => item.state !== "unknown" && item.state !== "running"
    && Array.isArray(item.goal_ids) && item.goal_ids.some(goal => gaps.has(String(goal))))
    .map(item => String(item.unit_id || item.work_item_id)))];
}
