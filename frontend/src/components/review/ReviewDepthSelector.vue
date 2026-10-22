<script setup lang="ts">
import { useId } from "vue";
import type { ReviewCapabilities, ReviewPlanOptions } from "../../types/reviewPlan";

defineProps<{
  modelValue: ReviewPlanOptions["depth"];
  presets: (ReviewCapabilities["depth_presets"][number] & { customized?: boolean })[];
  disabled?: boolean;
  label?: string;
}>();
const emit = defineEmits<{ "update:modelValue": [value: ReviewPlanOptions["depth"]] }>();
const id = useId();
const descriptions: Record<ReviewPlanOptions["depth"], string> = {
  quick: "聚焦直接变更的行为，限制扩展调查，先做快速筛查。",
  standard: "检查变更行为、调用关系与关联契约，按具体线索继续调查。",
  deep: "扩大调查容量，主动寻找反证，检查边界条件、兼容性与相关测试代码。",
};
function amount(value: number | null, unit: string) {
  return value === null ? "未设置" : `${value.toLocaleString("zh-CN")}${unit}`;
}
</script>

<template>
  <div class="review-depth-selector" role="group" :aria-label="label || '审查深度'">
    <div class="option-grid">
      <div v-for="preset in presets" :key="preset.id" class="option-card review-depth-card" :class="{ selected: modelValue === preset.id, 'is-disabled': disabled }">
        <button type="button" class="review-depth-choice" :disabled="disabled" :aria-label="preset.label" :aria-pressed="modelValue === preset.id" :aria-describedby="`${id}-${preset.id}`" @click="emit('update:modelValue', preset.id)">
          <span class="opt-head">{{ preset.label }}<span v-if="modelValue === preset.id" aria-hidden="true">✓</span></span>
          <span :id="`${id}-${preset.id}`" class="opt-desc">{{ descriptions[preset.id] }}</span>
          <template v-if="!$slots.configuration">
            <span class="review-depth-caption">{{ preset.customized ? '自定义资源上限' : '资源预设上限' }}</span>
            <span class="review-depth-limits">
              <span>模型调用 <strong>{{ amount(preset.limits.max_model_calls, ' 次') }}</strong></span>
              <span>Token <strong>{{ amount(preset.limits.max_tokens, '') }}</strong></span>
              <span>运行时间 <strong>{{ amount(preset.limits.timeout_seconds, ' 秒') }}</strong></span>
            </span>
          </template>
        </button>
        <slot name="configuration" :preset="preset" />
      </div>
    </div>
    <p class="field-hint">所有深度均保留独立证据核验，且不执行目标仓库代码。资源上限可在系统设置中按深度配置，也可在单次审查中调整；上限不代表预计消耗。</p>
  </div>
</template>

<style scoped>
.review-depth-selector { min-width: 0; }
.review-depth-card { display: flex; flex-direction: column; height: 100%; min-width: 0; }
.review-depth-choice { display: flex; flex-direction: column; align-items: stretch; width: 100%; flex: 1; padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.review-depth-choice:disabled { cursor: default; }
.review-depth-card.is-disabled:not(.selected) { color: var(--muted); background: var(--surface2); }
.review-depth-caption { display: block; margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--line); font-size: .8125rem; color: var(--muted); }
.review-depth-limits { display: grid; gap: 6px; margin-top: 8px; font-size: .8125rem; }
.review-depth-limits > span { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; }
.review-depth-limits strong { font-size: inherit; font-weight: 600; overflow-wrap: anywhere; }
.review-depth-selector button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
</style>
