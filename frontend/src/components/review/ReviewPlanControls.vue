<script setup lang="ts">
import { toRaw } from 'vue';
import type { ReviewCapabilities, ReviewPlanOptions } from '../../types/reviewPlan';
const props = defineProps<{ modelValue: ReviewPlanOptions; capabilities: ReviewCapabilities | null; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: ReviewPlanOptions] }>();
const focuses = { correctness: '正确性', security: '安全', performance: '性能', compatibility: '跨模块契约' } as const;
const limits = { total_input_tokens: '整轮输入 Token 预算', total_output_tokens: '整轮输出 Token 预算', timeout_seconds: '总期限（秒）', output_tokens: '单次输出 Token', concurrency: '并发', tool_calls: '单 Agent 工具调用上限', model_calls: '单 Agent 模型调用熔断', total_model_calls: '整轮模型调用熔断' } as const;
function change(update: (plan: ReviewPlanOptions) => void) { const next = structuredClone(toRaw(props.modelValue)); update(next); emit('update:modelValue', next); }
function focus(value: keyof typeof focuses, checked: boolean) { change(plan => { plan.focus = checked ? [...new Set([...plan.focus, value])] : plan.focus.filter(item => item !== value); }); }
</script>
<template>
<fieldset class="rg-plan-controls" :disabled="disabled">
<legend class="sr-only">审查配置</legend>
<div class="setting-section">
<h3>审查深度</h3>
<div class="grid three">
<label v-for="item in ([['quick','快速初筛','优先检查直接回归'],['standard','标准审查','结合调用与契约核查'],['deep','深度核验','扩大调查与核验范围']] as const)" :key="item[0]" class="option-card" :class="{ selected: modelValue.depth === item[0] }">
<input type="radio" name="review-depth" :value="item[0]" :checked="modelValue.depth === item[0]" @change="change(plan => { plan.depth = item[0]; })" />
<strong>{{ item[1] }}</strong>
<span class="opt-desc">{{ item[2] }}</span>
</label>
</div>
</div>
<div class="setting-section">
<h3>关注重点</h3>
<div class="rg-checkboxes">
<label v-for="(label,key) in focuses" :key="key" class="check-row">
<input type="checkbox" :checked="modelValue.focus.includes(key)" @change="focus(key, ($event.target as HTMLInputElement).checked)" />{{ label }}</label>
</div>
<p v-if="!modelValue.focus.length" class="input-error">请至少选择一个审查重点。</p>
</div>
<div class="setting-section">
<label class="field-label">指定文件范围（每行一个，留空审查全部变更文件）<textarea class="textarea" :value="modelValue.paths?.join('\n') || ''" rows="4" placeholder="src/example.py" @input="change(plan => { const paths = ($event.target as HTMLTextAreaElement).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean); plan.paths = paths.length ? paths : null; })" />
</label>
</div>
<label class="check-row">
<input type="checkbox" :checked="modelValue.include_pr_discussions" :disabled="!capabilities?.pr_discussions_available" @change="change(plan => { plan.include_pr_discussions = ($event.target as HTMLInputElement).checked; })" />纳入冻结的 PR 讨论上下文</label>
<details class="setting-section">
<summary class="link">自定义运行保护上限</summary>
<p class="muted small">优先以整轮 Token 和总期限控制预算；模型调用次数用作熔断，审查深度不另设次数配额。</p>
<div class="grid two">
<label v-for="(label,key) in limits" :key="key" class="field-label">{{ label }}<input class="input" type="number" min="1" :max="capabilities?.limits.maxima[key] ?? undefined" :placeholder="String(capabilities?.limits.defaults[key] ?? '')" :value="modelValue.limits[key] ?? ''" @input="change(plan => { plan.limits[key] = ($event.target as HTMLInputElement).value ? Number(($event.target as HTMLInputElement).value) : null; })" />
</label>
</div>
</details>
</fieldset>
</template>
