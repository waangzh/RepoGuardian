import type { ReviewTask, TaskRuntimeReport } from "../types/review";

const statusLabels: Record<string, string> = {
  not_scheduled: "未调度", duplicate: "重复线索", hint: "取证提示",
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

function relation(value: Record<string, unknown>): Record<string, unknown> {
  return value.relationship && typeof value.relationship === "object" && !Array.isArray(value.relationship)
    ? value.relationship as Record<string, unknown> : {};
}

export function relationshipGroups(values: Array<Record<string, unknown>> = []) {
  const groups = new Map<string, { key: string; label: string; reason: string; status: string; members: Array<Record<string, unknown>> }>();
  values.forEach((value, index) => {
    const r = relation(value);
    const known = Object.keys(r).length > 0;
    const key = JSON.stringify(known ? [r.source_file, r.target_file, r.type, value.reason, value.status] : ["legacy", index]);
    if (!groups.has(key)) groups.set(key, { key,
      label: known ? `${r.source_file} → ${r.target_file}（${r.type}）` : String(value.proposition || "未命名线索"),
      reason: String(value.reason || "未记录原因"), status: String(value.status || "unknown"), members: [] });
    groups.get(key)!.members.push(value);
  });
  return [...groups.values()];
}

export function relationshipEvidence(value: Record<string, unknown>): string {
  const r = relation(value);
  const sites = Array.isArray(r.call_sites) ? r.call_sites.filter((site): site is Record<string, unknown> =>
    Boolean(site) && typeof site === "object" && !Array.isArray(site)) : [];
  return `${value.lead_id || "未记录 ID"} · ${r.source || "未知调用方"} → ${r.target || "未知目标"}`
    + ` · 调用位置 ${sites.map(site => `${site.line ?? "?"}: ${site.expression ?? "?"}`).join(", ") || "未记录"}`
    + ` · ${r.resolution_status || "unknown"} / ${r.resolution_basis || "unknown"}`
    + ` · 关联目标 ${Array.isArray(value.related_goal_ids) ? value.related_goal_ids.join(", ") : "未记录"}`;
}

export function investigationRelationships(value: Record<string, unknown>): Array<Record<string, unknown>> {
  return Array.isArray(value.relationship_leads) ? value.relationship_leads.filter((item): item is Record<string, unknown> =>
    Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
}
