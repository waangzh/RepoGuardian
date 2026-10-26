import type { ReviewTask, ReviewReport, AgentResult } from '../types/review';

export const terminal = (status: string) => ['completed', 'completed_with_warnings', 'failed', 'cancelled'].includes(status);
export const reviewerLabel = (role: string) => ({ behavior: '行为审查', contract: '契约审查', specialist: '风险专家', verifier: '独立核验', investigator: '定向调查' }[role] || role);
export const decisionLabel = (decision: string) => ({ confirmed: '已确认', refuted: '已排除', insufficient_evidence: '证据不足' }[decision] || '尚未核验');
export const phaseLabel = (phase: string) => ({ prepare:'准备固定快照', planning:'规划完成', behavior:'行为审查', contract:'契约审查', specialist:'风险专家审查', collect:'聚合候选', verifier:'独立核验', investigator:'定向调查', arbitrate:'证据仲裁', report:'生成报告', completed:'审查结束', failed:'执行失败', cancelled:'已停止' }[phase] || phase);
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
  const results: AgentResult[] = [...report.discovery_results, ...report.verification_results];
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
export function defaultPlan(depth: 'quick' | 'standard' | 'deep' = 'standard', discussions = true) {
  return { depth, include_pr_discussions: discussions,
    focus: ['correctness', 'security', 'performance', 'compatibility'] as const,
    scope: { mode: 'all' as const, primary_files: [] as string[] },
    limits: { model_calls: null, tool_calls: null, concurrency: null, timeout_seconds: null, output_tokens: null } };
}
