import type { PublicationFinding, PublicationPlan, ReviewOutcome } from '../types/publication';
import type { FindingView } from '../types/reviewPlan';

export function publicationFindingTitle(finding: PublicationFinding): string {
  if (finding.raw_title != null) return finding.raw_title;
  // Undo only the old markdown_text encoding, not arbitrary Markdown or HTML.
  const entities: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': "'", '&#64;': '@' };
  return finding.title.replace(/\\([\\`*_{}\[\]()#+.!|~>\-])/g, '$1')
    .replace(/&(?:amp|lt|gt|quot|#x27|#64);/g, value => entities[value]!);
}

export const coverageLabel = (value?: string | null): string =>
  ({ complete: '完整', partial: '不完整', unknown: '未知' })[value || 'unknown'] || '未知';

export const severityLabel = (value?: string | null): string =>
  ({ critical: '严重', high: '高', medium: '中', low: '低' } as Record<string, string>)[value || ''] || value || '未知';

export const reviewStatusLabel = (value: FindingView['review_status']): string => ({
  confirmed: '代码问题已确认，但暂不能生成可发布评论', candidate: '候选', unresolved: '尚未确认',
  dismissed: '已排除', unknown: '未知',
})[value] || '未知';

export const dispositionLabel = (value: string): string => ({
  open: '重新打开', confirmed: '人工确认', ignored: '已忽略', resolved: '已处理',
} as Record<string, string>)[value] || value;

/** 与后端 publication_renderer.REASON_LABELS 保持一致；未知代码保留原文。 */
const REASON_LABELS: Record<string, string> = {
  task_failed: '任务或运行时执行失败', task_cancelled: '任务或运行时已取消',
  termination_incomplete: '任务未可靠完成全部计划', scope_incomplete: '部分目标未完成检查',
  budget_exhausted: '审查预算耗尽，请核对是否仍有未完成目标',
  deadline_exceeded: '审查达到时间上限，请核对是否仍有未完成目标或调查',
  scope_empty: '没有实际审查目标', verification_unresolved: '仍有未核实的候选问题',
  finding_status_unknown: '存在未知的问题状态', investigation_incomplete: '仍有未完成或证据不足的调查',
  unscheduled_investigation: '仍有未处理的调查线索', usage_integrity_unknown: '模型请求计量不完整或不一致',
  snapshot_missing: '审查快照缺失或身份无效', runtime_report_missing: '缺少任务运行报告',
  runtime_report_invalid: '任务运行报告格式无效', unsupported_legacy_projection: '历史引擎不支持此发布协议，请查看原始审计报告',
  historical_discussion_unresolved: '历史讨论复核确认原问题在当前代码中仍存在，需核对',
};
export const reportReasonLabel = (value: string): string => REASON_LABELS[value] || value;

/** 与后端 review_publication / publication_planner 的扣留原因保持一致。 */
const WITHHELD_REASON_LABELS: Record<string, string> = {
  finding_not_confirmed: '问题未处于已确认状态', publication_contract_missing: '缺少发布契约记录',
  verification_not_supported: '独立核验未支持该结论', publication_usage_unknown: '核验用量未知，不能视为完整证据',
  publication_text_missing: '缺少可发布正文', snapshot_mismatch: '证据快照与本次审查快照不一致',
  publication_subject_mismatch: '发布正文与核验对象不一致', publication_evidence_missing: '缺少代码证据或依据',
  issue_policy_failed: '未通过问题准入策略', placement_not_publishable: '问题位置不满足发布要求',
  duplicate_finding_conflict: '同一问题的记录存在冲突', review_content_blocked: '审查内容因运行或快照问题被整体阻断',
};
export const withheldReasonLabel = (value: string): string => WITHHELD_REASON_LABELS[value] || value;

export function reportValidationLabel(value?: ReviewOutcome['dynamic_validation']): string {
  return ({ not_executed: '当前 PR 版本未执行', passed: '当前 PR 版本已记录的对应检查通过', failed: '本次记录的动态检查未通过',
    independent_results: '独立结果 · Head 未知', unknown: '未知' })[value || 'unknown'];
}

export function reviewScopeLabel(examined: number | null, total: number | null): string {
  if (examined === null || total === null) return "检查范围尚不明确";
  const remaining = total - examined;
  return `已检查 ${examined}/${total} 项约定范围${remaining > 0 ? `，${remaining} 项待补查` : ""}`;
}

/** 旧计划的系统前缀只在展示时去除；输入完整性与核验状态仍保留在 Digest。 */
export function reportAuthorGoal(plan: PublicationPlan): string {
  const text = plan.digest.change_understanding?.author_goal || plan.digest.author_goal || '';
  const prefix = '作者声明（未经独立验证）：';
  return (text.startsWith(prefix) ? text.slice(prefix.length).replace(/（作者声明的输入范围有限制，见下方说明。）$/, '') : text).trim()
    || '未记录明确的作者目标。';
}

/** 详细报告章节与后端 render_user_report 的结构保持一致；未发布记录只在存在时出现。 */
export function unpublishedFindings(plan: PublicationPlan): FindingView[] {
  const published = new Set([...plan.inline_drafts, ...plan.summary_findings].map(item => item.finding_key));
  return (plan.digest.findings || []).filter(item => !published.has(item.id));
}

export function reportChapters(plan: PublicationPlan): { id: string; label: string }[] {
  const items = [
    { id: 'conclusion', label: '审查结论' },
    { id: 'changes', label: '变更理解' },
    { id: 'evidence', label: '代码依据' },
  ];
  if (unpublishedFindings(plan).length) items.push({ id: 'retained', label: '未发布记录' });
  items.push({ id: 'gaps', label: '已知局限' }, { id: 'actions', label: '后续建议' }, { id: 'appendix', label: '附录 · 范围与记录' });
  let index = 0;
  return items.map(item => item.id === 'conclusion' || item.id === 'appendix'
    ? item : { ...item, label: `${String(++index).padStart(2, '0')} · ${item.label}` });
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
