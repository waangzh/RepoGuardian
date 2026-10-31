<script setup lang="ts">
import { computed } from 'vue'; import type { Report, Run, WorkItem } from '../../types/review'; import { executionErrorLabel, observedUsage, phaseLabel } from '../../utils/nativeReview'; import AppIcon from '../common/AppIcon.vue'; import StatusBadge from '../common/StatusBadge.vue';
const props = defineProps<{ task: Run; units: WorkItem[]; report: Report | null }>();
const usage = computed(() => observedUsage(props.report));
const ordered = computed(() => [...props.units].sort((a,b) => a.id.localeCompare(b.id)));
const materialLabel = { complete: '完整', incomplete: '不完整', not_started: '未开始' };
const groupingLabel = { pending: '等待正式语义分组', deterministic: '确定性分组', model: '语义分组已冻结', fallback: '分组降级为逐文件审查' };
const unitDescription = (id: string) => { const unit = props.task.plan.units.find(item => item.id === id); return unit ? [unit.label, ...unit.paths].filter(Boolean).join(' · ') : id; };
const count = (value: number | null) => value == null ? '未知' : value.toLocaleString('zh-CN');
</script>
<template>
<div class="page-head">
<div>
<h2>活动</h2>
<p class="title-sub">显示工作项、物料交付、失败原因与服务端记录的用量。</p>
<p v-if="task.plan.grouping_status" class="muted small">{{ groupingLabel[task.plan.grouping_status] }}。分组只是调查组织方式，不构成源码证据。</p>
</div>
</div>
<div class="rg-activity-stats rg-activity-stats-wide">
<div>
<small>模型调用</small>
<strong>{{ count(usage.calls) }}</strong>
<span>次请求</span>
</div>
<div>
<small>总输入 Token</small>
<strong>{{ count(usage.input) }}</strong>
<span>包含命中与未命中缓存</span>
</div>
<div>
<small>输入缓存</small>
<strong>{{ count(usage.cachedInput) }}</strong>
<span>{{ usage.cacheHitRate == null ? '命中率未知' : `命中率 ${(usage.cacheHitRate * 100).toFixed(1)}%` }}</span>
</div>
<div>
<small>未缓存输入</small>
<strong>{{ count(usage.uncachedInput) }}</strong>
<span>供应商未命中缓存</span>
</div>
<div>
<small>输出 Token</small>
<strong>{{ count(usage.output) }}</strong>
<span>模型生成量</span>
</div>
<div>
<small>当前阶段</small>
<strong>{{ phaseLabel(task.phase) }}</strong>
<StatusBadge :status="task.status" />
</div>
</div>
<p v-if="report?.usage.cache_unknown_calls" class="muted small space-top">部分请求缺少供应商缓存明细，缓存合计与命中率保持未知。</p>
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
<strong>{{ item.kind === 'review' ? '审查' : '核验' }} · {{ unitDescription(item.unit_id) }}</strong>
<p class="rg-activity-meta"><span>尝试 {{ item.attempt }}</span><span>物料{{ materialLabel[item.material_delivery] }}</span><span>工具调用 {{ item.tool_calls }}</span></p>
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
