<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ReviewPlanOptions, ReviewCapabilities, ReviewLimits } from '../../types/reviewPlan';
import type { ReviewPreviewResponse } from '../../types/review';
import ReviewDepthSelector from './ReviewDepthSelector.vue';
const props = defineProps<{ preview: ReviewPreviewResponse; capabilities: ReviewCapabilities | null; busy: boolean }>();
const emit = defineEmits<{ change: [plan: ReviewPlanOptions] }>();
const draft = ref<ReviewPlanOptions | null>(null);
watch(() => props.preview, value => { draft.value = value.effective_plan ? JSON.parse(JSON.stringify(value.effective_plan)) as ReviewPlanOptions : null; }, { immediate: true });
const eligible = computed(() => props.preview.changed_files.filter(file => file.included || file.excluded_reason === 'outside_selected_scope'));
const modulePrefix = ref('');
const categories = { correctness: '正确性', security: '安全', performance: '性能', compatibility: '兼容性' };
const resources: { key: keyof ReviewLimits; label: string }[] = [
  { key: 'max_model_calls', label: '最大模型调用次数' }, { key: 'max_tokens', label: '最大 Token' },
  { key: 'timeout_seconds', label: '最大运行时间（秒）' }];
function changed() { if (draft.value) emit('change', JSON.parse(JSON.stringify(draft.value)) as ReviewPlanOptions); }
function depth(value: ReviewPlanOptions['depth']) {
  if (!draft.value) return;
  draft.value.depth = value;
  const preset = props.capabilities?.depth_presets.find(item => item.id === value);
  if (preset) draft.value.limits = { ...preset.limits };
  changed();
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
  <div v-if="draft && capabilities" :aria-busy="busy">
    <fieldset :disabled="busy" style="border:0;padding:0;min-width:0">
      <div class="setting-section"><h3>1. 审查深度</h3><ReviewDepthSelector :model-value="draft.depth" :presets="capabilities.depth_presets" :disabled="busy" @update:model-value="value => value !== 'server' && depth(value)" /></div>
      <div class="setting-section"><h3>2. 审查重点</h3><div class="hstack wrap"><label v-for="category in capabilities.focus_categories" :key="category" class="check-chip"><input v-model="draft.focus" type="checkbox" :value="category" @change="changed" />{{ categories[category] }}</label><button class="btn soft sm" type="button" @click="draft.focus = [...capabilities.focus_categories]; changed()">全部</button></div><p class="field-hint">重点按选择顺序传入检查策略；其他具体缺陷仍会保留。</p></div>
      <div class="setting-section"><h3>3. 主审范围</h3><div class="segmented"><button type="button" :class="{active: draft.scope.mode === 'all'}" @click="scope('all')">全部变更</button><button type="button" :class="{active: draft.scope.mode === 'selected'}" @click="scope('selected')">自定义文件 / 模块</button></div><template v-if="draft.scope.mode === 'selected'"><div class="hstack"><input v-model="modulePrefix" class="input" placeholder="模块目录，例如 src/api" aria-label="模块目录" /><button class="btn secondary sm" type="button" @click="selectModule">选择模块</button></div><label v-for="file in eligible" :key="file.file_path" class="check-row"><input v-model="draft.scope.primary_files" type="checkbox" :value="file.file_path" @change="changed" /><span class="mono break-anywhere">{{ file.file_path }}</span></label></template><p class="field-hint">未选变更会标记未主审；关联上下文可读取服务端允许的 Git 文件，敏感路径与越界链接仍拒绝。</p></div>
      <div class="setting-section"><h3>4. 单次资源约束</h3><label v-for="resource in resources" :key="resource.key" class="budget-row"><span>{{ resource.label }}</span><input :value="draft.limits[resource.key] ?? ''" type="number" class="input" :min="resource.key === 'timeout_seconds' ? 1 : 0" :max="capabilities.limits.maxima[resource.key] ?? undefined" :aria-label="resource.label" placeholder="服务端默认" @input="limit(resource.key, $event)" /></label><p class="field-hint">限额包含核验与重试，达到上限保留成果和未检查范围。</p></div>
    </fieldset>
  </div><p v-else class="notice">能力声明未加载，方案配置暂不可用，请刷新重试。</p>
</template>
