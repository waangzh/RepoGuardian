import type { MergeDecision, ReviewOutcome } from "../types/publication";

/** 展示层情景：由后端内部状态组合成用户可理解的单一结论，不新增后端顶层状态。 */
export type SituationKey =
  | "stale" | "failed" | "cancelled" | "validation_failed" | "blockers"
  | "evidence_gaps" | "scope_incomplete" | "nonblocking" | "clean";

export type SituationTone = "success" | "info" | "warning" | "danger";

export type SituationActionKey =
  | "view_blockers" | "view_unresolved" | "view_findings" | "view_gaps"
  | "view_validation" | "view_error" | "view_activity" | "view_retained"
  | "view_report" | "rereview";

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
  title: string;
  description: string;
  tone: SituationTone;
  icon: string;
  blockingCount: number;
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
  view_blockers: { label: "查看阻断问题", icon: "alert" },
  view_unresolved: { label: "查看待确认风险", icon: "search" },
  view_findings: { label: "查看问题", icon: "search" },
  view_gaps: { label: "查看缺口", icon: "shield" },
  view_validation: { label: "查看验证结果", icon: "flask" },
  view_error: { label: "查看错误", icon: "alert" },
  view_activity: { label: "查看技术记录", icon: "activity" },
  view_retained: { label: "查看已保留结果", icon: "search" },
  view_report: { label: "查看完整报告", icon: "report" },
  rereview: { label: "重新审查", icon: "plus" },
};

const COPY_PR = { label: "复制 PR 评论", prominence: "primary" as const, partial: false };
const COPY_REVIEW = { label: "复制审查意见", prominence: "secondary" as const, partial: false };
// 不完整或未通过验证的审查只能复制记录，不能称为一份完成的 PR Review。
const COPY_RECORD = { label: "复制审查记录", prominence: "secondary" as const, partial: false };
// 失败/取消后复制已有结论时，必须明确自述为部分审查记录。
const COPY_PARTIAL = { label: "复制部分审查记录", prominence: "secondary" as const, partial: true };

function actions(...keys: SituationActionKey[]): SituationAction[] {
  return keys.map(key => ({ key, ...ACTION_META[key] }));
}

export function reviewSituation(
  plan: SituationSource | null | undefined,
  options: { stale?: boolean } = {},
): ReviewSituation | null {
  if (!plan) return null;
  const outcome = plan.outcome;
  const decision = plan.digest?.merge_decision;
  const status = decision?.status;
  const reasons = outcome.reason_codes ?? [];
  const blockingCount = decision?.blocking_finding_keys?.length || (status === "fix_blockers" ? 1 : 0);
  const nonblockingCount = decision?.nonblocking_finding_keys?.length ?? 0;
  const unresolvedCount = outcome.unresolved_count ?? 0;
  const examinedGoals = outcome.examined_goals ?? 0;
  const totalGoals = outcome.total_goals ?? 0;
  const counts = { blockingCount, nonblockingCount, unresolvedCount, examinedGoals, totalGoals };

  // PR 已更新时原结论不再适用，优先于其他状态；不提供复制，避免继续交付旧结论。
  if (options.stale) {
    return { ...counts, key: "stale", title: "审查结果基于旧代码版本",
      description: "当前 PR 已更新，原结论可能不再适用。", tone: "warning", icon: "history",
      actions: actions("rereview"), copy: null };
  }
  // 失败与取消只保留已有记录：主操作是理解现状，复制是明确标注的部分记录。
  if (outcome.kind === "failed") {
    return { ...counts, key: "failed", title: "审查未完成",
      description: "执行过程中发生错误，无法生成本轮可信的完整审查结论。", tone: "danger", icon: "alert",
      actions: actions("view_error", "rereview"), copy: COPY_PARTIAL };
  }
  if (outcome.kind === "cancelled") {
    return { ...counts, key: "cancelled", title: "审查已停止",
      description: "已保留停止前的检查记录，剩余范围未完成。", tone: "warning", icon: "x",
      actions: actions(confirmedOrUnresolved(outcome) ? "view_retained" : "view_activity", "rereview"), copy: COPY_PARTIAL };
  }
  if (outcome.dynamic_validation === "failed") {
    return { ...counts, key: "validation_failed", title: "已有验证未通过",
      description: "与当前代码版本绑定的验证记录失败，需要查看失败原因。", tone: "danger", icon: "flask",
      actions: actions("view_validation", "rereview"), copy: COPY_RECORD };
  }
  if (blockingCount > 0 || status === "fix_blockers") {
    const extra: SituationActionKey[] = unresolvedCount > 0 ? ["view_unresolved"] : [];
    return { ...counts, key: "blockers", title: "发现需要优先修复的问题",
      description: `已确认 ${blockingCount || 1} 项高严重度问题，建议修复并重新审查后再考虑合并。`,
      tone: "danger", icon: "alert", actions: actions("view_blockers", ...extra), copy: COPY_REVIEW };
  }
  if (status === "resolve_unknowns" || status === "unavailable" || outcome.kind === "inconclusive") {
    const scopeIncomplete = reasons.includes("scope_incomplete")
      || (totalGoals > 0 && examinedGoals < totalGoals);
    if (scopeIncomplete) {
      return { ...counts, key: "scope_incomplete", title: "部分审查已完成",
        description: totalGoals > 0
          ? `已完成 ${examinedGoals}/${totalGoals} 个约定目标，剩余部分尚未检查。`
          : "没有可用的约定目标记录，无法判断检查范围。",
        tone: "warning", icon: "clock", actions: actions("view_gaps", "rereview"), copy: COPY_RECORD };
    }
    return { ...counts, key: "evidence_gaps", title: "审查结论尚不充分",
      description: "部分问题或调用路径尚未核验，无法形成可靠的完整判断。", tone: "warning", icon: "help",
      actions: actions("view_gaps", "rereview"), copy: COPY_RECORD };
  }
  if (nonblockingCount > 0) {
    return { ...counts, key: "nonblocking", title: "发现非阻断性问题",
      description: `已确认 ${nonblockingCount} 项改进建议，是否需要合并前处理取决于项目要求和实际影响。`,
      tone: "info", icon: "info", actions: actions("view_findings"), copy: COPY_PR };
  }
  return { ...counts, key: "clean", title: "本次静态审查未发现需处理的问题",
    description: "在已检查范围内未发现达到报告门槛的缺陷；合并仍需结合 CI 与审批。",
    tone: "success", icon: "check-circle", actions: actions("view_report"), copy: COPY_PR };
}

function confirmedOrUnresolved(outcome: ReviewOutcome): boolean {
  return (outcome.confirmed_count ?? 0) > 0 || (outcome.unresolved_count ?? 0) > 0;
}
