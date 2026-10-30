import type { ReviewTask, ReviewReport, AgentResult, FileContext } from '../types/review';

export const terminal = (status: string) => ['completed', 'completed_with_warnings', 'failed', 'cancelled'].includes(status);
export const reviewerLabel = (role: string) => ({ reviewer: '主审', contract: '契约审查', specialist: '风险专家', verifier: '独立核验', investigator: '定向调查', limitation_reviewer: '限制必要性复核' }[role] || role);
export const decisionLabel = (decision: string) => ({ confirmed: '已确认', refuted: '已排除', insufficient_evidence: '证据不足' }[decision] || '尚未核验');
export const phaseLabel = (phase: string) => ({ queued:'等待执行', prepare:'准备固定快照', planning:'规划完成', reviewing:'独立审查中', verifying_issues:'核验候选与必要前提', reviewer:'主审', contract:'契约审查', specialist:'风险专家审查', collect:'聚合候选', verifier:'独立核验', investigator:'定向调查', limitation_reviewer:'限制必要性复核', arbitrate:'证据仲裁', report:'汇总审查事实', synthesis:'综合 PR 总览', completed:'审查结束', failed:'执行失败', cancelled:'已停止' }[phase] || phase);
export const executionErrorLabel = (code: string | null) => ({
  ReviewBudgetExceeded: '达到整轮模型调用或累计输入预算，未完成范围保留为未知',
  BatchVerificationIncomplete: '批量核验裁决缺失、重复或超出范围',
  GraphRecursionError: '达到执行步数上限，未提交最终审查结果',
  ModelCallLimitExceededError: '模型调用预算已耗尽',
  ToolCallLimitExceededError: '工具调用预算已耗尽',
  StructuredOutputValidationError: '最终输出未通过结构校验',
  MultipleStructuredOutputsError: '最终输出包含相互冲突的结构化结果',
  TimeoutError: '模型或工具调用超时', APITimeoutError: '模型请求超时',
  RateLimitError: '模型服务请求受限', AuthenticationError: '模型服务认证失败',
  APIConnectionError: '模型服务连接失败', review_deadline: '达到本轮审查总期限',
  review_agents_failed: '所有独立审查分支执行失败',
  review_checkpoint_stage_changed: '旧检查点包含无法安全转换的混合阶段，原记录已保留，请新建审查',
  limitation_assessment_members_mismatch: '限制复核成员缺失、重复或超出本批范围',
  limitation_assessment_goal_outside_scope: '限制复核引用了该声明范围外的审查目标',
  limitation_assessment_evidence_not_read: '限制复核引用了本分支未读取的源码证据；搜索与分页标识不能作为证据',
  limitation_assessment_change_source_missing: '限制复核缺少对应变更文件与侧别的源码证据',
  limitation_assessment_structural_source_missing: '结构性限制复核缺少目标文件之外的 HEAD 引用方源码',
  limitation_assessment_source_invalid: '限制复核的归档源码证据不可用或完整性校验失败',
}[code || ''] || '执行异常，未取得有效结果');
export function resumeLabel(task: ReviewTask): string | null {
  if (!['failed', 'completed_with_warnings'].includes(task.status)) return null;
  const report = task.review_report;
  if (!report || report.termination === 'completed') return task.status === 'failed' ? '从检查点继续' : null;
  const failed = [...report.discovery_results, ...report.verification_results].filter(item => item.status === 'failed');
  const retiredFailure = report.limitation_results.some(item => item.status === 'failed');
  const limits = task.configuration.limits;
  if (task.retry_count || (!failed.length && !retiredFailure) || !limits) return null;
  if (!failed.every(item => item.model_calls != null && item.tool_calls != null
    && item.model_calls < limits.model_calls && item.tool_calls < limits.tool_calls)) return null;
  if (Date.now() - Date.parse(task.created_at) >= limits.timeout_seconds * 1000) return null;
  const budget = report.budget;
  if (failed.length && budget && ((budget.model_calls ?? 0) >= limits.total_model_calls
    || (budget.committed_input_tokens ?? 0) >= limits.total_input_tokens)) return null;
  return failed.length ? '补查失败分支' : '按当前流程完成汇总';
}
export function orderedProgress(task: ReviewTask) {
  return [...task.steps].sort((left, right) => Date.parse(left.started_at || left.updated_at)
    - Date.parse(right.started_at || right.updated_at));
}
export function progressSummary(task: ReviewTask) {
  const running = task.steps.filter(step => step.status === 'running');
  const report = task.review_report;
  return { running, candidates: report?.candidates.length ?? null,
    confirmed: report?.findings.length ?? null,
    examined: report?.goals.filter(goal => goal.status === 'examined').length ?? null,
    total: report?.goals.length ?? null };
}
export function observedUsage(report: ReviewReport | null) {
  if (!report) return { calls: null, tokens: null };
  if (report.budget && report.budget.model_calls != null) {
    const budget = report.budget;
    return { calls: budget.model_calls, tokens: budget.input_tokens == null || budget.output_tokens == null
      ? null : budget.input_tokens + budget.output_tokens };
  }
  const seen = new Set<string>();
  const results: AgentResult[] = [...report.discovery_results, ...report.verification_results, ...report.limitation_results].filter(item => {
    if (!item.execution_id) return true;
    if (seen.has(item.execution_id)) return false;
    seen.add(item.execution_id); return true;
  });
  return {
    calls: results.some(item => item.model_calls === null) ? null : results.reduce((sum, item) => sum + (item.model_calls ?? 0), 0),
    tokens: results.some(item => item.input_tokens === null || item.output_tokens === null) ? null
      : results.reduce((sum, item) => sum + item.input_tokens! + item.output_tokens!, 0),
  };
}
export function safeGitHubLink(value: string | null | undefined): string | undefined {
  try { const url = new URL(value || ''); return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}
export function isFileContextFor(value: unknown, fileId: number, path: string, snapshot: ReviewTask['snapshot']): value is FileContext {
  if (!value || typeof value !== 'object') return false;
  const context = value as Partial<FileContext>;
  return context.file_id === fileId && context.file_path === path
    && context.snapshot?.head_sha === snapshot.head_sha
    && context.snapshot?.effective_base_sha === snapshot.effective_base_sha
    && context.snapshot?.declared_base_sha === snapshot.declared_base_sha
    && Array.isArray(context.base_lines) && context.base_lines.every(line => typeof line === 'string')
    && Array.isArray(context.head_lines) && context.head_lines.every(line => typeof line === 'string');
}
export function defaultPlan(depth: 'quick' | 'standard' | 'deep' = 'standard', discussions = true) {
  return { depth, include_pr_discussions: discussions,
    focus: ['correctness', 'security', 'performance', 'compatibility'] as const,
    scope: { mode: 'all' as const, primary_files: [] as string[] },
    limits: { model_calls: null, tool_calls: null, concurrency: null, timeout_seconds: null,
      output_tokens: null, total_model_calls: null, total_input_tokens: null } };
}
