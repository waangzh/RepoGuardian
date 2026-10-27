<script setup lang="ts">
import { computed } from 'vue';
import type { ReviewPreviewResponse } from '../../types/review';
import type { ReviewCapabilities, ReviewPlanOptions } from '../../types/reviewPlan';
import { reviewerLabel } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import ReviewStepper from '../common/ReviewStepper.vue';
import ReviewPlanControls from '../review/ReviewPlanControls.vue';
const props = defineProps<{ preview: ReviewPreviewResponse; prUrl: string; model: string; options: ReviewPlanOptions; capabilities: ReviewCapabilities | null; busy: boolean; dirty: boolean }>();
defineEmits<{ submit: []; back: []; update: []; 'update:options': [value: ReviewPlanOptions]; 'update:model': [value: string] }>();
const included = computed(() => props.preview.changed_files.filter(file => file.included));
const additions = computed(() => props.preview.changed_files.reduce((sum, file) => sum + file.additions, 0));
const deletions = computed(() => props.preview.changed_files.reduce((sum, file) => sum + file.deletions, 0));
</script>
<template>
  <ReviewStepper :current="1" />
  <div class="page-head"><div><h1>确认审查方案</h1><p class="title-sub">核对固定快照、审查视角与范围。确认后才会调用模型。</p></div><span class="badge blue">只读静态审查</span></div>
  <div class="split-70"><div class="section-stack">
    <section class="card card-pad"><div class="card-header"><div><h2>本次 PR 概况</h2><p class="break-anywhere">{{ prUrl }}</p></div><AppIcon name="branch" /></div>
      <div class="grid three"><div><small>变更文件</small><div class="plan-number">{{ preview.changed_files.length }}</div></div><div><small>新增 / 删除</small><div class="plan-number">+{{ additions }} <small>/ −{{ deletions }}</small></div></div><div><small>纳入审查</small><div class="plan-number">{{ included.length }}</div></div></div>
      <details class="space-top"><summary class="link">查看文件纳入明细</summary><div v-for="file in preview.changed_files" :key="file.file_path" class="plan-file"><code>{{ file.file_path }}</code><span class="badge" :class="file.included ? 'blue' : 'neutral'">{{ file.included ? '纳入审查' : '未纳入' }}</span><p v-if="file.excluded_reason" class="muted small">{{ file.excluded_reason }}</p></div></details>
    </section>
    <section class="card card-pad"><div class="card-header"><div><h2>独立审查视角</h2><p>{{ preview.assignments.length }} 个派发任务 · {{ preview.goals.length }} 个源码检查范围，重复视角不重复计算覆盖。</p></div></div><article v-for="assignment in preview.assignments" :key="assignment.id" class="plan-file"><strong>{{ reviewerLabel(assignment.reviewer) }}</strong><p class="muted small">{{ assignment.objective }}</p><code>{{ assignment.focus_paths.join('、') }}</code></article></section>
    <section class="card card-pad"><details><summary class="link">调整配置与运行保护上限</summary><ReviewPlanControls :model-value="options" :capabilities="capabilities" :disabled="busy" @update:model-value="$emit('update:options', $event)" /><label class="field-label space-top">执行模型<input class="input" :value="model" placeholder="使用服务端默认模型" :disabled="busy" @input="$emit('update:model', ($event.target as HTMLInputElement).value)" /></label></details></section>
  </div><aside class="plan-aside"><section class="card card-pad"><div class="card-header"><h2>方案摘要</h2><AppIcon name="shield" /></div>
    <div v-for="(value,key) in { HEAD:preview.snapshot.head_sha, BASE:preview.snapshot.effective_base_sha }" :key="key" class="aside-stat"><span class="muted">{{ key }}</span><code class="break-anywhere">{{ value }}</code></div>
    <div class="aside-stat"><span class="muted">{{ dirty ? '待更新的执行模型' : '执行模型' }}</span><strong>{{ model || '服务端默认模型' }}</strong></div>
    <div class="aside-stat"><span class="muted">总期限 / 并发</span><strong>{{ preview.limits.timeout_seconds }} 秒 / {{ preview.limits.concurrency }}</strong></div>
    <div class="aside-stat"><span class="muted">每个 Agent 调用上限</span><strong>{{ preview.limits.model_calls }} 次模型 / {{ preview.limits.tool_calls }} 次工具</strong></div>
    <div class="aside-stat"><span class="muted">整轮预算</span><strong>{{ preview.limits.total_model_calls }} 次模型 / {{ preview.limits.total_input_tokens }} 输入 Token</strong></div>
    <p class="field-hint">输入包含缓存命中；请求前保守预估预留，返回后按服务商用量结算。未知调用保留预留，不按零计。</p>
    <p class="field-hint">方案有效至 {{ new Date(preview.expires_at).toLocaleTimeString() }}。配置、PR 版本或策略变化需要重新生成方案。</p>
    <p v-for="warning in preview.warnings" :key="warning" class="notice small space-top">{{ warning }}</p>
    <p v-if="dirty" class="input-error space-top" role="status">配置已修改，请先更新方案。当前派发任务、文件范围和额度仍属于上一次冻结方案。</p>
    <div class="plan-cta"><button class="btn primary" :disabled="busy || dirty || !options.focus.length" @click="$emit('submit')"><AppIcon name="play" />{{ busy ? '正在创建审查…' : '开始 AI 审查' }}</button><button class="btn secondary" :disabled="busy || !options.focus.length" @click="$emit('update')">更新方案</button><button class="btn secondary" :disabled="busy" @click="$emit('back')">返回修改 PR</button></div>
  </section></aside></div>
</template>
