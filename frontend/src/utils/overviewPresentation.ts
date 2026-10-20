import type { ReviewSummary } from "../types/publication";
import type { ReviewSituation, SituationKey } from "./reviewSituation";
import { reviewLimitationKind } from "./reviewSituation";
import type { TaskRuntimeSummary } from "./taskRuntimePresentation";
import type { ReviewSection } from "../types/workspace";
import { reportValidationLabel } from "./reportPresentation";

export interface OverviewBoundary {
  title: string;
  value: string;
  description: string;
  tone: "success" | "warning" | "danger" | "neutral";
  icon: string;
  target: ReviewSection;
}

export function overviewFacts(plan: ReviewSummary | null, runtime: TaskRuntimeSummary | null) {
  const outcome = plan?.outcome;
  const decision = plan?.digest.merge_decision;
  const examined = outcome?.examined_goals ?? runtime?.scope.examined ?? null;
  const total = outcome?.total_goals ?? runtime?.scope.total ?? null;
  const validation = outcome?.dynamic_validation ?? plan?.digest.validation_state ?? "unknown";
  const reasonLabels: Record<string, string> = {
    scope_incomplete: "仍有约定目标未完整检查。",
    investigation_incomplete: "关联调查尚未完成，需要补充证据。",
    usage_integrity_unknown: "模型用量记录不完整或不一致，不能视为完整统计。",
    termination_incomplete: "运行未可靠完成全部计划，请核对执行记录。",
  };
  const limitations = [...new Set([
    ...(decision?.limits || []),
    ...(decision?.unresolved_reasons || []).map(reason => /^[a-z][a-z0-9_]*$/.test(reason)
      ? reasonLabels[reason] || "存在未决审查原因，请结合详细记录核对。"
      : reason),
  ])];
  const investigationCount = outcome?.incomplete_investigations ?? null;
  const limitation = outcome ? reviewLimitationKind(outcome, decision?.unresolved_reasons) : null;
  const evidenceGap = limitation === "evidence_gaps" || Boolean(investigationCount)
    || Boolean(outcome?.unresolved_count);
  // 完成计数不是结论：范围完整性未知时不能由 n/n 推导为已完整检查。
  const scopeKnown = outcome?.review_completeness === "complete";
  const scopeIncomplete = outcome?.reason_codes?.includes("scope_incomplete")
    || outcome?.reason_codes?.includes("scope_empty")
    || (examined !== null && total !== null && examined < total);
  const boundaries: OverviewBoundary[] = [
    {
      title: "约定静态目标",
      value: examined === null || total === null ? "覆盖情况未知" : `${examined}/${total} 项已检查`,
      description: scopeIncomplete ? "仍有约定目标未完整检查，未完成范围不能计为已审查。"
        : "静态目标的完成记录，不代表所有业务路径都得到验证。",
      tone: scopeIncomplete ? "warning" : scopeKnown ? "success" : "neutral",
      icon: scopeKnown ? "check-circle" : "units",
      target: "scope",
    },
    {
      title: "关联证据与限制",
      value: !plan ? "证据状态未知" : investigationCount ? `${investigationCount} 项调查未完成`
        : evidenceGap ? "证据尚不充分" : limitation === "runtime_limits" ? "运行或计量记录受限" : limitations.length ? "存在已知限制"
        : outcome?.runtime_reliability === "unknown" ? "证据边界待核对" : "查看证据边界",
      description: limitations[0] || (!plan ? "摘要尚未取得，请先查看技术记录或重试加载。"
        : "关联线索与问题候选分别记录；无待确认候选不等于无未核验路径。"),
      tone: plan && (evidenceGap || limitation === "runtime_limits") ? "warning" : "neutral",
      icon: "help",
      target: "scope",
    },
    {
      title: "动态测试与构建",
      value: reportValidationLabel(validation),
      description: validation === "not_executed" ? "未运行目标仓库代码，无法据此推断测试或构建通过。"
        : validation === "passed" ? "已有当前 Head 绑定的受控验证结果，仅适用于记录的检查，不代表项目 CI 全部通过。"
        : validation === "failed" ? "已有当前 Head 绑定的验证失败记录，请核对失败原因。"
        : validation === "independent_results" ? "独立补丁验证结果不代表当前 PR Head 已通过。"
        : "动态验证状态未知，不能据此宣称测试或构建通过。",
      tone: validation === "failed" ? "danger" : validation === "passed" ? "success" : "neutral",
      icon: "flask",
      target: "activity",
    },
  ];
  return {
    examined, total, validation,
    // 情景矩阵可使用保守阻断标记；指标只展示后端提供的实际数量。
    blockers: decision?.blocking_finding_keys?.length ?? null,
    unresolved: outcome?.unresolved_count ?? null,
    limitations, boundaries,
  };
}

