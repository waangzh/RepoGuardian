import type { CrossUnitCoordinationPlan, CrossUnitFollowupResult, ReviewIssue, ReviewTask, ReviewUnitResult, UnitEvidenceReference } from "../types/review";

export const decisionLabels = { required: "需要跨组检查", uncertain: "关联尚不明确", skip: "本次可跳过" };
export const checkLabels: Record<string, string> = {
  checked: "已检查", not_checked: "未检查", unresolved: "未决", supported: "假设得到支持",
  refuted: "找到反证", verified: "依赖已核验", conflicting: "契约冲突",
};
export const reasonLabels: Record<string, string> = {
  changed_contract: "变更影响契约", unresolved_dependency: "依赖尚未核验", conflicting_contract: "契约结论冲突",
  dependency_coverage_gap: "关联组覆盖不足", unresolved_cross_unit_question: "跨组问题未决",
  relationship_unknown: "关联证据不足", summary_unknown: "检查记录不完整",
  independent_changes: "未触发跨组规则", no_cross_unit_scope: "不适用跨组检查",
};

export function isTerminal(status: string): boolean {
  return ["completed", "completed_with_warnings", "failed", "cancelled"].includes(status);
}

export function crossUnitPresentation(task: Pick<ReviewTask, "status" | "cross_unit_risk" | "coordination_plan">) {
  const risk = task.cross_unit_risk;
  const plan = task.coordination_plan;
  const terminal = isTerminal(task.status);
  let executionLabel = terminal ? "未保存执行记录" : "尚未开始";
  let executionTone = "pending";
  if (task.status === "cancelled" || plan?.status === "cancelled" || risk?.execution_status === "cancelled") {
    executionLabel = "已取消";
  } else if (plan?.status === "proposed") {
    executionLabel = terminal ? "协调未完成" : "正在生成协调计划";
    executionTone = terminal ? "warning" : "running";
  } else if (plan?.status === "validated") {
    executionLabel = terminal ? "补查未完成" : "定向补查进行中";
    executionTone = terminal ? "warning" : "running";
  } else if (plan || risk) {
    const status = plan?.status || risk?.execution_status;
    const labels: Record<string, string> = {
      completed: "协调已结束", unresolved: "仍有未决项", failed: "协调失败",
      not_requested: "未请求补查", not_implemented: terminal ? "未执行补查" : "等待协调",
    };
    executionLabel = labels[status || ""] || executionLabel;
    executionTone = status === "failed" ? "failed" : status === "unresolved" ? "warning"
      : status === "not_implemented" && !terminal ? "pending" : "neutral";
  }
  return {
    decisionLabel: risk ? decisionLabels[risk.decision] : terminal ? "筛查记录未知" : "尚未筛查",
    decisionTone: risk?.decision === "required" ? "warning" : risk?.decision === "uncertain" ? "inconclusive" : "neutral",
    executionLabel, executionTone,
  };
}

export function unitFindingLabel(result: ReviewUnitResult | undefined, count: number): string {
  if (count > 0) return `${count} 条问题记录`;
  if (!result) return "等待审查";
  if (result.status !== "completed") return "检查未完成";
  return "未报告问题";
}

export function coordinationDecisionLabel(plan: CrossUnitCoordinationPlan): string {
  if (plan.status === "proposed") return "判别尚未完成";
  if (plan.status === "failed") return "未形成完整协调结论";
  if (plan.status === "cancelled") return "协调已取消";
  return decisionLabels[plan.decision];
}

export function followupPresentation(result: CrossUnitFollowupResult | undefined, taskStatus: string) {
  if (!result) return { label: isTerminal(taskStatus) ? "未执行或未保存结果" : "等待执行", tone: "pending", validation: "尚无候选批次" };
  const labels = { candidate_found: "发现候选", refuted: "找到反证", unresolved: "未决", failed: "补查失败" };
  return { label: labels[result.outcome], tone: result.outcome === "failed" ? "failed" : result.outcome === "unresolved" ? "warning" : "neutral",
    validation: result.validation_status === "completed" ? "候选处理已结束" : result.validation_status === "pending" ? "候选处理未结束" : "候选处理状态未知" };
}

export function publishedFollowupIssues(result: CrossUnitFollowupResult | undefined, issues: ReviewIssue[]): ReviewIssue[] {
  const ids = new Set(result?.unit_result?.issues.map((issue) => issue.id) || []);
  return issues.filter((issue) => ["confirmed", "needs_human", "published"].includes(issue.status)
    && (ids.has(issue.id) || issue.source_issue_ids?.some((id) => ids.has(id))));
}

export function issueStatusBreakdown(result: CrossUnitFollowupResult | undefined) {
  const issues = result?.unit_result?.issues || [];
  const labels: Record<string, string> = { candidate: "候选待核验", evidence_resolved: "证据已定位", confirmed: "已确认", published: "已发布", needs_human: "待人工", dismissed: "已过滤" };
  const counts = Object.entries(labels).map(([status, label]) => ({ label, count: issues.filter((issue) => issue.status === status).length })).filter((item) => item.count > 0);
  const unknown = issues.length - counts.reduce((sum, item) => sum + item.count, 0);
  return unknown > 0 ? [...counts, { label: "状态未记录", count: unknown }] : counts;
}

export function evidenceSnapshotState(reference: UnitEvidenceReference, baseSha?: string, headSha?: string): "current" | "stale" | "unknown" {
  if ((baseSha && reference.base_sha && baseSha !== reference.base_sha) || (headSha && reference.head_sha && headSha !== reference.head_sha)) return "stale";
  return baseSha && headSha && reference.base_sha && reference.head_sha ? "current" : "unknown";
}

export function originalIssueUnit(issue: ReviewIssue, unitIds: string[]): string {
  return [issue.review_unit_id, ...(issue.source_review_unit_ids || [])].find((id) => unitIds.includes(id)) || "";
}

export function budgetPercent(used: number, maximum: number): number {
  return maximum > 0 ? Math.max(0, Math.min(100, used / maximum * 100)) : used > 0 ? 100 : 0;
}

export function formatCost(value: number | null | undefined): string {
  return value == null ? "未提供" : `$${(value / 1_000_000).toFixed(6)}`;
}

export function evidenceCatalog(task: Pick<ReviewTask, "review_unit_results" | "followup_results">): UnitEvidenceReference[] {
  const byId = new Map<string, UnitEvidenceReference>();
  const results = [...(task.review_unit_results || []), ...(task.followup_results || []).flatMap((item) => item.unit_result ? [item.unit_result] : [])];
  for (const result of results) for (const evidence of result.review_summary?.evidence || []) byId.set(evidence.id, evidence);
  return [...byId.values()];
}
