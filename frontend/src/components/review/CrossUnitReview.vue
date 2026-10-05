<script setup lang="ts">
import { computed } from "vue";
import type { CrossUnitFollowupRequest, ReviewIssue, ReviewTask } from "../../types/review";
import { budgetPercent, coordinationDecisionLabel, crossUnitPresentation, evidenceCatalog, followupPresentation, formatCost, issueStatusBreakdown, publishedFollowupIssues, reasonLabels } from "../../utils/reviewObservability";
import StatusBadge from "../common/StatusBadge.vue";
import EmptyState from "../common/EmptyState.vue";
import EvidenceReferences from "./EvidenceReferences.vue";
import UnitReviewRecord from "./UnitReviewRecord.vue";

const props = defineProps<{ task: ReviewTask }>();
const emit = defineEmits<{ openUnit: [id: string]; openIssue: [issue: ReviewIssue] }>();
const presentation = computed(() => crossUnitPresentation(props.task));
const catalog = computed(() => evidenceCatalog(props.task));
const risk = computed(() => props.task.cross_unit_risk);
const plan = computed(() => props.task.coordination_plan);
const metrics = computed(() => plan.value?.runtime_fingerprint ? plan.value.runtime_metrics : null);
const budget = computed(() => plan.value?.execution_budget);
const results = computed(() => props.task.followup_results || []);
const relationships = { calls: "调用", imports: "导入", test_of: "测试覆盖", configures: "配置影响", declared_dependency: "声明依赖" };
const indexLabels = { available: "索引可用", partial: "索引不完整", unknown: "索引状态未知" };
const batchLabels = { pending: "待规划", validated: "规划已校验", skipped: "输入超限，未覆盖", failed: "规划失败，未覆盖" };
function unitName(id: string): string {
  const unit = props.task.review_units.find((item) => item.id === id);
  return unit?.primary_files[0] || id;
}
function resultFor(request: CrossUnitFollowupRequest) { return results.value.find((item) => item.request_id === request.id); }
function findings(request: CrossUnitFollowupRequest) { return publishedFollowupIssues(resultFor(request), props.task.issues); }
const orphanResults = computed(() => results.value.filter((item) => !plan.value?.followups.some((request) => request.id === item.request_id)));
</script>

