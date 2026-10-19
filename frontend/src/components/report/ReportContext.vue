<script setup lang="ts">
import { computed } from 'vue';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import { reviewSituation } from '../../utils/reviewSituation';
import { coverageLabel, reportValidationLabel } from '../../utils/reportPresentation';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
const props = defineProps<{ task: ReviewTask; plan?: PublicationPlan | null }>();
const situation = computed(() => reviewSituation(props.plan));
const head = computed(() => props.plan?.snapshot.head_sha || props.task.snapshot?.head_sha);
</script>
<template>
  <section class="rd-context" aria-label="当前审查快照">
    <div class="rd-context-top">
      <div><div class="rd-pr-path">{{ task.pr ? `${task.pr.owner} / ${task.pr.repo} · PULL REQUEST #${task.pr.number}` : `REVIEW / ${task.id}` }}</div><h2>{{ task.pr?.title || '审查任务' }}</h2></div>
      <StatusBadge :status="task.status" />
    </div>
    <div class="rd-meta"><code>Head · {{ head?.slice(0, 12) || '未记录' }}</code><code v-if="task.pr">{{ task.pr.base.ref }} ← {{ task.pr.head.ref }}</code><span class="badge neutral">{{ task.changed_files.length }} 个文件</span><span class="badge blue">{{ task.mode === 'review' ? '只读静态审查' : '静态审查与修复' }}</span></div>
    <div class="rd-outcome" :class="situation?.tone || 'warning'">
      <div><div class="rd-outcome-title"><span class="rd-outcome-icon"><AppIcon :name="situation?.icon || 'help'" :size="18" /></span><strong>{{ situation?.title || '审查结论尚不可用' }}</strong></div><p>{{ situation?.description || '请结合任务状态与原始记录判断，不能推断为审查通过。' }}</p></div>
      <div class="rd-outcome-badges"><span class="badge" :class="plan?.outcome.review_completeness === 'complete' ? 'success' : 'warning'">检查覆盖：{{ coverageLabel(plan?.outcome.review_completeness) }}</span><span class="badge neutral">已确认 {{ plan?.outcome.confirmed_count ?? '未知' }} · 待确认 {{ plan?.outcome.unresolved_count ?? '未知' }}</span></div>
    </div>
    <div class="rd-boundary"><AppIcon name="flask" :size="14" />动态验证：{{ reportValidationLabel(plan?.outcome.dynamic_validation) }}<span>静态检查不替代 CI 与人工审批</span></div>
  </section>
</template>
