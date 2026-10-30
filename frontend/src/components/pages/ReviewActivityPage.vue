<script setup lang="ts">
import { computed } from 'vue'; import type { Report, Run, WorkItem } from '../../types/review'; import { executionErrorLabel, observedUsage, phaseLabel } from '../../utils/nativeReview'; import AppIcon from '../common/AppIcon.vue'; import StatusBadge from '../common/StatusBadge.vue';
const props = defineProps<{ task: Run; units: WorkItem[]; report: Report | null }>();
const usage = computed(() => observedUsage(props.report));
const ordered = computed(() => [...props.units].sort((a,b) => a.id.localeCompare(b.id)));
</script>
<template>
<div class="page-head">
<div>
<h2>活动</h2>
<p class="title-sub">显示工作项、物料交付、失败原因与服务端记录的用量。</p>
</div>
</div>
<div class="grid three">
<section class="card card-pad">
<small>模型调用</small>
<div class="plan-number">{{ usage.calls ?? '未知' }}</div>
</section>
<section class="card card-pad">
<small>Token</small>
<div class="plan-number">{{ usage.tokens ?? '未知' }}</div>
</section>
<section class="card card-pad">
<small>当前阶段</small>
<p class="space-top">{{ phaseLabel(task.phase) }}</p>
</section>
</div>
<section class="card card-pad space-top">
<div class="card-header">
<h3>工作项</h3>
<AppIcon name="activity" />
</div>
<p v-if="!ordered.length" class="muted small">尚未记录工作项。</p>
<ol v-else class="rg-stage-list">
<li v-for="item in ordered" :key="item.id">
<StatusBadge :status="item.status" />
<div class="min0">
<strong>{{ item.kind === 'review' ? '审查' : '核验' }} · {{ item.unit_id }}</strong>
<p class="muted small">尝试 {{ item.attempt }} · 物料 {{ item.material_delivery }} · 工具调用 {{ item.tool_calls }}</p>
<p v-if="item.reused_from" class="notice small">复用此前已交付且完成的范围：<code>{{ item.reused_from }}</code>。源码字节已重新核对，历史问题由本轮独立核验。</p>
<p v-if="item.error_code" class="notice warn small">{{ executionErrorLabel(item.error_code) }}（{{ item.error_code }}）</p>
<details v-if="Object.keys(item.missing_material_reasons).length">
<summary class="link">缺失材料</summary>
<pre class="rg-json">{{ JSON.stringify(item.missing_material_reasons,null,2) }}</pre>
</details>
</div>
</li>
</ol>
</section>
<section v-if="report?.reasons.length" class="card card-pad space-top">
<h3>报告限制</h3>
<p v-for="reason in report.reasons" :key="reason">{{ reason }}</p>
</section>
</template>
