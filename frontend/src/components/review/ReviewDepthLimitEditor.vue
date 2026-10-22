<script setup lang="ts">
import { computed, ref, useId, watch } from "vue";
import { reviewDepthResources, usePreferences } from "../../composables/usePreferences";
import type { ReviewCapabilities, ReviewPlanOptions } from "../../types/reviewPlan";

const props = defineProps<{ depth: ReviewPlanOptions["depth"]; capabilities: ReviewCapabilities }>();
const { depthLimitOverrides, saveDepthLimits, resetDepthLimits, reviewDepthLimits, depthLimitsPersistent } = usePreferences();
const id = useId();
type ResourceKey = typeof reviewDepthResources[number]["key"];
const draft = ref<Record<ResourceKey, string>>({ max_model_calls: "", max_tokens: "", timeout_seconds: "" });
const preset = computed(() => props.capabilities.depth_presets.find(item => item.id === props.depth));
const stored = computed(() => JSON.stringify(depthLimitOverrides(props.depth)));
const error = ref("");
const message = ref("");
const policyError = computed(() => {
  try { reviewDepthLimits(props.depth, props.capabilities); return ""; }
  catch (err) { return err instanceof Error ? err.message : "请检查此档配置。"; }
});
const customized = computed(() => Object.keys(depthLimitOverrides(props.depth)).length > 0);
watch([preset, stored], () => {
  const values = depthLimitOverrides(props.depth);
  draft.value = {
    max_model_calls: values.max_model_calls?.toString() ?? "",
    max_tokens: values.max_tokens?.toString() ?? "",
    timeout_seconds: values.timeout_seconds?.toString() ?? "",
  };
  error.value = "";
  message.value = "";
}, { immediate: true, flush: "sync" });

function save() {
  error.value = "";
  message.value = "";
  const limits: Partial<Record<ResourceKey, number>> = {};
  for (const resource of reviewDepthResources) {
    const value = draft.value[resource.key].trim();
    if (value !== "") limits[resource.key] = Number(value);
  }
  try {
    saveDepthLimits(props.depth, limits, props.capabilities);
    message.value = depthLimitsPersistent.value ? "此档配置已保存于此浏览器。" : "浏览器存储不可用，配置仅在本页会话中生效。";
  } catch (err) {
    error.value = err instanceof Error ? err.message : "保存失败，请检查配置。";
  }
}
function reset() {
  resetDepthLimits(props.depth);
  message.value = depthLimitsPersistent.value ? "此档已恢复当前服务端预设。" : "已在本页恢复预设；浏览器存储不可用，无法持久保存。";
}
function amount(value: number | null | undefined) {
  return value == null ? "未设置" : value.toLocaleString("zh-CN");
}
</script>

<template>
  <form v-if="preset" class="depth-limit-editor" :aria-label="`${preset.label}资源配置`" @submit.prevent="save" @input="error = ''; message = ''">
    <div class="depth-limit-heading"><strong>资源上限</strong><span>{{ customized ? "已自定义" : "服务端预设" }}</span></div>
    <div class="depth-limit-fields">
      <div v-for="resource in reviewDepthResources" :key="resource.key" class="depth-limit-field">
        <label :for="`${id}-${resource.key}`">{{ resource.label }}<small>{{ resource.unit }}</small></label>
        <input :id="`${id}-${resource.key}`" :value="draft[resource.key]" type="number" class="input" step="1" :min="resource.min" :max="capabilities.limits.maxima[resource.key] ?? undefined" :placeholder="preset.limits[resource.key]?.toString() ?? '未设置'" :aria-label="`${preset.label}${resource.label}上限`" :aria-describedby="`${id}-${resource.key}-hint`" @input="draft[resource.key] = ($event.target as HTMLInputElement).value" />
        <small :id="`${id}-${resource.key}-hint`" class="depth-limit-hint">预设 {{ amount(preset.limits[resource.key]) }} · {{ capabilities.limits.maxima[resource.key] === null ? "未设服务端上限" : `最高 ${amount(capabilities.limits.maxima[resource.key])}` }}</small>
      </div>
    </div>
    <p class="depth-limit-note">留空使用对应服务端预设，保存仅影响此档。</p>
    <div class="depth-limit-actions">
      <button type="submit" class="btn secondary sm">保存此档</button>
      <button type="button" class="btn soft sm" @click="reset">恢复预设</button>
    </div>
    <p v-if="error || policyError" class="depth-limit-error" role="alert">{{ error || policyError }}</p>
    <p v-if="message" class="depth-limit-message" role="status">{{ message }}</p>
  </form>
</template>

<style scoped>
.depth-limit-editor { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--line); }
.depth-limit-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; font-size: .8125rem; }
.depth-limit-heading strong { font-size: inherit; font-weight: 600; }
.depth-limit-heading > span { color: var(--muted); }
.depth-limit-fields { display: grid; gap: 16px; }
.depth-limit-field { display: grid; grid-template-columns: minmax(0, 1fr) minmax(96px, 46%); align-items: center; gap: 6px 12px; }
.depth-limit-field label { display: grid; gap: 2px; margin: 0; color: var(--ink); font-size: .875rem; font-weight: 600; }
.depth-limit-field label small { color: var(--muted); font-size: .75rem; font-weight: 400; }
.depth-limit-field .input { width: 100%; min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--line2); border-radius: 8px; background: var(--surface); color: var(--ink); font-size: .875rem; font-variant-numeric: tabular-nums; text-align: right; appearance: textfield; }
.depth-limit-field .input::-webkit-inner-spin-button, .depth-limit-field .input::-webkit-outer-spin-button { margin: 0; appearance: none; }
.depth-limit-field .input:focus { outline: 2px solid var(--primary); outline-offset: 2px; }
.depth-limit-field .input::placeholder { color: var(--muted); opacity: 1; }
.depth-limit-hint { grid-column: 1 / -1; color: var(--muted); font-size: .75rem; line-height: 1.5; overflow-wrap: anywhere; }
.depth-limit-note, .depth-limit-message, .depth-limit-error { margin: 16px 0 0; font-size: .8125rem; line-height: 1.7; overflow-wrap: anywhere; }
.depth-limit-note, .depth-limit-message { color: var(--muted); }
.depth-limit-error { color: var(--danger); }
.depth-limit-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.depth-limit-actions .btn { min-height: 36px; }
</style>
