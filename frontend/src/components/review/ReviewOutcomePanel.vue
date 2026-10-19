<script setup lang="ts">
import { computed } from "vue";
import type { PublicationPlan, ReviewSummary } from "../../types/publication";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{ plan: PublicationPlan | ReviewSummary }>();
const coverageLabels = { complete: "完整", partial: "部分完成", unknown: "未知" };
// 与后端 merge_decision 三态一一对应；当前结论使用服务端返回的 label。
const mergeOptions = [
  { status: "conditional_merge", label: "未发现阻断性问题", icon: "check-circle", tone: "success",
    description: "本次静态审查没有发现需要阻止合并的问题；请结合 CI、审批和人工判断决定是否合并。" },
  { status: "fix_blockers", label: "建议修改后再合并", icon: "alert", tone: "danger",
    description: "存在已核验且影响关键行为的问题，需要先处理。" },
  { status: "resolve_unknowns", label: "需要补充检查", icon: "help", tone: "warning",
    description: "当前审查存在足以影响决策的证据缺口。" },
];
const decision = computed(() => props.plan.digest.merge_decision);
const currentStatus = computed(() => decision.value?.status === "conditional_merge" ? "conditional_merge"
  : decision.value?.status === "fix_blockers" ? "fix_blockers" : "resolve_unknowns");
const options = computed(() => mergeOptions.map(option => ({
  ...option,
  active: option.status === currentStatus.value,
  label: option.status === currentStatus.value ? (decision.value?.label || option.label) : option.label,
})));
</script>
<template>
  <article class="summary-banner publication-outcome" aria-label="审查结论">
    <div class="top"><div><div class="eyebrow">REVIEW OUTCOME · 本轮快照结论</div><h2>{{ decision?.label || '需要补充检查' }}</h2><p>目标检查 {{ plan.outcome.examined_goals }} / {{ plan.outcome.total_goals }} · {{ plan.outcome.confirmed_count }} 项已确认问题 · {{ plan.outcome.unresolved_count }} 项未决候选</p></div><span class="badge" :class="plan.outcome.review_completeness === 'complete' ? 'blue' : 'warn'">约定检查范围：{{ coverageLabels[plan.outcome.review_completeness] || '未知' }}</span></div>
    <ul class="merge-options" aria-label="合并建议">
      <li v-for="option in options" :key="option.status" class="merge-option" :class="[option.tone, { active: option.active }]">
        <AppIcon :name="option.icon" :size="20" />
        <div><strong>{{ option.label }}<span v-if="option.active" class="merge-current">当前结论</span></strong><p>{{ option.description }}</p></div>
      </li>
    </ul>
    <p>{{ decision?.advice }}</p>
    <p class="small muted">代码问题结论：{{ ({ confirmed: '有已确认问题', unresolved: '仍有未决候选', no_confirmed_findings: '未发现已确认缺陷', unknown: '未知' })[plan.outcome.finding_conclusion] || '未知' }} · 运行可信状态：{{ ({ reliable: '可靠', unreliable: '存在计量或执行限制', unknown: '未知' })[plan.outcome.runtime_reliability] || '未知' }} · 动态验证：{{ ({ not_executed: '未执行', passed: '已有当前 Head 通过结果', failed: '已有当前 Head 失败结果', independent_results: '已有独立补丁结果，Head 未知', unknown: '未知' })[plan.outcome.dynamic_validation] || '未知' }}</p>
    <ul v-if="decision?.limits?.length" class="rg-limitations"><li v-for="item in decision.limits" :key="item">{{ item }}</li></ul>
    <ul v-if="plan.digest.validations?.length"><li v-for="item in plan.digest.validations" :key="item.id">{{ item.source }} / {{ item.stage }}：{{ item.status }}。{{ item.reason }}</li></ul>
    <div class="callout">本轮结论由后端生成；{{ plan.outcome.incomplete_investigations }} 项调查尚未完成。未产生问题草稿不代表代码安全或测试通过。</div>
    <p v-if="'schema_version' in plan && plan.schema_version === 'review-summary-v1'" class="runtime-caption">这是结果概览；逐条问题正文与发布状态以“问题与证据”“审查报告”页为准。</p>
    <div>
      <details v-if="plan.withheld_findings.length || plan.outcome.reason_codes.length" class="runtime-technical"><summary>限制与扣留原因</summary><ul><li v-for="reason in plan.outcome.reason_codes" :key="reason">{{ reason }}</li><li v-for="item in plan.withheld_findings" :key="`${item.candidate_id}:${item.reason_code}`">{{ item.candidate_id }} · {{ item.reason_code }}</li></ul></details>
    </div>
  </article>
</template>
