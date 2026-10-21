import type { MergeDecision, ReviewOutcome } from "../types/publication";
import type { ReviewSection } from "../types/workspace";

/** 展示层情景：由后端内部状态组合成用户可理解的单一结论，不新增后端顶层状态。 */
export type SituationKey =
  | "stale" | "failed" | "cancelled" | "validation_failed" | "blockers"
  | "evidence_gaps" | "scope_incomplete" | "runtime_limits" | "nonblocking" | "clean";

export type SituationTone = "success" | "info" | "warning" | "danger";

export type SituationActionKey =
  | "view_blockers" | "view_unresolved" | "view_findings" | "view_gaps"
  | "view_validation" | "view_error" | "view_activity" | "view_retained"
  | "view_report" | "view_investigations" | "view_limits" | "rereview";

export interface SituationAction {
  key: SituationActionKey;
  label: string;
  icon: string;
}

/** 复制是交付动作而不是导航动作，单独给出标签与主次，避免失败/取消时被当作完成审查。 */
export interface SituationCopy {
  label: string;
  prominence: "primary" | "secondary";
  /** 仅失败/取消为 true：复制内容只是停止前保留的部分记录。 */
  partial: boolean;
}

export interface ReviewSituation {
  key: SituationKey;
  /** 审查事实：代码中发现了什么，以及结论覆盖的范围。 */
  title: string;
  /** 事实边界：结论覆盖什么、不覆盖什么；不写操作指令。 */
  description: string;
  /** 下一步建议：用户现在应该怎么办；与操作按钮互补，不替代事实。 */
  nextStep: string;
  tone: SituationTone;
  icon: string;
  blockingCount: number | null;
  nonblockingCount: number;
  unresolvedCount: number;
  examinedGoals: number;
  totalGoals: number;
  actions: SituationAction[];
  copy: SituationCopy | null;
}

export interface SituationSource {
  outcome: ReviewOutcome;
  digest: { merge_decision?: MergeDecision };
}

const ACTION_META: Record<SituationActionKey, { label: string; icon: string }> = {
  view_blockers: { label: "查看高优先级问题", icon: "alert" },
  view_unresolved: { label: "查看未确认事项", icon: "search" },
  view_findings: { label: "查看建议", icon: "search" },
  view_gaps: { label: "查看未检查范围", icon: "shield" },
  view_validation: { label: "查看失败记录", icon: "flask" },
  view_error: { label: "查看错误", icon: "alert" },
  view_activity: { label: "查看技术记录", icon: "activity" },
  view_retained: { label: "查看已保留结果", icon: "search" },
  view_report: { label: "查看完整报告", icon: "report" },
  view_investigations: { label: "查看未完成调查", icon: "search" },
  view_limits: { label: "查看运行限制", icon: "activity" },
  rereview: { label: "重新审查此 PR", icon: "plus" },
};

const COPY_PR = { label: "复制 PR 评论", prominence: "primary" as const, partial: false };
const COPY_REVIEW = { label: "复制审查意见", prominence: "secondary" as const, partial: false };
// 不完整或未通过验证的审查只能复制记录，不能称为一份完成的 PR Review。
const COPY_RECORD = { label: "复制审查记录", prominence: "secondary" as const, partial: false };
// 失败/取消后复制已有结论时，必须明确自述为部分审查记录。
const COPY_PARTIAL = { label: "复制部分审查记录", prominence: "secondary" as const, partial: true };

const EVIDENCE_REASONS = new Set([
  "verification_unresolved", "finding_status_unknown", "investigation_incomplete",
  "unscheduled_investigation", "confirmed_findings_withheld", "finding_severity_unknown",
  "author_context_truncated", "author_context_unknown", "historical_discussion_unresolved",
]);
const RUNTIME_REASONS = new Set([
  "budget_exhausted", "deadline_exceeded", "usage_integrity_unknown", "termination_incomplete",
  "snapshot_missing", "runtime_report_missing", "runtime_report_invalid", "unsupported_legacy_projection",
  "head_validation_failed", "head_validation_unknown",
]);

