<script setup lang="ts">
import { computed } from "vue";
import type { ReviewPreviewResponse } from "../../types/review";
import type { ProviderModelInfo } from "../../types/operations";
import AppIcon from "../common/AppIcon.vue";
import AppSelect from "../common/AppSelect.vue";
import ReviewPlanControls from '../review/ReviewPlanControls.vue';
import type { ReviewCapabilities, ReviewPlanOptions } from '../../types/reviewPlan';
import type { PreviewJob } from '../../types/previewJob';
import ReviewStepper from "../common/ReviewStepper.vue";
import { groupingReasonText, unitSymbolSummary, unitTitle } from "../../utils/unitPresentation";
import { incrementalReviewLabel } from "../../utils/reportPresentation";
const props = defineProps<{ capabilities: ReviewCapabilities | null; dirty: boolean; previewing: boolean; preview: ReviewPreviewResponse; prUrl: string; model: string; models: ProviderModelInfo[]; defaultModel: string; modelsLoading: boolean; modelsError: string | null; submitting: boolean; created?: boolean; previewJob?: PreviewJob | null }>();
const emit = defineEmits<{ "update:model": [value: string]; submit: []; back: []; repreview: []; change: [plan: ReviewPlanOptions]; "open-created": [] }>();
const modelOptions = computed(() => [{ value: "", label: `服务端默认${props.defaultModel ? ` · ${props.defaultModel}` : ''}` }, ...props.models.map(item => ({ value: item.id, label: item.id }))]);
const additions = computed(() => props.preview.changed_files.reduce((sum, file) => sum + file.additions, 0));
const deletions = computed(() => props.preview.changed_files.reduce((sum, file) => sum + file.deletions, 0));
const depthLabel = computed(() => {
  const depth = props.preview.effective_plan?.depth;
  return props.capabilities?.depth_presets.find(preset => preset.id === depth)?.label
    || (depth ? { quick: '快速初筛', standard: '标准审查', deep: '深度核验' }[depth] : '审查深度未知');
});
const scopeLabel = computed(() => props.preview.effective_plan?.scope.mode === 'all'
  ? '全部可审查变更' : props.preview.effective_plan?.scope.mode === 'selected' ? '自定义文件范围' : '审查范围未知');
const incrementalLabel = computed(() => incrementalReviewLabel(props.preview.incremental));
const discussionLabel = computed(() => props.preview.effective_plan?.include_pr_discussions === true
  ? '已纳入历史讨论' : props.preview.effective_plan?.include_pr_discussions === false ? '未纳入历史讨论' : '以保存的配置为准');