<template>
  <section class="cross-review" aria-label="跨 Unit 协作解释">
    <header class="cross-heading"><div><span class="section-kicker">跨组检查</span><h2>为什么补查，实际查到了什么</h2><p>筛查决定是否需要检查；补查产生的候选仍需证据解析、策略检查和独立验证。</p></div></header>
    <div class="cross-layout">
      <article class="risk-explanation">
        <div class="cross-statuses"><div><small>确定性筛查判定</small><StatusBadge :status="presentation.decisionTone" :label="presentation.decisionLabel" /></div><div><small>协调执行状态</small><StatusBadge :status="presentation.executionTone" :label="presentation.executionLabel" /></div></div>
        <EmptyState v-if="!risk" icon="help" title="尚无跨组筛查记录" description="审查可能尚未进入筛查阶段，或该历史任务未保存此记录；未知不代表可跳过。" />
        <template v-else>
          <p class="interpretation">{{ risk.decision === 'required' ? '需要检查表示存在契约、关联或覆盖疑点，不等于已经确认缺陷。' : risk.decision === 'uncertain' ? '关联或检查记录不足，保留不确定性，等待受限关系判别。' : '当前规则允许跳过跨组补查；原始 Unit 的局部覆盖和正确性仍需分别判断。' }}</p>
          <ol class="risk-reasons"><li v-for="(reason, index) in risk.reasons" :key="index"><strong>{{ reasonLabels[reason.code] || '筛查依据' }}</strong><p>{{ reason.detail }}</p><div class="scope-links"><button v-for="id in reason.unit_ids" :key="id" type="button" @click="emit('openUnit', id)">{{ unitName(id) }} ↗</button></div><code v-for="file in reason.files" :key="file" class="scope-file">{{ file }}</code><EvidenceReferences :ids="reason.evidence_ids" :catalog="catalog" :relationships="risk.relationships" :head-sha="task.pr?.head.sha" :base-sha="task.pr?.base.sha" /></li></ol>
          <p v-if="!risk.reasons.length" class="muted">没有保存具体筛查依据。</p>
          <p class="index-note">{{ indexLabels[risk.index_status] }}<span> · 静态关联不能单独证明本次变更存在缺陷。</span></p>
          <details v-if="risk.relationships.length" class="relationships"><summary>关联路径 · {{ risk.relationships.length }}</summary><article v-for="edge in risk.relationships" :key="edge.id"><div><code>{{ edge.source_file }}<template v-if="edge.source_symbol"> · {{ edge.source_symbol }}</template></code><span>{{ relationships[edge.type] }} →</span><code>{{ edge.target_file }}<template v-if="edge.target_symbol"> · {{ edge.target_symbol }}</template></code></div><p>{{ edge.provenance || '未记录关联说明' }}</p><small>{{ edge.parser_id || '来源未记录' }} · 解析评分 {{ edge.confidence.toFixed(2) }}，不是缺陷概率</small></article></details>
        </template>
        <div v-if="plan" class="coordination-decision"><h3>协调记录</h3><StatusBadge status="neutral" :label="coordinationDecisionLabel(plan)" /><p>{{ plan.reason }}</p><EvidenceReferences :ids="plan.evidence_ids || []" :catalog="catalog" :head-sha="task.pr?.head.sha" :base-sha="task.pr?.base.sha" /></div>
        <p v-else-if="risk?.non_execution_reason" class="index-note">{{ risk.non_execution_reason }}</p>
        <p v-else-if="task.coverage?.coordination_reason" class="index-note">{{ task.coverage.coordination_reason }}</p>
      </article>

      <aside class="coordination-resources">
        <h3>协调与补查资源</h3><p>本轮观测与任务累计预算分别统计，不与全任务指标相加。</p>
        <template v-if="budget"><div class="budget-line"><span>任务累计调用预算</span><strong>{{ budget.model_calls }} / {{ budget.max_model_calls }}</strong></div><div class="budget-bar" role="progressbar" aria-label="协调共享调用预算" :aria-valuenow="Math.round(budgetPercent(budget.model_calls, budget.max_model_calls))" aria-valuemin="0" aria-valuemax="100"><i :style="{ width: `${budgetPercent(budget.model_calls, budget.max_model_calls)}%` }" /></div>
          <div class="budget-line"><span>任务累计 Token 预算</span><strong>{{ budget.token_usage.toLocaleString() }} / {{ budget.max_token_usage.toLocaleString() }}</strong></div><div class="budget-bar" :class="{ 'is-exhausted': budget.token_usage > budget.max_token_usage }"><i :style="{ width: `${budgetPercent(budget.token_usage, budget.max_token_usage)}%` }" /></div><small>包含预留与实际用量回填；失败、重试和恢复不退还已用预算。</small>
        </template><p v-else class="muted">尚无预算记录。</p>
        <dl v-if="metrics" class="runtime-facts"><div><dt>本轮调用尝试</dt><dd>{{ metrics.model_calls }}</dd></div><div><dt>失败操作 / 未返回结果</dt><dd>{{ metrics.failed_calls }} / {{ metrics.unknown_calls }}</dd></div><div><dt>恢复 / 账本复用</dt><dd>{{ metrics.resume_count }} / {{ metrics.cache_hits }}</dd></div><div><dt>预留 Token 估算</dt><dd>{{ metrics.estimated_tokens.toLocaleString() }}</dd></div><div><dt>已回报实际 Token</dt><dd>{{ metrics.usage_reported_calls ? metrics.actual_tokens.toLocaleString() : '未回报' }}<small>{{ metrics.usage_reported_calls }} 次调用有 usage 回报</small></dd></div><div><dt>可用成本 · USD</dt><dd>{{ formatCost(metrics.cost_microusd) }}</dd></div><div><dt>调用累计耗时</dt><dd>{{ (metrics.latency_ms / 1000).toFixed(1) }} 秒</dd></div><div><dt>候选 / 独立验证确认</dt><dd>{{ metrics.candidate_count }} / {{ metrics.confirmed_count }}</dd></div></dl>
        <p v-else class="muted">未保存本轮观测，不能将缺失用量视为零消耗。</p>
        <p class="coverage-contract">补查不进入原始 Unit 覆盖率分母。候选数、已确认数与最终去重后的发布数可能不同。</p>
        <details v-if="plan" class="audit-identity"><summary>审计标识与计数口径</summary><p>预算覆盖协调、补查诊断、独立验证及传输重试；本轮指标只统计当前运行账本。</p><p>复用已完成记录不会新增模型用量；缺失成本不会显示为 $0。</p><code>{{ plan.runtime_fingerprint || '历史任务未保存运行指纹' }}</code><small>{{ plan.cache_namespace || '未保存缓存命名空间' }}</small></details>
      </aside>
    </div>

    <section v-if="plan?.catalog_batches?.length" class="followups" aria-label="协调目录覆盖">
      <header><div><h3>协调目录覆盖</h3><p>每批完整加载相关 Unit 和关系；规划通过不代表补查完成或代码正确。</p></div><span>{{ plan.catalog_batches.filter(item => item.status === 'validated').length }} / {{ plan.catalog_batches.length }} 批完成规划</span></header>
      <details v-for="(batch, index) in plan.catalog_batches" :key="batch.id" class="followup-item">
        <summary>目录批次 {{ index + 1 }} · {{ batchLabels[batch.status] }} · {{ batch.unit_ids.length }} 个 Unit · {{ batch.relationship_ids.length }} 条关系</summary>
        <p v-if="batch.reason" class="index-note">{{ batch.reason }}</p>
        <div class="scope-links"><button v-for="id in batch.unit_ids" :key="id" type="button" @click="emit('openUnit', id)">{{ unitName(id) }} ↗</button></div>
      </details>
    </section>
    <section class="followups"><header><div><h3>定向补查</h3><p>先看检查问题和反证目标，再看候选处理结果。</p></div><span>{{ plan?.followups.length || 0 }} 项计划 · {{ results.length }} 项结果</span></header>
      <EmptyState v-if="!plan?.followups.length && !results.length" icon="branch" :title="risk?.decision === 'skip' ? '本次未请求定向补查' : '尚无定向补查计划'" description="没有补查或没有候选，都不能单独证明变更安全。" />
      <article v-for="(request, index) in plan?.followups || []" :key="request.id" class="followup-item"><header><div><small>补查 {{ index + 1 }}</small><h4>{{ request.question }}</h4></div><StatusBadge :status="followupPresentation(resultFor(request), task.status).tone" :label="followupPresentation(resultFor(request), task.status).label" /></header>
        <div class="scope-links"><button v-for="id in request.unit_ids" :key="id" type="button" @click="emit('openUnit', id)">{{ unitName(id) }} ↗</button></div>
        <dl class="followup-objectives"><div><dt>主检查文件</dt><dd><code>{{ request.primary_files.join('、') }}</code></dd></div><div><dt>反证目标</dt><dd>{{ request.counterevidence_goal }}</dd></div><div><dt>停止条件</dt><dd>{{ request.stop_condition }}</dd></div></dl>
        <EvidenceReferences :ids="request.evidence_ids" :catalog="catalog" :head-sha="task.pr?.head.sha" :base-sha="task.pr?.base.sha" />
        <div v-if="resultFor(request)" class="followup-conclusion"><strong>{{ followupPresentation(resultFor(request), task.status).validation }}</strong><p>{{ resultFor(request)?.reason }}</p><p v-if="resultFor(request)?.unit_result?.issues.length">本批次 {{ resultFor(request)?.unit_result?.issues.length }} 条记录：{{ issueStatusBreakdown(resultFor(request)).map(item => item.count + ' ' + item.label).join(' · ') }}</p><p v-else-if="resultFor(request)?.outcome === 'refuted'">已记录针对本次补查问题的反证，不代表整个变更正确。</p><p v-else>未报告候选不等于已找到反证。</p></div>
        <div class="followup-findings"><button v-for="issue in findings(request)" :key="issue.id" type="button" @click="emit('openIssue', issue)"><StatusBadge :status="issue.status" /><span>{{ issue.title }}</span><span>查看证据 →</span></button></div>
        <details v-if="resultFor(request)?.unit_result" class="followup-record"><summary>查看补查的实际检查记录</summary><UnitReviewRecord :result="resultFor(request)?.unit_result || undefined" :head-sha="task.pr?.head.sha" :base-sha="task.pr?.base.sha" /></details>
      </article>
      <article v-for="result in orphanResults" :key="result.request_id" class="followup-item"><StatusBadge :status="followupPresentation(result, task.status).tone" :label="followupPresentation(result, task.status).label" /><p>{{ result.reason }}</p><p class="muted">此结果没有匹配的计划记录，无法完整还原检查范围。</p></article>
      <div v-if="plan?.unresolved_questions.length" class="coordination-questions"><h4>协调仍未解决的问题</h4><ul><li v-for="(question, index) in plan.unresolved_questions" :key="index">{{ question }}</li></ul></div>
    </section>
  </section>