export function reviewLimitationKind(outcome: ReviewOutcome, extraReasons: string[] = []) {
  const reasons = [...(outcome.reason_codes ?? []), ...extraReasons];
  if (reasons.some(reason => reason === "scope_incomplete" || reason === "scope_empty")
      || (outcome.total_goals > 0 && outcome.examined_goals < outcome.total_goals)) return "scope_incomplete";
  if (outcome.unresolved_count > 0 || outcome.incomplete_investigations > 0
      || reasons.some(reason => EVIDENCE_REASONS.has(reason))) return "evidence_gaps";
  if (reasons.some(reason => RUNTIME_REASONS.has(reason))
      || (outcome.runtime_reliability && outcome.runtime_reliability !== "reliable")) return "runtime_limits";
  return null;
}

function actions(...keys: SituationActionKey[]): SituationAction[] {
  return keys.map(key => ({ key, ...ACTION_META[key] }));
}

export type FindingEntryFilter = "all" | "blockers" | "suggestions" | "unresolved";
export interface SituationTarget {
  section: ReviewSection;
  findingFilter?: FindingEntryFilter;
  scopeFilter?: "incomplete";
  investigations?: boolean;
  activityFocus?: "limits" | "validation" | "error";
}
export function situationActionTarget(key: Exclude<SituationActionKey, "rereview">): SituationTarget {
  const targets: Record<Exclude<SituationActionKey, "rereview">, SituationTarget> = {
    view_blockers: { section: "findings", findingFilter: "blockers" },
    view_unresolved: { section: "findings", findingFilter: "unresolved" },
    view_findings: { section: "findings", findingFilter: "suggestions" },
    view_retained: { section: "findings", findingFilter: "all" },
    view_gaps: { section: "scope", scopeFilter: "incomplete" },
    view_investigations: { section: "scope", investigations: true },
    view_validation: { section: "activity", activityFocus: "validation" },
    view_error: { section: "activity", activityFocus: "error" },
    view_limits: { section: "activity", activityFocus: "limits" },
    view_activity: { section: "activity" },
    view_report: { section: "report" },
  };
  return targets[key];
}

function evidenceTitle(outcome: ReviewOutcome, reasons: string[]): string {
  // 历史复核是已取得独立代码证据的既有风险，不能降级为普通“疑点”。
  if (reasons.includes("historical_discussion_unresolved")) return "历史讨论中的问题经代码核验仍存在，需先核对";
  if (outcome.unresolved_count > 0) return `有 ${outcome.unresolved_count} 项疑点需要进一步确认`;
  if (outcome.incomplete_investigations > 0) return `有 ${outcome.incomplete_investigations} 项关联调查尚未完成`;
  if (reasons.some(reason => reason.startsWith("author_context_"))) return "作者声明范围尚待核对";
  if (reasons.includes("confirmed_findings_withheld")) return "部分代码证据已确认的记录未通过交付准入，需核对证据";
  return "存在未确认事项，需进一步核实";
}

