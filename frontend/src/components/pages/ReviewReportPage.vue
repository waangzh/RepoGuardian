<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'; import { getPublicationCapability } from '../../api/client'; import type { Run } from '../../types/review'; import type { PublicationPlan } from '../../types/publication'; import type { PublicationCapability } from '../../types/operations'; import { terminal } from '../../utils/nativeReview'; import AppIcon from '../common/AppIcon.vue'; import ReportMarkdown from '../report/ReportMarkdown.vue'; import GitHubPublicationPanel from '../review/publication/GitHubPublicationPanel.vue';
const props = defineProps<{ task: Run; plan: PublicationPlan | null; report: string; loading: boolean }>(); defineEmits<{ export: [] }>();
const showPublish = ref(false), capability = ref<PublicationCapability | null>(null), capabilityError = ref(false);
const shortHead = computed(() => props.task.snapshot.head_sha.slice(0,8));
async function loadCapability() { capabilityError.value = false;
try { capability.value = await getPublicationCapability(); } catch { capabilityError.value = true; } } onMounted(() => { void loadCapability(); });
</script>
<template>
<div class="rd-workspace">
<div class="rd-heading">
<div>
<h2>发布</h2>
<p>基于提交 <code>{{ shortHead }}</code> · 已保存的审查结果</p>
</div>
<details class="rd-snapshot-info">
<summary>快照与交付边界</summary>
<dl>
<div>
<dt>HEAD</dt>
<dd>
<code>{{ task.snapshot.head_sha }}</code>
</dd>
</div>
<div>
<dt>有效 BASE</dt>
<dd>
<code>{{ task.snapshot.base_sha }}</code>
</dd>
</div>
<div>
<dt>声明 BASE</dt>
<dd>
<code>{{ task.snapshot.declared_base_sha }}</code>
</dd>
</div>
</dl>
<p>未执行目标仓库代码、测试或构建。发布资格由服务端独立校验。</p>
</details>
</div>
<div class="rd-shell">
<div class="rd-toolbar">
<button class="btn secondary" :disabled="!report" @click="$emit('export')">
<AppIcon name="download" />导出报告</button>
<button class="btn secondary" :aria-expanded="showPublish" @click="showPublish = !showPublish">
<AppIcon name="send" />{{ showPublish ? '收起发布面板' : '发布到 GitHub' }}</button>
</div>
<p v-if="loading" class="muted">正在读取报告…</p>
<ReportMarkdown v-else-if="report" :content="report" />
<p v-else class="muted">尚无可读取的 Markdown 报告。</p>
<GitHubPublicationPanel v-if="showPublish && plan && terminal(task.status)" :review-id="task.id" :plan="plan" :capability="capability" :capability-error="capabilityError" @retry-capability="loadCapability" @result="() => {}" />
</div>
</div>
</template>
