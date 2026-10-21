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
    historical_discussion_unresolved: "历史讨论复核确认的既有问题在当前代码中仍存在。",
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
      description: scopeIncomplete ? "仍有约定目标未完整检查。"
        : scopeKnown ? "" : "范围完整性尚未确认。",
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
        : evidenceGap ? "未决调查或问题候选仍需补充证据。"
        : limitation === "runtime_limits" ? "请核对执行与计量记录。"
        : outcome?.runtime_reliability === "unknown" ? "运行可靠性尚未确认。" : ""),
      tone: plan && (evidenceGap || limitation === "runtime_limits") ? "warning" : "neutral",
      icon: "help",
      target: "scope",
    },
    {
      title: "动态测试与构建",
      value: reportValidationLabel(validation),
      description: validation === "not_executed" ? ""
        : validation === "passed" ? "仅限当前 Head 绑定的受控检查。"
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
    // 情景矩阵可使用保守阻断标记；指标展示按缺陷身份去重后的独立数量，
    // 旧冻结计划缺少该字段时才回退为来源相加。
    blockers: decision?.blocking_finding_keys
      ? decision.blocking_independent_count
        ?? decision.blocking_finding_keys.length + (decision.blocking_historical_keys?.length ?? 0) : null,
    unresolved: outcome?.unresolved_count ?? null,
    limitations, boundaries,
  };
}

export function overviewConclusionDescription(situation: ReviewSituation | null): string {
  if (!situation) return "";
  // 事实边界优先使用情景给出的 description，避免各页面各写一套措辞。
  return situation.description || SITUATION_DESCRIPTIONS[situation.key];
}

const SITUATION_DESCRIPTIONS: Record<SituationKey, string> = {
  stale: "记录仅适用于原 Head，最新提交需要重新审查。",
  failed: "执行错误导致中断，现有结果仅代表部分审查。",
  cancelled: "用户主动停止审查，剩余范围尚未完成。",
  validation_failed: "当前 Head 绑定的受控检查存在失败记录，不代表全部 CI 失败。",
  blockers: "问题已有代码证据支持，按审查策略建议在合并前处理；该建议不是 GitHub Mergeability 结论。",
  evidence_gaps: "历史复核确认的既有风险与本轮未确认事项分别记录，均会限制合并判断。",
  scope_incomplete: "检查范围完整性尚待核对，未完成目标需单独确认。",
  runtime_limits: "执行、计量或可靠性信息尚待核对，不据此判断代码存在缺陷。",
  nonblocking: "是否在合并前处理，取决于项目要求和实际影响。",
  clean: "这是已检查范围内的静态结论，不等同于项目 CI 通过。",
};

interface NextSteps {
  title: string;
  description: string;
  steps: [string, string][];
}

const NEXT_STEPS: Record<SituationKey, NextSteps> = {
  evidence_gaps: {
    title: "建议先核对证据缺口",
    description: "定位历史已核验风险或未确认事项，再决定补查或交付记录。",
    steps: [["查看未确认事项", "核对缺口与当前 PR 风险的关系"], ["补充运行级证据", "结合项目 CI 或受控外部验证"], ["复核后再形成决定", "结合业务约定与人工审批"]],
  },
  scope_incomplete: {
    title: "补齐未完成的检查范围",
    description: "定位未检查目标，确认是否需要补查。",
    steps: [["定位未完成目标", "查看覆盖缺口、原因和相关文件"], ["核对运行限制", "确认预算、时限或执行错误"], ["决定是否重新审查", "重新分析当前 PR 并确认方案"]],
  },
  runtime_limits: {
    title: "先核对运行与计量记录",
    description: "核对终止原因与用量，判断是否需要重审。",
    steps: [["查看运行记录", "核对终止、计量或验证限制"], ["确认实际检查范围", "目标完成数不代表用量记录完整"], ["决定是否需要重新审查", "不要将未知用量当作完整统计"]],
  },
  blockers: {
    title: "先复核高优先级问题",
    description: "从代码定位和触发条件开始核对。",
    steps: [["查看问题与证据", "确认触发条件、定位和影响范围"], ["修复并完成项目验证", "使用项目 CI 或受控外部验证"], ["基于新提交重新审查", "旧快照结论不自动适用于新 Head"]],
  },
  clean: {
    title: "可以准备审查交付",
    description: "阅读报告，或预览并复制 PR 评论。",
    steps: [["核对版本与 CI", "确认 PR 当前 Head 和项目验证结果"], ["阅读交付摘要", "核对结论措辞与覆盖边界"], ["由人工决定是否合并", "静态结论不代替项目审批"]],
  },
  nonblocking: {
    title: "复核本轮改进建议",
    description: "核对建议的实际影响与处理优先级。",
    steps: [["核对发现与依据", "区分代码证据已确认的问题和待确认候选"], ["结合项目要求处理", "评估影响并完成必要验证"], ["核对交付内容", "确认快照、摘要与已知限制"]],
  },
  stale: {
    title: "先为新代码创建审查",
    description: "获取最新 Head 后重新确认审查方案。",
    steps: [["重新获取 PR 快照", "拉取最新 Head 和 Diff 基准"], ["生成并确认新方案", "重新核对文件范围、重点与预算"], ["对照新旧审查结果", "旧证据不会自动用于新的代码版本"]],
  },
  failed: {
    title: "先了解执行失败原因",
    description: "定位执行错误，再决定是否重新审查。",
    steps: [["查看技术记录", "定位错误与终止原因"], ["核对已保留结果", "确认实际检查范围和证据限制"], ["决定是否重新审查", "基于当前 PR 重新确认方案"]],
  },
  cancelled: {
    title: "核对停止前保留的记录",
    description: "核对已保留结果与未检查范围。",
    steps: [["查看已保留结果", "核对已检查目标和问题记录"], ["了解未检查范围", "不将未检查部分当成完成"], ["决定是否重新审查", "重新分析当前 PR 并确认方案"]],
  },
  validation_failed: {
    title: "先核对验证失败原因",
    description: "核对验证来源、检查范围与失败原因。",
    steps: [["查看动态验证记录", "核对验证来源、快照和失败原因"], ["处理失败并补充验证", "在受控执行端确认修复效果"], ["重新核对审查结论", "运行状态与静态发现分别判断"]],
  },
};

export function overviewNextSteps(situation: ReviewSituation | null): NextSteps {
  return situation ? NEXT_STEPS[situation.key] : {
    title: "先查看本轮审查记录",
    description: "重试读取摘要，或先核对技术记录。",
    steps: [["重试加载摘要", "或前往技术记录核对执行状态"], ["保留未知与未决", "摘要缺失不代表零问题"]],
  };
}