export function reviewSituation(
  plan: SituationSource | null | undefined,
  options: { stale?: boolean } = {},
): ReviewSituation | null {
  if (!plan) return null;
  const outcome = plan.outcome;
  const decision = plan.digest?.merge_decision;
  const status = decision?.status;
  const historyBlocking = decision?.blocking_historical_keys?.length ?? 0;
  const historySuggestions = decision?.nonblocking_historical_keys?.length ?? 0;
  const blockingCount = decision?.blocking_finding_keys
    ? decision.blocking_finding_keys.length + historyBlocking : historyBlocking || null;
  const nonblockingCount = (decision?.nonblocking_finding_keys?.length ?? 0) + historySuggestions;
  const unresolvedCount = outcome.unresolved_count ?? 0;
  const examinedGoals = outcome.examined_goals ?? 0;
  const totalGoals = outcome.total_goals ?? 0;
  const counts = { blockingCount, nonblockingCount, unresolvedCount, examinedGoals, totalGoals };
  // 主结论以后端已保存的决策标签为准；只有旧冻结计划缺少标签时才回退到展示层措辞。
  const backendTitle = decision?.label?.trim() && decision.label.trim() !== "暂不能给出合并建议"
    ? decision.label.trim() : null;
  const titleOf = (fallback: string) => backendTitle || fallback;
  const meteringOnly = status === "conditional_merge" && outcome.review_completeness === "complete"
    && outcome.unresolved_count === 0 && outcome.incomplete_investigations === 0
    && outcome.total_goals > 0 && outcome.examined_goals === outcome.total_goals
    && outcome.reason_codes?.length === 1 && outcome.reason_codes[0] === "usage_integrity_unknown"
    && !decision?.unresolved_reasons?.length;

  // 保留历史记录入口，避免切换情景时卸载尚待确认的复制组件。
  if (options.stale) {
    return { ...counts, key: "stale", title: "PR 已更新，当前审查结果已过期",
      description: "本轮记录仅适用于原 Head，不能用于判断最新提交是否可以合并。",
      nextStep: "重新审查此 PR，基于最新提交生成新的审查结论。", tone: "warning", icon: "history",
      actions: actions("rereview"), copy: {
        label: outcome.kind === "failed" || outcome.kind === "cancelled" ? "复制历史部分审查记录" : "复制历史审查记录",
        prominence: "secondary", partial: outcome.kind === "failed" || outcome.kind === "cancelled",
      } };
  }
  // 失败与用户停止只保留已有记录：主操作是理解现状，复制是明确标注的部分记录。
  if (outcome.kind === "failed") {
    return { ...counts, key: "failed", title: titleOf("审查中断，尚无完整结论"),
      description: "现有记录不能作为完整合并建议。",
      nextStep: "查看错误并处理运行故障，然后重新审查此 PR。", tone: "danger", icon: "alert",
      actions: actions("view_error", "rereview"), copy: COPY_PARTIAL };
  }
  if (outcome.kind === "cancelled") {
    return { ...counts, key: "cancelled", title: titleOf("审查已停止，已保留部分结果"),
      description: "审查由用户主动停止，保留记录不是完整结论。",
      nextStep: "查看已保留结果，确认未完成范围后决定是否重新审查。", tone: "warning", icon: "x",
      actions: actions("view_retained", "rereview"), copy: COPY_PARTIAL };
  }
  // 与后端决策同序：已确认的 High/Critical 问题优先，验证失败作为并列事实而不是覆盖结论。
  if ((blockingCount ?? 0) > 0 || status === "fix_blockers") {
    const extra: SituationActionKey[] = unresolvedCount > 0 ? ["view_unresolved"] : [];
    if (outcome.dynamic_validation === "failed") extra.push("view_validation");
    return { ...counts, key: "blockers",
      title: titleOf(blockingCount ? `发现 ${blockingCount} 项高优先级问题，建议修复后复审` : "发现高优先级问题，建议修复后复审"),
      description: "这些问题已有代码证据支持，按 RepoGuardian 审查策略建议在合并前处理；该建议不是 GitHub Mergeability 状态或分支保护结论。"
        + (outcome.dynamic_validation === "failed" ? "当前 Head 另有动态检查未通过，需一并核对。" : ""),
      nextStep: "查看高优先级问题的证据并修复，然后基于新提交重新审查。",
      tone: "danger", icon: "alert",
      actions: actions(historyBlocking ? "view_report" : "view_blockers", ...extra), copy: COPY_REVIEW };
  }
  if (outcome.dynamic_validation === "failed") {
    return { ...counts, key: "validation_failed", title: titleOf("本次记录的动态检查未通过"),
      description: "失败记录只覆盖与本次 Head 绑定的受控检查，不能泛化为整个项目 CI 失败，也不能据此宣称验证通过。",
      nextStep: "查看失败记录，核对检查范围、来源与失败原因。", tone: "danger", icon: "flask",
      actions: actions("view_validation", "rereview"), copy: COPY_RECORD };
  }
  if (status === "resolve_unknowns" || status === "unavailable"
      || (outcome.kind === "inconclusive" && !meteringOnly)) {
    const limitation = reviewLimitationKind(outcome, decision?.unresolved_reasons);
    if (limitation === "scope_incomplete") {
      return { ...counts, key: "scope_incomplete",
        title: titleOf(totalGoals > examinedGoals ? `还有 ${totalGoals - examinedGoals} 项约定检查未完成`
          : totalGoals > 0 ? "检查范围完整性尚待核对" : "检查范围尚不明确"),
        description: totalGoals > 0
          ? `已检查 ${examinedGoals}/${totalGoals} 项；范围完整性尚不能确认。`
          : "没有可用的约定目标记录，无法判断检查范围。",
        nextStep: totalGoals > 0
          ? "查看未检查范围，核对未完成目标和原因，再决定补查或重新审查。"
          : "查看未检查范围或重新审查，补齐可核对的约定目标。",
        tone: "warning", icon: "clock", actions: actions("view_gaps", "rereview"), copy: COPY_RECORD };
    }
    if (limitation === "evidence_gaps") {
      const reasons = [...(outcome.reason_codes ?? []), ...(decision?.unresolved_reasons ?? [])];
      const historical = reasons.includes("historical_discussion_unresolved");
      const action = unresolvedCount > 0 ? "view_unresolved"
        : outcome.incomplete_investigations > 0 || reasons.some(reason => ["investigation_incomplete", "unscheduled_investigation"].includes(reason))
          ? "view_investigations" : "view_report";
      return { ...counts, key: "evidence_gaps", title: titleOf(evidenceTitle(outcome, reasons)),
        description: historical
          ? "历史复核已用当前固定代码确认部分既有问题仍然存在；它们不计入本轮 Diff 的新增问题，但会影响合并判断。"
          : "未确认事项不等于代码证据已确认的缺陷，但会限制合并判断。",
        nextStep: historical
          ? "查看完整报告，核对历史问题的证据、影响范围与修复状态。"
          : action === "view_unresolved" ? "查看未确认事项，补齐代码或目标依据后再判断。"
            : action === "view_investigations" ? "查看未完成调查，补齐关联证据后再判断。"
              : "查看完整报告，核对证据缺口后再判断。",
        tone: "warning", icon: "help",
        actions: actions(action, ...(unresolvedCount > 0 && outcome.incomplete_investigations > 0 ? ["view_investigations" as const] : []), "rereview"),
        copy: COPY_RECORD };
    }
    return { ...counts, key: "runtime_limits",
      title: titleOf(limitation ? "审查已结束，但结果可靠性仍需核对" : "审查结论暂不完整"),
      description: limitation
        ? `${totalGoals > 0 ? `约定静态目标已检查 ${examinedGoals}/${totalGoals} 项` : "约定静态目标的检查范围尚不明确"}；运行或计量异常本身不代表代码缺陷，未知用量不能当作完整统计。`
        : "限制原因或可靠性记录尚不明确，不能据此推断存在代码缺陷或审查已完整完成。",
      nextStep: "查看运行限制，核对执行、计量与验证记录。",
      tone: "warning", icon: "activity", actions: actions("view_limits", "rereview"), copy: COPY_RECORD };
  }
  if (nonblockingCount > 0) {
    return { ...counts, key: "nonblocking", title: titleOf(`发现 ${nonblockingCount} 项改进建议，暂无高优先级问题`),
      description: "这些建议已确认具备代码证据，但未达到高优先级；是否在合并前处理取决于项目要求和实际影响。",
      nextStep: "查看建议并评估影响，或复制 PR 评论与作者讨论。",
      tone: "info", icon: "info", actions: actions(historySuggestions ? "view_report" : "view_findings"), copy: COPY_PR };
  }
  return { ...counts, key: "clean", title: titleOf("本次检查未发现需在合并前处理的问题"),
    description: "本结论仅覆盖已检查范围，不代表 PR 安全或项目 CI 全部通过；合并仍需结合项目验证和人工审批。"
      + (outcome.reason_codes?.includes("usage_integrity_unknown") ? "运行计量记录不完整，未知用量不能当作完整统计。" : ""),
    nextStep: "预览并复制 PR 评论，结合项目验证和人工审批决定是否合并。",
    tone: "success", icon: "check-circle", actions: actions("view_report"), copy: COPY_PR };
}
