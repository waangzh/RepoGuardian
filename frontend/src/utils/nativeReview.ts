import type { FileContext, Report, Run, ReviewOptions } from '../types/review';

export const terminal = (status: string) =>
  ['completed', 'partial', 'failed', 'cancelled'].includes(status);

export const phaseLabel = (phase: string) =>
  ({
    queued: '等待执行',
    prepare: '准备固定快照',
    review: '审查中',
    grouping: '组织审查单元',
    planning: '规划审查单元',
    reviewing: '审查中',
    verification: '核验候选',
    report: '汇总报告',
    running: '执行中',
    finished: '审查结束',
    completed: '审查结束',
    partial: '部分完成',
    failed: '执行失败',
    cancelled: '已停止',
  })[phase] || phase;

export const executionErrorLabel = (code: string | null) =>
  ({
    ReviewBudgetExceeded: '达到审查预算，未完成范围保持未知',
    agent_model_limit: '达到单个 Agent 的模型调用上限，调查尚未完成',
    agent_structured_output_missing: '模型未返回有效的结构化结果，任务尚未完成',
    model_call_limit: '达到整轮模型调用上限，未完成范围保持未知',
    input_token_limit: '达到整轮输入 Token 上限，未完成范围保持未知',
    output_token_limit: '达到整轮输出 Token 上限，未完成范围保持未知',
    context_capacity: '模型上下文容量不足，调查尚未完成',
    tool_call_limit: '达到工具调用上限，调查尚未完成',
    agent_no_progress: 'Agent 重复执行相同调查，没有取得进展',
    review_cutoff: '达到调查截止时间，未完成范围保持未知',
    hard_deadline: '达到本轮审查总期限',
    TimeoutError: '模型或工具调用超时',
    APITimeoutError: '模型请求超时',
    RateLimitError: '模型服务请求受限',
    AuthenticationError: '模型服务认证失败',
    APIConnectionError: '模型服务连接失败',
    review_deadline: '达到本轮审查总期限',
  })[code || ''] || '执行异常，未取得有效结果';

export function observedUsage(report: Report | null) {
  if (!report) return {
    calls: null, tokens: null, input: null, output: null,
    cachedInput: null, uncachedInput: null, cacheHitRate: null,
  };

  const usage = report.usage;
  const calls = typeof usage.model_calls === 'number' ? usage.model_calls : null;
  const input = typeof usage.input_tokens === 'number' ? usage.input_tokens : null;
  const output = typeof usage.output_tokens === 'number' ? usage.output_tokens : null;
  const cachedInput = typeof usage.cached_input_tokens === 'number'
    ? usage.cached_input_tokens : null;
  const uncachedInput = typeof usage.uncached_input_tokens === 'number'
    ? usage.uncached_input_tokens : null;
  const cacheHitRate = typeof usage.cache_hit_rate === 'number'
    ? usage.cache_hit_rate : null;

  return {
    calls, tokens: input == null || output == null ? null : input + output,
    input, output, cachedInput, uncachedInput, cacheHitRate,
  };
}

export function safeGitHubLink(value: string | null | undefined): string | undefined {
  try {
    const url = new URL(value || '');
    return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

export function isFileContextFor(
  value: unknown,
  fileId: number,
  path: string,
  snapshot: Run['snapshot'],
): value is FileContext {
  if (!value || typeof value !== 'object') return false;

  const context = value as Partial<FileContext>;
  return context.file_id === fileId
    && context.file_path === path
    && context.snapshot?.head_sha === snapshot.head_sha
    && context.snapshot?.base_sha === snapshot.base_sha
    && context.snapshot?.declared_base_sha === snapshot.declared_base_sha
    && Array.isArray(context.base_lines)
    && Array.isArray(context.head_lines);
}

export function defaultPlan(
  depth: ReviewOptions['depth'] = 'standard',
  discussions = false,
): ReviewOptions {
  return {
    depth,
    paths: null,
    focus: ['correctness', 'security', 'performance', 'compatibility'],
    include_pr_discussions: discussions,
    limits: {
      model_calls: null,
      tool_calls: null,
      concurrency: null,
      timeout_seconds: null,
      output_tokens: null,
      total_model_calls: null,
      total_input_tokens: null,
      total_output_tokens: null,
      context_tokens: null,
    },
  };
}
