import type { ReviewIssue, ReviewTask, TaskRuntimeReport } from "../types/review";

const statusLabels: Record<string, string> = {
  not_scheduled: "未调度", duplicate: "重复线索", hint: "取证提示",
  examined: "已检查", blocked: "证据阻塞", not_examined: "尚未检查", unsupported: "不支持检查",
  answered: "已有答案", insufficient_evidence: "证据不足", not_executed: "未执行",
  confirmed: "已确认", dismissed: "已驳回", candidate: "待验证候选", unresolved: "未确认",
  supported: "支持命题", refuted: "反驳命题", complete: "完整", unknown: "未知", inconsistent: "不一致",
  plan_exhausted: "计划处理结束", budget_exhausted: "预算耗尽", deadline: "运行超时",
  no_progress: "无进展停止", technical_failure: "技术失败", cancelled: "已取消", running: "运行中",
  completed: "本轮审查已结束", failed: "审查失败", published: "已发布（报告）",
};

export interface TaskRuntimeSummary {
  execution: { status: string; termination_reason: string; scope_complete: boolean };
  findings: { total: number; active: number; unknown: number; confirmed: number; dismissed: number; unresolved: number; without_issue: number; publishable: number };
  investigations: { total: number; answered: number; insufficient_evidence: number; not_executed: number };
  scope: { total: number; examined: number; incomplete: number; blocked: number; unsupported: number; not_examined: number };
  usage: { integrity: string; unknown_attempts: number };
}

export function taskRuntimeSummary(report: TaskRuntimeReport): TaskRuntimeSummary {
  const findings = report.findings || [];
  const status = (item: Record<string, unknown>) => String(item.status || "unknown");
  const investigations = report.admitted_investigations || [];
  const scopes = report.initial_scope_coverage || [];
  return {
    execution: { status: report.execution_status, termination_reason: report.termination_reason, scope_complete: report.scope_complete },
    findings: {
      total: findings.length,
      active: findings.filter(item => findingGroup(status(item)) !== "dismissed").length,
      unknown: findings.filter(item => findingGroup(status(item)) === "unknown").length,
      confirmed: findings.filter(item => findingGroup(status(item)) === "confirmed").length,
      dismissed: findings.filter(item => status(item) === "dismissed").length,
      unresolved: findings.filter(item => ["candidate", "unresolved"].includes(status(item))).length,
      without_issue: findings.filter(item => !item.issue).length,
      publishable: findings.filter(item => ["confirmed", "published"].includes(status(item))
        && Boolean((item.publication as Record<string, unknown> | undefined)?.published_text)).length,
    },
    investigations: {
      total: investigations.length,
      answered: investigations.filter(item => status(item) === "answered").length,
      insufficient_evidence: investigations.filter(item => status(item) === "insufficient_evidence").length,
      not_executed: investigations.filter(item => status(item) === "not_executed").length,
    },
    scope: {
      total: scopes.length,
      examined: scopes.filter(item => status(item) === "examined").length,
      incomplete: scopes.filter(item => status(item) !== "examined").length,
      blocked: scopes.filter(item => status(item) === "blocked").length,
      unsupported: scopes.filter(item => status(item) === "unsupported").length,
      not_examined: scopes.filter(item => status(item) === "not_examined").length,
    },
    usage: { integrity: report.usage_integrity, unknown_attempts: report.unknown_attempts },
  };
}


export type RuntimeFindingGroup = "confirmed" | "unresolved" | "dismissed" | "unknown";
export type RuntimeFindingFilter = "active" | RuntimeFindingGroup;

export function findingGroup(status: string): RuntimeFindingGroup {
  if (["confirmed", "published"].includes(status)) return "confirmed";
  if (["candidate", "unresolved"].includes(status)) return "unresolved";
  return status === "dismissed" ? "dismissed" : "unknown";
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown): string { return typeof value === "string" ? value : ""; }

export interface RuntimeFindingRow {
  key: string; status: string; group: RuntimeFindingGroup; title: string;
  failureScenario: string; affectedBehavior: string; recommendation: string;
  reason: string; rawReason: string; filePath: string; line: number | null; issue: ReviewIssue | null;
}

