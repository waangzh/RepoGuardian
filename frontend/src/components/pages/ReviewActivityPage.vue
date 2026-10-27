<script setup lang="ts">
import type { ReviewTask } from '../../types/review';
import { executionErrorLabel, observedUsage, orderedProgress, phaseLabel, reviewerLabel } from '../../utils/nativeReview';
import { computed } from 'vue';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
const props = defineProps<{ task: ReviewTask }>();
const usage = computed(() => observedUsage(props.task.review_report));
const steps = computed(() => orderedProgress(props.task));
const assignments = computed(() => (props.task.review_report?.assignments ?? [])
  .map(assignment => ({ ...assignment, result: resultFor(assignment.id) })));
function resultFor(id: string) { return props.task.review_report?.discovery_results.find(item => item.id === id); }
function resultLabel(id: string) {
  if (props.task.steps.some(item => item.name === id && item.status === 'running')) return '执行中';
  const result = resultFor(id);
  return result ? {completed:'已完成',failed:'失败'}[result.status] : '尚无已保存结果';
}
</script>
<template>
  <div class="page-head"><div><h2>Agent 活动</h2><p class="title-sub">每个 Agent 保有独立消息历史；此处显示父图的结构化进度和已结算用量。</p></div></div>
  <div class="grid three"><section class="card card-pad"><small>观测模型调用</small><div class="plan-number">{{ usage.calls ?? '未知' }}</div></section><section class="card card-pad"><small>观测 Token</small><div class="plan-number">{{ usage.tokens ?? '未知' }}</div></section><section class="card card-pad"><small>当前阶段</small><p class="space-top">{{ phaseLabel(task.phase) }}</p></section></div>
  <section v-if="task.review_report?.budget.model_calls != null" class="card card-pad space-top"><h3>整轮预算与计量</h3><p>模型请求 {{ task.review_report.budget.model_calls }} / {{ task.review_report.budget.model_call_limit }}；实际输入 {{ task.review_report.budget.input_tokens ?? '未知' }}；缓存命中 {{ task.review_report.budget.cached_input_tokens ?? '未知' }}。</p><p class="small muted">已结算与未知预留合计 {{ task.review_report.budget.committed_input_tokens }} / {{ task.review_report.budget.input_token_limit }} 输入 Token；未知调用 {{ task.review_report.budget.unknown_calls }} 次。缓存输入仍占预算，不代表按未缓存价格计费。</p></section>
  <section class="card card-pad space-top"><div class="card-header"><h3>执行时间线</h3><AppIcon name="activity" /></div><p v-if="!steps.length" class="muted small">任务尚未记录执行步骤。</p><ol class="rg-stage-list"><li v-for="step in steps" :key="step.name"><StatusBadge :status="step.status" /><div class="min0"><strong>{{ phaseLabel(step.message) }}</strong><p class="muted small"><template v-if="step.started_at">开始：{{ new Date(step.started_at).toLocaleString() }}；</template>{{ step.status === 'running' ? '更新' : '结束' }}：{{ new Date(step.updated_at).toLocaleString() }} · {{ step.name }}</p><p v-if="step.data.error" class="notice warn small">{{ executionErrorLabel(String(step.data.error)) }}（{{ step.data.error }}）</p><p v-if="step.message === 'collect' && step.data.candidates === 0" class="small">本轮没有候选进入后续核验，不表示已排除全部缺陷。</p><p v-if="step.message === 'arbitrate' && step.data.candidates === 0" class="small">无候选进入独立核验。</p><p v-if="step.data.model_calls != null" class="small">累计模型调用 {{ step.data.model_calls }} / {{ task.configuration.limits.model_calls }}；工具调用 {{ step.data.tool_calls ?? '未知' }} / {{ task.configuration.limits.tool_calls }}；工具失败 {{ step.data.tool_failures ?? '未知' }} 次。</p><details><summary class="link">结构化进度</summary><pre class="rg-json">{{ JSON.stringify(step.data,null,2) }}</pre></details></div></li></ol></section>
  <section v-if="task.review_report" class="card card-pad space-top"><div class="card-header"><h3>独立审查任务</h3></div><div v-for="assignment in assignments" :key="assignment.id" class="plan-file"><strong>{{ reviewerLabel(assignment.reviewer) }}</strong><p class="muted small">{{ assignment.objective }}</p><code>{{ assignment.focus_paths.join('、') }}</code><p class="small">执行结果：{{ resultLabel(assignment.id) }}</p><template v-if="assignment.result"><p v-if="assignment.result.error" class="notice warn small">{{ executionErrorLabel(assignment.result.error) }}（{{ assignment.result.error }}）</p><p class="small">累计模型调用 {{ assignment.result.model_calls ?? '未知' }} / {{ task.configuration.limits.model_calls }}；工具失败 {{ assignment.result.tool_failures }} 次。<span v-if="assignment.result.previous_attempts.length">已包含补查前用量与失败记录。</span></p></template></div></section>
  <section v-if="steps.some(step => step.data.policy_exclusions || step.data.incomplete_searches || step.data.structured_output_retries)" class="card card-pad space-top"><h3>诊断分类</h3><p v-for="step in steps.filter(item => item.data.policy_exclusions || item.data.incomplete_searches || item.data.structured_output_retries)" :key="step.name" class="small">{{ phaseLabel(step.message) }}：策略排除记录 {{ step.data.policy_exclusions ?? 0 }}；搜索范围诊断 {{ step.data.incomplete_searches ?? 0 }}；结构化输出纠错 {{ step.data.structured_output_retries ?? 0 }}。正常续页和策略排除不计作工具执行失败。</p></section>
</template>