const resourceLabel = computed(() => {
  const plan = props.preview.effective_plan;
  const preset = props.capabilities?.depth_presets.find(item => item.id === plan?.depth);
  if (!plan || !preset) return '以已保存方案为准';
  return (Object.keys(preset.limits) as (keyof typeof preset.limits)[]).every(key => plan.limits[key] === preset.limits[key])
    ? '服务端资源预设' : '自定义资源上限';
});
// 重新校验时页面不切换，就地展示服务端真实阶段，避免长时间无反馈。
const updateStage = computed(() => {
  const stages = props.previewJob?.stages;
  const stage = stages && stages.length ? stages[stages.length - 1] : null;
  if (!stage) return '正在重新获取 PR 快照并生成方案…';
  return typeof stage.percent === 'number' ? `${stage.message} · ${stage.percent}%` : stage.message;
});
</script>
<template>
  <ReviewStepper :current="1" />
  <div class="page-head"><div><div class="eyebrow">STEP 02 / REVIEW PLAN</div><h1>确认审查方案</h1><p class="title-sub">核对本次审查深度、范围与资源配置。确认后即可开始，需要时再展开高级设置。</p></div></div>
  <div class="split-70"><div class="section-stack">
    <section class="card card-pad"><div class="card-header"><div><h2>本次 PR 概况</h2><p class="break-anywhere">{{ prUrl }}</p></div><span class="badge success">已解析 Diff</span></div><div class="grid three"><div><small>变更文件</small><div class="plan-number">{{ preview.changed_file_count }}</div></div><div><small>新增 / 删除</small><div class="plan-number">+{{ additions }} <small>/ −{{ deletions }}</small></div></div><div><small>纳入审查</small><div class="plan-number">{{ preview.included_file_count }}</div></div></div><div class="facts"><span v-for="tag in preview.risk_tags" :key="tag" class="fact">{{ tag }}</span><span class="fact">{{ preview.excluded_files.length }} 个文件被策略排除</span></div></section>
    <section class="card card-pad">
      <div class="card-header"><div><h2>{{ depthLabel }}</h2><p>{{ scopeLabel }} · {{ resourceLabel }}</p></div><AppIcon name="shield" :size="20" /></div>
      <p class="muted small">无需手动分配调用次数、Token 或运行时间。达到上限时会保留已有结果，并说明未检查的范围。</p>
      <p class="field-hint" role="status">{{ discussionLabel }}。此选择随本次方案保存。</p>
      <p v-if="incrementalLabel" class="field-hint" role="status">{{ incrementalLabel }}</p>
      <details class="setting-section rg-plan-advanced">
        <summary class="link"><AppIcon name="settings" :size="16" />高级设置<span class="muted small"> · 深度、重点、范围、模型及资源上限</span></summary>
        <div class="space-top">
          <ReviewPlanControls :preview="preview" :capabilities="capabilities" :busy="submitting || previewing" @change="emit('change', $event)" />
          <div class="setting-section"><h3>执行模型</h3><AppSelect :model-value="model" :options="modelOptions" :disabled="modelsLoading || submitting || previewing" aria-label="执行模型" @update:model-value="emit('update:model', $event)" /><p class="field-hint">模型变更后须重新预览，确认后按保存的模型执行。</p><p v-if="modelsError" class="input-error">{{ modelsError }}</p></div>
          <div class="setting-section"><h3>初始预览分组</h3><details v-for="(unit, index) in preview.review_units" :key="unit.id" class="plan-file"><summary :title="unit.primary_files[0] || unit.id">{{ unitTitle(unit, index, preview.review_units.length) }}<span v-if="unitSymbolSummary(unit)" class="plan-file__symbol">{{ unitSymbolSummary(unit) }}</span></summary><p class="small muted">{{ groupingReasonText(unit.grouping_reason) }}</p><code v-if="unit.primary_files.length > 1" class="small">{{ unit.primary_files.length }} 主审 · {{ unit.related_files.length }} 关联</code><code v-else class="small">{{ unit.related_files.length }} 个关联文件</code></details></div>
        </div>
      </details>
    </section>
  </div><aside class="plan-aside"><section class="card card-pad" :aria-busy="submitting"><div class="card-header"><div><h2>方案摘要</h2><p>当前接口支持的真实执行配置</p></div><AppIcon name="settings" /></div><div v-for="item in [['审查方式', `${depthLabel} · 只读`], ['主审范围', `${preview.included_file_count} / ${preview.changed_file_count} 个文件`], ['资源配置', resourceLabel]]" :key="item[0]" class="aside-stat"><span class="muted">{{ item[0] }}</span><strong class="break-anywhere">{{ item[1] }}</strong></div><div class="notice space-top"><AppIcon name="info" /><div>方案与 Base/Head 快照已绑定，版本或策略变化将要求重新预览。初始分组估算不代表最终调用数或费用。</div></div><p v-for="warning in preview.warnings" :key="warning" class="small input-error">{{ warning }}</p><div v-if="created" class="notice" role="status" aria-live="polite"><AppIcon name="info" :size="16" /><div><strong>审查任务已创建</strong><p>任务已进入队列，但状态读取失败。 <button type="button" class="link" @click="emit('open-created')">打开该任务</button></p></div></div><div v-else-if="submitting" class="notice" role="status" aria-live="polite"><AppIcon name="clock" :size="16" /><div><strong>正在创建审查任务</strong><p>正在校验 PR 快照与方案版本，完成后会自动进入审查进度。</p></div></div><div v-if="submitting && !created" class="progress rg-indeterminate" role="progressbar" aria-label="正在创建审查任务"><span style="width:35%" /></div><div class="plan-cta"><button type="button" class="btn secondary" :disabled="submitting || previewing" @click="emit('repreview')">{{ previewing ? '更新中…' : '更新方案' }}</button><p v-if="previewing" class="field-hint" role="status" aria-live="polite">{{ updateStage }}</p><p v-if="dirty" class="input-error">配置已变化，请先更新方案。</p><button type="button" class="btn primary" :disabled="submitting || previewing || dirty || !preview.preview_id || !capabilities" @click="emit('submit')"><AppIcon :name="submitting ? 'clock' : 'play'" :size="16" />{{ submitting ? '正在创建审查任务…' : '开始 AI 审查' }}</button><button type="button" class="btn secondary" :disabled="submitting" @click="emit('back')">返回修改 PR 链接</button></div></section></aside></div>
</template>