export function runtimeFindingRows(report: TaskRuntimeReport, issues: ReviewIssue[] = []): RuntimeFindingRow[] {
  return (report.findings || []).map((value, index) => {
    const candidate = asRecord(value.candidate);
    const rawIssue = asRecord(value.issue);
    const publication = asRecord(asRecord(value.publication).published_text);
    const evidence = asRecord(rawIssue.primary_evidence);
    const sources = Array.isArray(value.source_candidate_ids) ? value.source_candidate_ids : [];
    const issue = issues.find(item => item.id === rawIssue.id || item.source_issue_ids?.some(id => sources.includes(id))) || null;
    const status = text(value.status) || "unknown";
    const rawReason = text(value.reason);
    const reasons: Record<string, string> = {
      candidate_derivation_incomplete: "候选处理尚未完成，证据和结论仍需确认。",
      independent_verification_refuted: "独立核验未支持该问题，已从待处理事项中排除。",
      verification_usage_unknown: "核验请求的用量状态未知，本次结果尚未确认为有效结论。",
    };
    const field = (name: string) => text(rawIssue[name]) || text(publication[name]) || text(candidate[name]);
    return {
      key: text(value.key) || text(candidate.candidate_id) || `finding-${index}`,
      status, group: findingGroup(status), title: text(rawIssue.title) || text(publication.claim) || text(candidate.claim) || "未命名发现",
      failureScenario: field("failure_scenario"), affectedBehavior: field("affected_behavior"), recommendation: field("recommendation"),
      reason: reasons[rawReason] || (/^[a-z][a-z0-9_:;.-]*$/.test(rawReason) ? "本条记录存在处理限制，展开技术详情查看原始原因。" : rawReason),
      rawReason, filePath: text(evidence.file_path),
      line: typeof evidence.resolved_start_line === "number" && Number.isInteger(evidence.resolved_start_line) && evidence.resolved_start_line > 0 ? evidence.resolved_start_line : null,
      issue,
    };
  });
}

export function filterRuntimeFindings(rows: RuntimeFindingRow[], filter: RuntimeFindingFilter): RuntimeFindingRow[] {
  return rows.filter(row => filter === "active" ? row.group !== "dismissed" : row.group === filter);
}

export function runtimeWarnings(report: TaskRuntimeReport): string[] {
  const summary = taskRuntimeSummary(report);
  const warnings: string[] = [];
  if (report.execution_status === "failed") warnings.push(report.failure_summary || "审查遇到技术故障，尚未形成完整结论。");
  if (report.execution_status === "cancelled") warnings.push("审查已取消，以下仅保留停止前取得的结果。");
  if (summary.scope.incomplete) warnings.push(`${summary.scope.incomplete} 项约定目标尚未完成检查。`);
  if (!summary.scope.total) warnings.push("尚无可核对的范围记录，不能据此判断检查完整性。");
  const investigations = summary.investigations;
  if (investigations.insufficient_evidence) warnings.push(`${investigations.insufficient_evidence} 项调查证据不足，需要补充确认。`);
  if (investigations.not_executed) warnings.push(`${investigations.not_executed} 项调查尚未执行。`);
  const unknownInvestigations = investigations.total - investigations.answered - investigations.insufficient_evidence - investigations.not_executed;
  if (unknownInvestigations) warnings.push(`${unknownInvestigations} 项调查状态未知。`);
  if (summary.findings.unknown) warnings.push(`${summary.findings.unknown} 项发现状态未知，已保留原始记录。`);
  if (report.usage_integrity !== "complete") warnings.push(report.usage_integrity === "inconsistent"
    ? "用量记录不一致，当前结算数不能视作完整消耗。"
    : `本次用量尚不完整，${report.unknown_attempts} 次调用用量未知。`);
  if (["budget_exhausted", "deadline", "no_progress"].includes(report.termination_reason)) warnings.push(`${runtimeLabel(report.termination_reason)}；执行停止不表示所有调查均已完成。`);
  return warnings;
}

export function reviewCompletionMessage(task: ReviewTask): string {
  const report = runtimeReport(task);
  if (!report) return task.issues.length ? `发现 ${task.issues.length} 个需关注的问题，报告已生成。` : "未发现达到报告门槛的问题，报告已生成。";
  const summary = taskRuntimeSummary(report);
  return `${summary.findings.confirmed} 项已确认问题，${summary.findings.unresolved} 项待确认候选。`
    + (runtimeWarnings(report).join(" ") || "约定范围已检查，报告已生成。");
}

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
