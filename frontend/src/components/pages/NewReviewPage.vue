<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { listReviews } from '../../api/client';
import type { Run } from '../../types/review';
import type { ReviewCapabilities, ReviewPlanOptions } from '../../types/reviewPlan';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
import ReviewPlanControls from '../review/ReviewPlanControls.vue';
const props = defineProps<{ prUrl: string; model: string; models: string[]; options: ReviewPlanOptions; capabilities: ReviewCapabilities | null; busy: boolean }>();
defineEmits<{ 'update:prUrl': [value: string]; 'update:model': [value: string]; 'update:options': [value: ReviewPlanOptions]; create: []; open: [id: string]; history: [] }>();
const valid = computed(() => /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*\/?$/.test(props.prUrl));
const recent = ref<Run[]>([]), error = ref(''), loading = ref(true);
const controller = new AbortController();
onBeforeUnmount(() => controller.abort()); onMounted(async () => { try { const result = await listReviews('', 1, controller.signal);
if (!controller.signal.aborted) recent.value = result.items.slice(0, 3); } catch { if (!controller.signal.aborted) error.value = '最近记录加载失败，可在审查历史中重试。'; } finally { if (!controller.signal.aborted) loading.value = false; } });
</script>
<template>
<section class="hero-import">
<h1>粘贴 GitHub PR 链接，<br />直接发起只读代码审查。</h1>
<p>服务端在创建后固定快照并运行审查。可选预览不会成为强制确认步骤。</p>
<form @submit.prevent="valid && options.focus.length && !busy && $emit('create')">
<label class="field-label" for="pr-url">GitHub Pull Request 链接</label>
<div class="hero-input">
<input id="pr-url" class="input" type="url" required :value="prUrl" placeholder="https://github.com/owner/repo/pull/123" :disabled="busy" @input="$emit('update:prUrl', ($event.target as HTMLInputElement).value)" />
<button class="btn primary" :disabled="!valid || !options.focus.length || busy">
<AppIcon name="arrow-right" />开始审查</button>
</div>
<p v-if="prUrl && !valid" class="input-error">请输入有效的 GitHub Pull Request 链接。</p>
</form>
<div class="hero-meta">
<span>
<AppIcon name="check-circle" />固定快照与可信证据</span>
<span>
<AppIcon name="shield" />严格只读代码审查</span>
</div>
</section>
<div class="grid twoone space-top">
<section class="card card-pad">
<div class="card-header">
<div>
<h2>审查偏好</h2>
<p>创建请求直接提交当前配置，由服务端校验。</p>
</div>
</div>
<details>
<summary class="link">调整深度、重点与范围 · {{ {quick:'快速初筛',standard:'标准审查',deep:'深度核验'}[options.depth] }}</summary>
<ReviewPlanControls :model-value="options" :capabilities="capabilities" :disabled="busy" @update:model-value="$emit('update:options', $event)" />
</details>
<label class="field-label space-top">执行模型<input class="input" :value="model" list="review-models" placeholder="使用服务端默认模型" :disabled="busy" @input="$emit('update:model', ($event.target as HTMLInputElement).value)" />
<datalist id="review-models">
<option v-for="id in models" :key="id" :value="id" />
</datalist>
</label>
</section>
<section class="card card-pad">
<div class="card-header">
<h2>最近审查</h2>
<button class="link" @click="$emit('history')">全部记录 →</button>
</div>
<p v-if="loading || error" class="muted small" role="status">{{ error || '正在读取最近记录…' }}</p>
<p v-else-if="!recent.length" class="muted small">还没有审查记录。从一个 Pull Request 开始。</p>
<button v-for="item in recent" :key="item.id" class="recent-row" :disabled="busy" @click="$emit('open', item.id)">
<AppIcon name="branch" />
<span class="recent-identity">
<strong>#{{ item.pr.number }} {{ item.pr.title }}</strong>
<small>{{ item.snapshot.repository }}</small>
</span>
<StatusBadge :status="item.status" />
</button>
</section>
</div>
</template>
