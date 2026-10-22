<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ReviewPlanOptions, ReviewCapabilities, ReviewLimits } from '../../types/reviewPlan';
import type { ReviewPreviewResponse } from '../../types/review';
import ReviewDepthSelector from './ReviewDepthSelector.vue';
import { usePreferences } from '../../composables/usePreferences';
const props = defineProps<{ preview: ReviewPreviewResponse; capabilities: ReviewCapabilities | null; busy: boolean }>();
const emit = defineEmits<{ change: [plan: ReviewPlanOptions] }>();
const draft = ref<ReviewPlanOptions | null>(null);
const { reviewDepthLimits, reviewDepthPresets } = usePreferences();
const depthError = ref('');
watch(() => props.preview, value => { draft.value = value.effective_plan ? JSON.parse(JSON.stringify(value.effective_plan)) as ReviewPlanOptions : null; }, { immediate: true });
const eligible = computed(() => props.preview.changed_files.filter(file => file.included || file.excluded_reason === 'outside_selected_scope'));
const modulePrefix = ref('');
const categories = { correctness: '正确性', security: '安全', performance: '性能', compatibility: '兼容性' };
const resources: { key: keyof ReviewLimits; label: string; unit: string }[] = [
  { key: 'max_model_calls', label: '最大模型调用次数', unit: '次' }, { key: 'max_tokens', label: '最大 Token', unit: 'tokens' },
  { key: 'timeout_seconds', label: '最大运行时间', unit: '秒' }];
function changed() { if (draft.value) emit('change', JSON.parse(JSON.stringify(draft.value)) as ReviewPlanOptions); }
function depth(value: ReviewPlanOptions['depth']) {
  if (!draft.value || !props.capabilities) return;
  depthError.value = '';
  try {
    const limits = reviewDepthLimits(value, props.capabilities);
    draft.value.depth = value;
    draft.value.limits = limits;
    changed();
  } catch (err) {
    depthError.value = err instanceof Error ? err.message : '请检查此档配置。';
  }
}
function scope(mode: 'all' | 'selected') {
  if (!draft.value) return;
  draft.value.scope = { mode, primary_files: mode === 'all' ? [] : eligible.value.map(file => file.file_path) };
  changed();
}
function limit(key: keyof ReviewLimits, event: Event) {
  if (!draft.value) return;
  const value = (event.target as HTMLInputElement).value;
  draft.value.limits[key] = value === '' ? null : Number(value);
  changed();
}
function selectModule() {
  if (!draft.value || !modulePrefix.value.trim()) return;
  const prefix = modulePrefix.value.trim().replace(/\/$/, '') + '/';
  draft.value.scope = { mode: 'selected', primary_files: eligible.value.filter(file => file.file_path.startsWith(prefix)).map(file => file.file_path) };
  changed();
}
</script>
<template>
  <div v-if="draft && capabilities" class="rg-plan-controls" :aria-busy="busy">
    <fieldset :disabled="busy" style="border:0;padding:0;min-width:0">
      <div class="setting-section"><h3>1. 审查深度</h3><ReviewDepthSelector :model-value="draft.depth" :presets="reviewDepthPresets(capabilities)" :disabled="busy" @update:model-value="depth" /><p v-if="depthError" class="input-error" role="alert">{{ depthError }}</p></div>
      <div class="setting-section"><h3>2. 审查重点</h3><div class="hstack wrap focus-options"><label v-for="category in capabilities.focus_categories" :key="category" class="check-chip focus-chip"><input v-model="draft.focus" type="checkbox" :value="category" @change="changed" />{{ categories[category] }}</label><button class="btn soft sm focus-all" type="button" @click="draft.focus = [...capabilities.focus_categories]; changed()">全部</button></div><p class="field-hint">重点按选择顺序传入检查策略；其他具体缺陷仍会保留。</p></div>
      <div class="setting-section"><h3>3. 主审范围</h3><div class="segmented"><button type="button" :class="{active: draft.scope.mode === 'all'}" @click="scope('all')">全部变更</button><button type="button" :class="{active: draft.scope.mode === 'selected'}" @click="scope('selected')">自定义文件 / 模块</button></div><template v-if="draft.scope.mode === 'selected'"><div class="hstack"><input v-model="modulePrefix" class="input" placeholder="模块目录，例如 src/api" aria-label="模块目录" /><button class="btn secondary sm" type="button" @click="selectModule">选择模块</button></div><label v-for="file in eligible" :key="file.file_path" class="check-row"><input v-model="draft.scope.primary_files" type="checkbox" :value="file.file_path" @change="changed" /><span class="mono break-anywhere">{{ file.file_path }}</span></label></template><p class="field-hint">未选变更会标记未主审；关联上下文可读取服务端允许的 Git 文件，敏感路径与越界链接仍拒绝。</p></div>
      <div class="setting-section"><h3>4. 单次资源约束</h3><div class="budget-grid"><label v-for="resource in resources" :key="resource.key" class="budget-row"><span class="budget-row__label">{{ resource.label }}</span><span class="budget-row__field"><input :value="draft.limits[resource.key] ?? ''" type="number" class="input budget-input" :min="resource.key === 'timeout_seconds' ? 1 : 0" :max="capabilities.limits.maxima[resource.key] ?? undefined" :aria-label="resource.label" placeholder="服务端默认" @input="limit(resource.key, $event)" /><small class="budget-row__unit">{{ resource.unit }}</small></span></label></div><p class="field-hint">限额包含核验与重试，达到上限保留成果和未检查范围。</p></div>
      <div class="setting-section"><h3>5. 历史讨论</h3><label class="check-row"><input v-model="draft.include_pr_discussions" type="checkbox" :disabled="!capabilities.pr_discussions_available" @change="changed" />审查时参考 PR 历史讨论</label><p class="field-hint">{{ capabilities.pr_discussions_available ? '开启后采集并核实历史讨论，可能增加耗时与模型用量；关闭后不采集、不提供给模型，也不执行历史复核。' : '服务端未开放历史讨论审查。' }} 变更后需校验并保存方案。</p></div>
    </fieldset>
  </div><p v-else class="notice">能力声明未加载，方案配置暂不可用，请刷新重试。</p>