</template>

<style scoped>
.section-kicker { display: block; color: var(--primary); font-size: 12px; letter-spacing: .12em; font-weight: 700; margin-bottom: 8px; }
.cross-heading h2 { margin-bottom: 8px; font-size: 22px; letter-spacing: -.02em; }
.cross-heading p, .followups > header p { color: var(--text-secondary); font-size: 13px; line-height: 1.7; margin-bottom: 18px; }
.cross-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(250px, 310px); gap: 18px; }
.risk-explanation, .coordination-resources, .followup-item { min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 22px; }
.cross-statuses { display: flex; flex-wrap: wrap; gap: 26px; border-bottom: 1px solid var(--border); padding-bottom: 18px; }
.cross-statuses > div { display: grid; gap: 8px; }
small, .muted { color: var(--text-secondary); font-size: 12px; }
.interpretation { font-size: 13px; line-height: 1.75; padding: 12px 0; margin: 0; }
.risk-reasons { padding-left: 22px; margin: 0; }
.risk-reasons li { padding: 12px 0; border-top: 1px solid var(--border); }
.risk-reasons strong { font-size: 14px; }
.risk-reasons p, .coordination-decision p, .followup-conclusion p { font-size: 13px; line-height: 1.75; margin: 6px 0; overflow-wrap: anywhere; }
.scope-links { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.scope-links button { border: 1px solid var(--border); border-radius: 4px; background: var(--surface-subtle); color: var(--primary); cursor: pointer; font-size: 12px; padding: 5px 8px; overflow-wrap: anywhere; text-align: left; max-width: 100%; }
.scope-links button:hover { border-color: var(--primary); }
.scope-file { display: block; font-size: 12px; color: var(--text-secondary); overflow-wrap: anywhere; }
.index-note { font-size: 12px; color: var(--text-secondary); padding-top: 12px; margin: 0; line-height: 1.6; }
.relationships, .audit-identity { border-top: 1px solid var(--border); padding-top: 14px; margin-top: 14px; font-size: 12px; color: var(--text-secondary); overflow-wrap: anywhere; }
summary { cursor: pointer; font-weight: 600; padding: 4px 0; }
.relationships article { margin-top: 12px; padding: 12px; background: var(--surface-subtle); }
.relationships article > div { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.relationships p { margin: 8px 0; }
.coordination-decision { border-top: 1px solid var(--border); padding-top: 18px; margin-top: 18px; }
h3 { font-size: 16px; margin: 0 0 10px; }
.coordination-resources > p { margin: 0 0 16px; font-size: 12px; color: var(--text-secondary); line-height: 1.65; }
.budget-line { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 5px; font-size: 12px; margin: 16px 0 7px; }
.budget-line strong { font-variant-numeric: tabular-nums; }
.budget-bar { height: 5px; overflow: hidden; border-radius: 8px; background: var(--surface-muted); margin-bottom: 10px; }
.budget-bar i { display: block; height: 100%; background: var(--primary); }
.budget-bar.is-exhausted i { background: var(--warning); }
.runtime-facts { font-size: 12px; margin: 22px 0; }
.runtime-facts > div { display: flex; justify-content: space-between; gap: 8px; border-top: 1px solid var(--border); padding: 10px 0; }
.runtime-facts dt { color: var(--text-secondary); }
.runtime-facts dd { text-align: right; font-weight: 650; font-variant-numeric: tabular-nums; margin: 0; }
.runtime-facts small, .audit-identity small, .audit-identity code { display: block; font-size: 11px; font-weight: 400; margin-top: 6px; }
.coordination-resources > .coverage-contract { border-left: 2px solid var(--primary); padding-left: 10px; }
.followups { margin-top: 26px; }
.followups > header, .followup-item > header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.followups > header > span { font-size: 12px; color: var(--text-secondary); }
.followup-item { margin-bottom: 12px; }
.followup-item h4 { font-size: 16px; line-height: 1.65; margin: 5px 0; overflow-wrap: anywhere; }
.followup-objectives { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; margin: 16px 0; font-size: 13px; }
.followup-objectives > div:first-child { grid-column: 1 / -1; }
.followup-objectives dt { color: var(--text-secondary); margin-bottom: 5px; font-size: 12px; }
.followup-objectives dd { margin: 0; line-height: 1.7; overflow-wrap: anywhere; }
.followup-conclusion { margin-top: 14px; padding: 12px 14px; background: var(--surface-subtle); border-left: 3px solid var(--border-strong); }
.followup-conclusion strong { font-size: 13px; }
.followup-findings button { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; width: 100%; border: 0; border-top: 1px solid var(--border); background: transparent; text-align: left; padding: 12px 0; font-size: 13px; color: var(--text-primary); cursor: pointer; }
.followup-findings button > span:last-child { margin-left: auto; color: var(--primary); }
.followup-record { margin-top: 14px; font-size: 13px; color: var(--text-secondary); }
.followup-record > summary { margin-bottom: 10px; }
.coordination-questions { padding: 16px; border-left: 3px solid var(--warning); background: var(--warning-soft); font-size: 13px; line-height: 1.75; overflow-wrap: anywhere; }
.coordination-questions h4 { margin: 0; }
@media (max-width: 1100px) { .cross-layout { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 600px) { .risk-explanation, .coordination-resources, .followup-item { padding: 16px; } .cross-heading h2 { font-size: 19px; } .followup-objectives { grid-template-columns: 1fr; } .followups > header, .followup-item > header { flex-direction: column; gap: 6px; } }
</style>
