/**
 * 统一问题工作台的数据适配：本轮 Diff 运行记录与历史讨论复核风险
 * 共用同一套筛选和展示结构，来源单独标记，计数与条目必须一一对应。
 */
import type { HistoricalFindingView, ReviewSummary } from "../types/publication";
import type { RuntimeFindingGroup, RuntimeFindingRow } from "./taskRuntimePresentation";

/** 问题来源：本轮 Diff 的运行记录，或历史讨论复核确认仍存在的既有风险。 */
export type FindingSource = "all" | "current" | "historical";

/** 历史复核已核验风险在列表中的行；字段与当前问题对齐，便于共用筛选。 */
export interface HistoricalFindingRow {
  key: string;
  source: "historical";
  status: "historical_confirmed";
  group: "historical";
  severity: HistoricalFindingView["severity"];
  priority: HistoricalFindingView["priority"];
  title: string;
  affectedBehavior: string;
  recommendation: string;
  reason: string;
  filePath: string;
  line: null;
  discussionUrl: string | null;
  evidenceIds: string[];
}

export type WorkbenchFinding = (RuntimeFindingRow & { source: "current" }) | HistoricalFindingRow;

/** 与本轮记录指向同一缺陷、已在独立计数中合并的历史讨论。 */
export function mergedHistoricalKeys(historical: string[] | undefined, deduplicated: string[] | undefined): string[] {
  return (deduplicated ?? []).filter(key => (historical ?? []).includes(key));
}

/** 独立缺陷数；旧冻结计划缺少独立计数时，只合并明确标注为同一缺陷的历史记录。 */
export function independentFindingCount(keys: string[] | undefined, historical: string[] | undefined,
  independent: number | null | undefined, deduplicated: string[] | undefined): number | null {
  if (!keys) return null;
  return independent ?? keys.length + (historical ?? []).length - mergedHistoricalKeys(historical, deduplicated).length;
}

/**
 * 只取进入合并决策键列表的历史风险；已与本轮记录指向同一缺陷的不重复展示。
 * 旧冻结计划可能只有带证据编号的 verified_defects，此时保留条目但严重度保持未知，
 * 不从标题或路径推断。
 */
export function historicalFindingRows(plan: Pick<ReviewSummary, "digest" | "outcome"> | null | undefined): HistoricalFindingRow[] {
  const decision = plan?.digest.merge_decision;
  const history = plan?.outcome.historical_discussion;
  if (!decision || !history) return [];
  const listed = new Set([...(decision.blocking_historical_keys ?? []), ...(decision.nonblocking_historical_keys ?? [])]);
  const merged = new Set(mergedHistoricalKeys([...listed], decision.deduplicated_historical_keys));
  const rows = new Map<string, HistoricalFindingRow>();
  for (const item of history.findings ?? []) {
    if (!listed.has(item.discussion_id) || merged.has(item.discussion_id)) continue;
    rows.set(item.discussion_id, {
      key: item.discussion_id,
      source: "historical",
      status: "historical_confirmed",
      group: "historical",
      severity: item.severity,
      priority: item.priority,
      title: item.affected_behavior || item.reason,
      affectedBehavior: item.affected_behavior || "",
      recommendation: item.recommendation || "",
      reason: item.reason,
      filePath: item.path || "",
      line: null,
      discussionUrl: item.discussion_url,
      evidenceIds: item.evidence_ids,
    });
  }
  for (const item of history.verified_defects ?? []) {
    const identity = item.discussion_id;
    if (!identity || rows.has(identity) || !listed.has(identity) || merged.has(identity)) continue;
    if (!item.evidence_ids?.length) continue;
    rows.set(identity, {
      key: identity,
      source: "historical",
      status: "historical_confirmed",
      group: "historical",
      severity: "unknown",
      priority: "assess_impact",
      title: item.reason || "历史讨论确认的问题仍存在",
      affectedBehavior: "",
      recommendation: "",
      reason: item.reason || "未记录原因",
      filePath: item.path || "",
      line: null,
      discussionUrl: null,
      evidenceIds: [...item.evidence_ids],
    });
  }
  // 决策键存在但明细缺失时仍保留占位条目：宁可显示“明细不可用”，
  // 也不能让概览数量大于列表中可核对的条目。
  for (const identity of listed) {
    if (rows.has(identity) || merged.has(identity)) continue;
    rows.set(identity, {
      key: identity,
      source: "historical",
      status: "historical_confirmed",
      group: "historical",
      severity: "unknown",
      priority: "assess_impact",
      title: "历史复核记录缺少可展示的明细",
      affectedBehavior: "",
      recommendation: "",
      reason: "该历史讨论已计入合并建议，但当前快照未保存可展示的影响说明；请查看完整报告或原讨论。",
      filePath: "",
      line: null,
      discussionUrl: null,
      evidenceIds: [],
    });
  }
  return [...rows.values()];
}

export function workbenchFindings(current: RuntimeFindingRow[], historical: HistoricalFindingRow[]): WorkbenchFinding[] {
  return [...current.map(row => ({ ...row, source: "current" as const })), ...historical];
}

export const priorityLabels: Record<HistoricalFindingRow["priority"], string> = {
  fix_before_merge: "建议合并前修复",
  assess_improvement: "评估改进",
  assess_impact: "影响待核对",
};