</template>
<style scoped>
/* 章节标题与内容之间留出稳定间距，避免标题紧贴选项。 */
.rg-plan-controls .setting-section h3 { margin: 0 0 14px; }
.rg-plan-controls .setting-section .field-hint { margin-top: 12px; }

/* 审查重点改用紧凑 chip，减少内部留白，圆角与项目筛选 chip 一致。 */
.focus-options { align-items: center; gap: 8px; }
.rg-plan-controls .focus-chip { min-height: 36px; padding: 7px 12px; border-radius: 8px; font-size: .875rem; font-weight: 600; line-height: 1.2; }
.rg-plan-controls .focus-chip input { width: 14px; height: 14px; }
.rg-plan-controls .focus-all { min-height: 36px; padding: 7px 14px; border-radius: 8px; }

.budget-grid { display: grid; gap: 8px; }
.budget-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 16px; margin: 0; padding: 9px 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface2); }
.budget-row__label { color: var(--ink); font-size: .875rem; font-weight: 650; }
.budget-row__field { display: flex; align-items: center; gap: 8px; }
.budget-grid .budget-input { width: 124px; min-height: 36px; padding: 6px 10px; border-color: var(--line2); border-radius: 8px; background: var(--surface); font-size: .875rem; font-variant-numeric: tabular-nums; text-align: right; appearance: textfield; }
.budget-grid .budget-input::-webkit-inner-spin-button, .budget-grid .budget-input::-webkit-outer-spin-button { margin: 0; appearance: none; }
.budget-grid .budget-input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--blueWash); }
.budget-row__unit { min-width: 34px; color: var(--muted); font-size: .8125rem; }
@media (max-width: 520px) {
  .budget-row { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .budget-row__field { justify-content: flex-start; }
  .budget-grid .budget-input { width: 100%; text-align: left; }
}
</style>
