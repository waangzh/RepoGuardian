<script setup lang="ts">
import type { PublicationPlan } from "../../types/publication";
defineProps<{ plan: PublicationPlan }>();
const labels = { clean: "未发现可行动问题", findings: "发现已确认问题", inconclusive: "审查结论不确定", failed: "审查失败", cancelled: "审查已取消" };
const coverageLabels = { complete: "完整", partial: "部分完成", unknown: "未知" };
</script>
<template>
  <article class="summary-banner publication-outcome" aria-label="审查结论">
    <div class="top"><div><div class="eyebrow">REVIEW OUTCOME · 本轮快照结论</div><h2>{{ labels[plan.outcome.kind] }}</h2><p>目标检查 {{ plan.outcome.examined_goals }} / {{ plan.outcome.total_goals }} · {{ plan.outcome.confirmed_count }} 项已确认问题 · {{ plan.outcome.unresolved_count }} 项未决候选</p></div><span class="badge" :class="plan.outcome.coverage === 'complete' ? 'blue' : 'warn'">覆盖{{ coverageLabels[plan.outcome.coverage] }}</span></div>
    <div class="callout">本轮结论由后端生成；{{ plan.outcome.incomplete_investigations }} 项调查尚未完成。未产生问题草稿不代表代码安全或测试通过。</div>
    <p class="runtime-caption">计划本身不执行发布，不能据此判断“已发布”或“尚未发布到 GitHub”，请查看发布页回执。</p>
    <div>
      <details v-if="plan.withheld_findings.length || plan.outcome.reason_codes.length" class="runtime-technical"><summary>限制与扣留原因</summary><ul><li v-for="reason in plan.outcome.reason_codes" :key="reason">{{ reason }}</li><li v-for="item in plan.withheld_findings" :key="`${item.candidate_id}:${item.reason_code}`">{{ item.candidate_id }} · {{ item.reason_code }}</li></ul></details>
    </div>
  </article>
</template>
