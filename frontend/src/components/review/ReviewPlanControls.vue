<script setup lang="ts">
import { toRaw } from 'vue';
import type { ReviewCapabilities, ReviewPlanOptions } from '../../types/reviewPlan';
const props = defineProps<{ modelValue: ReviewPlanOptions; capabilities: ReviewCapabilities | null; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: ReviewPlanOptions] }>();
const focuses = { correctness: '正确性', security: '安全', performance: '性能', compatibility: '跨模块契约' } as const;
const limits = { model_calls: '每个 Agent 模型调用', tool_calls: '每个 Agent 工具调用', concurrency: 'Agent 并发', timeout_seconds: '总期限（秒）', output_tokens: '单次输出 Token', total_model_calls: '整轮模型调用', total_input_tokens: '整轮累计输入 Token（含缓存）' } as const;
function change(update: (plan: ReviewPlanOptions) => void) {
  const next = structuredClone(toRaw(props.modelValue));
  update(next); emit('update:modelValue', next);
}
function focus(value: keyof typeof focuses, checked: boolean) {
  change(plan => { plan.focus = checked ? [...new Set([...plan.focus, value])] : plan.focus.filter(item => item !== value); });
}
</script>
<template>
  <fieldset class="rg-plan-controls" :disabled="disabled">
    <legend class="sr-only">审查配置</legend>
    <div class="setting-section"><h3>审查深度</h3><div class="grid three">
      <label v-for="item in ([['quick','快速初筛','优先检查直接回归'],['standard','标准审查','结合调用与契约核查'],['deep','深度核验','扩大调查与核验范围']] as const)" :key="item[0]" class="option-card" :class="{ selected: modelValue.depth === item[0] }"><input type="radio" name="review-depth" :value="item[0]" :checked="modelValue.depth === item[0]" @change="change(plan => { plan.depth = item[0]; })" /><strong>{{ item[1] }}</strong><span class="opt-desc">{{ item[2] }}</span></label>
    </div></div>
    <div class="setting-section"><h3>关注重点</h3><div class="rg-checkboxes"><label v-for="(label,key) in focuses" :key="key" class="check-row"><input type="checkbox" :checked="modelValue.focus.includes(key)" @change="focus(key, ($event.target as HTMLInputElement).checked)" />{{ label }}</label></div><p v-if="!modelValue.focus.length" class="input-error">请至少选择一个审查重点。</p></div>
    <div class="setting-section"><label class="field-label">主审范围<select class="select" :value="modelValue.scope.mode" @change="change(plan => { plan.scope.mode = ($event.target as HTMLSelectElement).value as 'all' | 'selected'; if (plan.scope.mode === 'all') plan.scope.primary_files = []; })"><option value="all">全部合格变更文件</option><option value="selected">指定变更文件</option></select></label>
      <label v-if="modelValue.scope.mode === 'selected'" class="field-label space-top">文件路径（每行一个）<textarea class="textarea" :value="modelValue.scope.primary_files.join('\n')" rows="4" placeholder="src/example.py" @input="change(plan => { plan.scope.primary_files = ($event.target as HTMLTextAreaElement).value.split(/\r?\n/); })" /></label>
    </div>
    <label class="check-row"><input type="checkbox" :checked="modelValue.include_pr_discussions" :disabled="!capabilities?.pr_discussions_available" @change="change(plan => { plan.include_pr_discussions = ($event.target as HTMLInputElement).checked; })" />纳入冻结的 PR 讨论上下文</label>
    <details class="setting-section"><summary class="link">自定义运行保护上限</summary><p class="field-hint">留空采用服务端所选深度的预设；用量缺失时保持未知。</p><div class="grid two"><label v-for="(label,key) in limits" :key="key" class="field-label">{{ label }}<input class="input" type="number" min="1" :max="capabilities?.limits.maxima[key] ?? undefined" :value="modelValue.limits[key] ?? ''" @input="change(plan => { plan.limits[key] = ($event.target as HTMLInputElement).value ? Number(($event.target as HTMLInputElement).value) : null; })" /></label></div></details>
  </fieldset>
</template>