interface NextSteps {
  title: string;
  description: string;
  steps: [string, string][];
}

const NEXT_STEPS: Record<SituationKey, NextSteps> = {
  evidence_gaps: {
    title: "建议先核对证据缺口",
    description: "先了解哪些判断缺少证据，再决定是否补查或交付本轮记录。",
    steps: [["查看未核验线索", "核对缺口与当前 PR 风险的关系"], ["补充运行级证据", "结合项目 CI 或受控外部验证"], ["复核后再形成决定", "结合业务约定与人工审批"]],
  },
  scope_incomplete: {
    title: "补齐未完成的检查范围",
    description: "先确认哪些约定目标未检查及中断原因，再决定是否重新审查。",
    steps: [["定位未完成目标", "查看覆盖缺口、原因和相关文件"], ["核对运行限制", "确认预算、时限或执行错误"], ["决定是否重新审查", "重新分析当前 PR 并确认方案"]],
  },
  runtime_limits: {
    title: "先核对运行与计量记录",
    description: "代码检查情况与运行记录完整性分别判断，运行限制不等于代码缺陷。",
    steps: [["查看运行记录", "核对终止、计量或验证限制"], ["确认实际检查范围", "目标完成数不代表用量记录完整"], ["决定是否需要重新审查", "不要将未知用量当作完整统计"]],
  },
  blockers: {
    title: "先复核高优先级问题",
    description: "从明确的代码定位、触发条件与证据开始，核对影响后再修复。",
    steps: [["查看问题与证据", "确认触发条件、定位和影响范围"], ["修复并完成项目验证", "使用项目 CI 或受控外部验证"], ["基于新提交重新审查", "旧快照结论不自动适用于新 Head"]],
  },
  clean: {
    title: "可以准备审查交付",
    description: "已检查范围内未发现达到报告门槛的问题，交付前仍需核对快照与 CI。",
    steps: [["核对版本与 CI", "确认 PR 当前 Head 和项目验证结果"], ["阅读交付摘要", "核对结论措辞与覆盖边界"], ["由人工决定是否合并", "静态结论不代替项目审批"]],
  },
  nonblocking: {
    title: "复核本轮改进建议",
    description: "结合实际影响判断处理优先级，再准备审查交付。",
    steps: [["核对发现与依据", "区分已确认问题和待确认候选"], ["结合项目要求处理", "评估影响并完成必要验证"], ["核对交付内容", "确认快照、摘要与已知限制"]],
  },
  stale: {
    title: "先为新代码创建审查",
    description: "PR 已更新，本轮结论仅适用于旧快照。",
    steps: [["重新获取 PR 快照", "拉取最新 Head 和 Diff 基准"], ["生成并确认新方案", "重新核对文件范围、重点与预算"], ["对照新旧审查结果", "旧证据不会自动用于新的代码版本"]],
  },
  failed: {
    title: "先了解执行失败原因",
    description: "本轮未形成完整结论，已有记录仍可追溯。",
    steps: [["查看技术记录", "定位错误与终止原因"], ["核对已保留结果", "确认实际检查范围和证据限制"], ["决定是否重新审查", "基于当前 PR 重新确认方案"]],
  },
  cancelled: {
    title: "核对停止前保留的记录",
    description: "剩余范围尚未完成，交付内容只代表部分审查记录。",
    steps: [["查看已保留结果", "核对已检查目标和问题记录"], ["了解未检查范围", "不将未检查部分当成完成"], ["决定是否重新审查", "重新分析当前 PR 并确认方案"]],
  },
  validation_failed: {
    title: "先核对验证失败原因",
    description: "已有与当前快照绑定的验证失败记录，需结合具体检查判断。",
    steps: [["查看动态验证记录", "核对验证来源、快照和失败原因"], ["处理失败并补充验证", "在受控执行端确认修复效果"], ["重新核对审查结论", "运行状态与静态发现分别判断"]],
  },
};

export function overviewNextSteps(situation: ReviewSituation | null): NextSteps {
  return situation ? NEXT_STEPS[situation.key] : {
    title: "先查看本轮审查记录",
    description: "后端摘要尚不可用，当前不能推断问题数量或完整结论。",
    steps: [["重试加载摘要", "或前往技术记录核对执行状态"], ["保留未知与未决", "摘要缺失不代表零问题"]],
  };
}
