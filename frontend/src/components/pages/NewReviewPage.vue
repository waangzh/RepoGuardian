<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { listReviews } from "../../api/client";
import type { ReviewTask } from "../../types/review";
import { validPullRequestUrl } from "../../utils/workspacePresentation";
import AppIcon from "../common/AppIcon.vue";
import ReviewStepper from "../common/ReviewStepper.vue";
import StatusBadge from "../common/StatusBadge.vue";
const props = defineProps<{ prUrl: string; loading: boolean; busy: boolean }>();
const emit = defineEmits<{ "update:prUrl": [value: string]; preview: []; open: [id: string]; history: [] }>();
const valid = computed(() => validPullRequestUrl(props.prUrl));
const recent = ref<ReviewTask[]>([]);
const error = ref("");
const loadingHistory = ref(true);
const controller = new AbortController();
onBeforeUnmount(() => controller.abort());
onMounted(async () => {
  try { const result = await listReviews({ pageSize: 3, signal: controller.signal }); if (!controller.signal.aborted) recent.value = result.items; }
  catch { if (!controller.signal.aborted) error.value = "最近记录加载失败，可在审查历史中重试。"; }
  finally { if (!controller.signal.aborted) loadingHistory.value = false; }
});
</script>
<template>
  <ReviewStepper :current="0" />
  <section class="hero-import">
    <div class="eyebrow">MAKE EVERY REVIEW EXPLAINABLE</div><h1>更清楚地审查每一个<br />Pull Request。</h1>
    <p>从代码变更出发，先了解审查范围，再获取带证据的审查结论。结论、覆盖与尚未解决的事项，清楚呈现。</p>
    <form @submit.prevent="valid && !loading && !busy && emit('preview')">
      <label class="field-label" for="pr-url">GitHub Pull Request 链接</label>
      <div class="hero-input"><input id="pr-url" class="input" type="url" :value="prUrl" placeholder="https://github.com/owner/repo/pull/123" autocomplete="url" :disabled="busy" aria-describedby="pr-hint" @input="emit('update:prUrl', ($event.target as HTMLInputElement).value)" /><button type="submit" class="btn primary" :disabled="!valid || loading || busy"><AppIcon :name="loading ? 'clock' : 'arrow-right'" />{{ loading ? '正在分析 PR…' : '分析 PR' }}</button></div>
      <p v-if="prUrl && !valid" id="pr-hint" class="input-error">请输入 https://github.com/仓库所有者/仓库/pull/编号 格式的 PR 链接。</p>
    </form>
    <div class="hero-meta"><span><AppIcon name="check-circle" />确认前不调用模型</span><span><AppIcon name="shield" />严格只读代码审查</span><span><AppIcon name="context" />证据与检查限制可追溯</span></div>
  </section>
  <div class="grid twoone space-top"><section class="card card-pad"><div class="card-header"><div><h2>继续上次审查</h2><p>来自服务端保存的任务快照</p></div><button type="button" class="link" @click="emit('history')">全部记录 →</button></div><p v-if="loadingHistory" class="muted small" role="status">正在读取最近记录…</p><p v-else-if="error" class="muted small" role="status">{{ error }}</p><p v-else-if="!recent.length" class="muted small">还没有审查记录。从一个 Pull Request 开始。</p><button v-for="item in recent" :key="item.id" type="button" class="recent-row" :disabled="busy" @click="emit('open', item.id)"><AppIcon name="branch" /><span class="recent-identity"><strong>{{ item.pr ? `#${item.pr.number} · ${item.pr.title}` : item.pr_url }}</strong><small>{{ item.pr ? `${item.pr.owner}/${item.pr.repo}` : item.id.slice(0, 12) }}</small></span><StatusBadge :status="item.status" /></button></section><section class="card card-pad"><div class="card-header"><h2>审查边界</h2><span class="badge blue">以证据为基础</span></div><div class="notice"><AppIcon name="info" /><div>服务端不执行被审查仓库的测试、构建或脚本。静态审查不能替代测试或业务验收。</div></div><p class="muted small space-top">代码上下文可能发送至已配置的模型服务。GitHub 写入需要单独预检与二次确认。</p></section></div>
</template>
