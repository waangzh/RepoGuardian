<script setup lang="ts">
import type { PublicationPlan, ReviewSummary } from "../../types/publication";
defineProps<{ plan: PublicationPlan | ReviewSummary }>();
const coverageLabels = { complete: "完整", partial: "部分完成", unknown: "未知" };
</script>
<template>
  <article class="summary-banner publication-outcome" aria-label="审查结论">
    <div class="top"><div><div class="eyebrow">REVIEW OUTCOME · 本轮快照结论</div><h2>{{ plan.digest.merge_decision?.label || '需要补充检查' }}</h2><p>目标检查 {{ plan.outcome.examined_goals }} / {{ plan.outcome.total_goals }} · {{ plan.outcome.confirmed_count }} 项已确认问题 · {{ plan.outcome.unresolved_count }} 项未决候选</p></div><span class="badge" :class="plan.outcome.review_completeness === 'complete' ? 'blue' : 'warn'">约定检查范围：{{ coverageLabels[plan.outcome.review_completeness] || '未知' }}</span></div>
    <p>{{ plan.digest.merge_decision?.advice }}</p>
    <p class="small muted">代码问题结论：{{ ({ confirmed: '有已确认问题', unresolved: '仍有未决候选', no_confirmed_findings: '未发现已确认缺陷', unknown: '未知' })[plan.outcome.finding_conclusion] || '未知' }} · 运行可信状态：{{ ({ reliable: '可靠', unreliable: '存在计量或执行限制', unknown: '未知' })[plan.outcome.runtime_reliability] || '未知' }} · 动态验证：{{ ({ not_executed: '未执行', passed: '已有当前 Head 通过结果', failed: '已有当前 Head 失败结果', independent_results: '已有独立补丁结果，Head 未知', unknown: '未知' })[plan.outcome.dynamic_validation] || '未知' }}</p>
    <ul v-if="plan.digest.merge_decision?.limits?.length" class="rg-limitations"><li v-for="item in plan.digest.merge_decision.limits" :key="item">{{ item }}</li></ul>
    <ul v-if="plan.digest.validations?.length"><li v-for="item in plan.digest.validations" :key="item.id">{{ item.source }} / {{ item.stage }}：{{ item.status }}。{{ item.reason }}</li></ul>
    <div class="callout">本轮结论由后端生成；{{ plan.outcome.incomplete_investigations }} 项调查尚未完成。未产生问题草稿不代表代码安全或测试通过。</div>
    <p v-if="'schema_version' in plan && plan.schema_version === 'review-summary-v1'" class="runtime-caption">这是结果概览；逐条问题正文与发布状态以“问题与证据”“审查报告”页为准。</p>
    <div>
      <details v-if="plan.withheld_findings.length || plan.outcome.reason_codes.length" class="runtime-technical"><summary>限制与扣留原因</summary><ul><li v-for="reason in plan.outcome.reason_codes" :key="reason">{{ reason }}</li><li v-for="item in plan.withheld_findings" :key="`${item.candidate_id}:${item.reason_code}`">{{ item.candidate_id }} · {{ item.reason_code }}</li></ul></details>
    </div>
  </article>
</template>
