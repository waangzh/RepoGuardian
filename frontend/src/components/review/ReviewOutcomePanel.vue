<script setup lang="ts">
import type { PublicationPlan } from "../../types/publication";
defineProps<{ plan: PublicationPlan }>();
const labels = { clean: "未发现可行动问题", findings: "发现已确认问题", inconclusive: "审查结论不确定", failed: "审查失败", cancelled: "审查已取消" };
const coverageLabels = { complete: "完整", partial: "部分完成", unknown: "未知" };
</script>
<template>
  <article class="overview-card publication-outcome" aria-label="审查结论">
    <header><div><h2>{{ labels[plan.outcome.kind] }}</h2><p>覆盖：{{ coverageLabels[plan.outcome.coverage] }} · 目标检查 {{ plan.outcome.examined_goals }} / {{ plan.outcome.total_goals }}</p></div><code>{{ plan.outcome.kind }}</code></header>
    <div class="runtime-panel__body">
      <div class="runtime-stats">
        <div><span>已确认问题</span><strong>{{ plan.outcome.confirmed_count }}</strong></div>
        <div><span>可生成草稿</span><strong>{{ plan.outcome.publishable_count }}</strong></div>
        <div><span>未决候选</span><strong>{{ plan.outcome.unresolved_count }}</strong><small>{{ plan.outcome.incomplete_investigations }} 项未完成调查</small></div>
      </div>
      <p class="runtime-caption">内容由后端按审查快照生成，尚未发布到 GitHub。未产生问题草稿不代表审查通过。</p>
      <details v-if="plan.withheld_findings.length || plan.outcome.reason_codes.length" class="runtime-technical"><summary>限制与扣留原因</summary><ul><li v-for="reason in plan.outcome.reason_codes" :key="reason">{{ reason }}</li><li v-for="item in plan.withheld_findings" :key="`${item.candidate_id}:${item.reason_code}`">{{ item.candidate_id }} · {{ item.reason_code }}</li></ul></details>
    </div>
  </article>
</template>
