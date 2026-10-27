<script setup lang="ts">
import type { ReviewTask } from '../../types/review';
import { observedUsage, phaseLabel, reviewerLabel } from '../../utils/nativeReview';
import { computed } from 'vue';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
const props = defineProps<{ task: ReviewTask }>();
const usage = computed(() => observedUsage(props.task.review_report));
function resultLabel(id: string) {
  const result = props.task.review_report?.discovery_results.find(item => item.id === id);
  return result ? {completed:'已完成',failed:'失败'}[result.status] : '尚无已保存结果';
}
</script>
<template>
  <div class="page-head"><div><h2>Agent 活动</h2><p class="title-sub">每个 Agent 保有独立消息历史；此处显示父图的结构化进度和已结算用量。</p></div></div>
  <div class="grid three"><section class="card card-pad"><small>观测模型调用</small><div class="plan-number">{{ usage.calls ?? '未知' }}</div></section><section class="card card-pad"><small>观测 Token</small><div class="plan-number">{{ usage.tokens ?? '未知' }}</div></section><section class="card card-pad"><small>当前阶段</small><p class="space-top">{{ phaseLabel(task.phase) }}</p></section></div>
  <section class="card card-pad space-top"><div class="card-header"><h3>执行时间线</h3><AppIcon name="activity" /></div><p v-if="!task.steps.length" class="muted small">任务尚未记录执行步骤。</p><ol class="rg-stage-list"><li v-for="step in task.steps" :key="step.name"><StatusBadge :status="step.status" /><div class="min0"><strong>{{ phaseLabel(step.message) }}</strong><p class="muted small">{{ new Date(step.updated_at).toLocaleString() }} · {{ step.name }}</p><details><summary class="link">结构化进度</summary><pre class="rg-json">{{ JSON.stringify(step.data,null,2) }}</pre></details></div></li></ol></section>
  <section v-if="task.review_report" class="card card-pad space-top"><div class="card-header"><h3>独立审查任务</h3></div><div v-for="assignment in task.review_report.assignments" :key="assignment.id" class="plan-file"><strong>{{ reviewerLabel(assignment.reviewer) }}</strong><p class="muted small">{{ assignment.objective }}</p><code>{{ assignment.focus_paths.join('、') }}</code><p class="small">执行结果：{{ resultLabel(assignment.id) }}</p></div></section>
</template>
