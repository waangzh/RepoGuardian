<script setup lang="ts">
import { computed } from "vue";
import type { PublicationPlan, ReviewSummary } from "../../types/publication";
import type { ReviewSituation } from "../../utils/reviewSituation";
import { reviewSituation } from "../../utils/reviewSituation";
import AppIcon from "../common/AppIcon.vue";
import { reportValidationLabel } from "../../utils/reportPresentation";

const props = defineProps<{ plan: PublicationPlan | ReviewSummary; stale?: boolean }>();
const coverageLabels = { complete: "完整", partial: "不完整", unknown: "未知" };
const decision = computed(() => props.plan.digest.merge_decision);
const situation = computed(() => reviewSituation(props.plan, { stale: props.stale }));
</script>
<template>
  <article class="summary-banner publication-outcome" aria-label="审查结论">
    <div v-if="situation" class="outcome-verdict" :class="situation.tone">
      <AppIcon :name="situation.icon" :size="22" />
      <div class="outcome-verdict__text">
        <h2>{{ situation.title }}</h2>
        <p>{{ situation.description }}</p>
        <p v-if="situation.nextStep" class="outcome-verdict__next"><strong>下一步：</strong>{{ situation.nextStep }}</p>
      </div>
    </div>
    <details class="runtime-technical outcome-details">
      <summary>技术结论与限制</summary>
      <p class="small muted">审查完整性：{{ coverageLabels[plan.outcome.review_completeness] || '未知' }} · 代码问题结论：{{ ({ confirmed: '有已确认问题', unresolved: '仍有未决候选', no_confirmed_findings: '未发现已确认缺陷', unknown: '未知' })[plan.outcome.finding_conclusion] || '未知' }} · 运行可信状态：{{ ({ reliable: '可靠', unreliable: '存在计量或执行限制', unknown: '未知' })[plan.outcome.runtime_reliability] || '未知' }} · 动态验证：{{ reportValidationLabel(plan.outcome.dynamic_validation) }}。验证通过仅限已记录的检查，不代表项目 CI 全部通过。</p>
      <ul v-if="decision?.limits?.length" class="rg-limitations"><li v-for="item in decision.limits" :key="item">{{ item }}</li></ul>
      <ul v-if="plan.digest.validations?.length"><li v-for="item in plan.digest.validations" :key="item.id">{{ item.source }} / {{ item.stage }}：{{ item.status }}。{{ item.reason }}</li></ul>
      <ul><li v-for="reason in plan.outcome.reason_codes" :key="reason">{{ reason }}</li><li v-for="item in plan.withheld_findings" :key="`${item.candidate_id}:${item.reason_code}`">{{ item.candidate_id }} · {{ item.reason_code }}</li></ul>
    </details>
  </article>
</template>
