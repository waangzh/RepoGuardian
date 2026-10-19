import type { PublicationPlan, ReviewOutcome } from '../types/publication';

export const coverageLabel = (value?: string | null): string =>
  ({ complete: '完整', partial: '部分完成', unknown: '未知' })[value || 'unknown'] || '未知';

export function reportValidationLabel(value?: ReviewOutcome['dynamic_validation']): string {
  return ({ not_executed: '未执行', passed: '当前 Head 已通过', failed: '当前 Head 未通过',
    independent_results: '独立结果 · Head 未知', unknown: '未知' })[value || 'unknown'];
}

export function reportLimitations(plan: PublicationPlan): string[] {
  const digest = plan.digest;
  return [...new Set([
    ...(digest.merge_decision?.limits || []),
    ...(digest.author_context_limitation ? [digest.author_context_limitation] : []),
    ...(digest.unresolved_leads || []),
    ...digest.incomplete_goals.map(goal => `${goal.path || goal.goal_id || '检查目标'}：${goal.reason || goal.status}`),
    ...digest.excluded_files.map(file => `${file.path}：${file.reason}`),
    ...(digest.investigations || []).filter(item => item.answer === 'unresolved').map(item => `${item.proposition}：${item.reason || '尚未核验'}`),
  ])];
}

/** Only link to a frozen GitHub source location, never to a model-provided URL. */
export function reportSourceLink(prUrl: string | null | undefined, path: string, sha: string | null, line?: number | null): string | null {
  if (!sha || !/^[a-f0-9]{40}$/i.test(sha) || !path || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..')) return null;
  try {
    const url = new URL(prUrl || '');
    const parts = url.pathname.split('/').filter(Boolean);
    if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password
      || parts.length !== 4 || parts[2] !== 'pull' || !/^\d+$/.test(parts[3]!)) return null;
    return `https://github.com/${parts[0]}/${parts[1]}/blob/${sha}/${path.split('/').map(encodeURIComponent).join('/')}${line && Number.isSafeInteger(line) && line > 0 ? `#L${line}` : ''}`;
  } catch { return null; }
}
