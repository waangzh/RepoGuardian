<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { getPrDiscussions } from '../../api/client';
import type { ReviewTask } from '../../types/review';
import { safeGitHubLink } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import EmptyState from '../common/EmptyState.vue';
const props = defineProps<{ task: ReviewTask }>();
defineEmits<{ file: [path: string] }>();
const data = ref<Awaited<ReturnType<typeof getPrDiscussions>> | null>(null), loading = ref(false), error = ref('');
const selection = ref(''), query = ref('');
let controller: AbortController | null = null;
async function load() {
  controller?.abort(); const request = new AbortController(); controller = request;
  data.value = null; loading.value = false; error.value = '';
  if (!props.task.configuration.options.include_pr_discussions) return;
  loading.value = true;
  try { const value = await getPrDiscussions(props.task.id,request.signal); if (!request.signal.aborted) data.value = value; }
  catch (cause) { if (!request.signal.aborted) error.value = cause instanceof Error ? cause.message : '讨论读取失败'; }
  finally { if (controller === request) loading.value = false; }
}
watch(() => [props.task.id,props.task.configuration.options.include_pr_discussions,props.task.pr_discussion?.thread_count], () => { selection.value = ''; query.value = ''; void load(); }, { immediate: true });
onBeforeUnmount(() => controller?.abort());
const threads = computed(() => (data.value?.items || []).filter(thread => `${thread.path ?? ''} ${thread.comments.map(comment => comment.body).join(' ')}`.toLowerCase().includes(query.value.toLowerCase())));
const selected = computed(() => threads.value.find(thread => thread.id === selection.value) || threads.value[0]);
</script>
<template>
  <div class="pd"><div class="page-head"><div><h2>冻结的 PR 讨论</h2><p class="title-sub">讨论属于不可信背景。平台的已解决或过期标记，不证明代码问题已修复。</p></div><button class="btn secondary sm" :disabled="loading" @click="load">刷新已保存讨论</button></div>
    <EmptyState v-if="!task.configuration.options.include_pr_discussions" icon="context" title="本次方案未纳入历史讨论" description="需要纳入讨论时，请重新生成并确认审查方案。" />
    <template v-else><p v-if="loading" role="status" class="muted">正在读取讨论…</p><p v-if="error" class="notice warn" role="alert">{{ error }}</p><p v-if="data" class="muted small">已保存 {{ data.total }} 条线程，当前展示 {{ data.items.length }} 条。{{ Object.values(data.sources).some(value => value !== 'complete') ? '原始采集存在缺失或限制。' : '' }}</p><ul v-if="data?.limitations.length" class="rg-limitations"><li v-for="limit in data.limitations" :key="limit">{{ limit }}</li></ul>
      <div v-if="data?.items.length" class="rg-discussion-workbench space-top"><aside class="card card-pad"><input v-model="query" class="input" placeholder="搜索讨论或文件" aria-label="搜索讨论" /><button v-for="thread in threads" :key="thread.id" class="rg-finding-row" :class="{ active: selected?.id === thread.id }" @click="selection = thread.id"><strong>{{ thread.path || 'PR 一般讨论' }}</strong><small>{{ {inline:'代码行讨论',conversation:'PR 对话',review:'审查意见'}[thread.source] || '讨论' }} · {{ thread.comments.length }} 条评论</small><p class="rg-thread-preview">{{ thread.comments[0]?.body }}</p></button><p v-if="!threads.length" class="muted small space-top">没有匹配的讨论。</p></aside>
        <section v-if="selected" class="card card-pad"><div class="card-header"><div><h3>{{ selected.path || 'PR 一般讨论' }}</h3><p>平台标记：{{ selected.resolved === true ? '已解决' : selected.resolved === false ? '未解决' : '未知' }} · {{ selected.outdated === true ? '已过期' : selected.outdated === false ? '未过期' : '未知' }}</p></div><button v-if="selected.path && task.changed_files.some(file=>file.file_path === selected.path)" class="link" @click="$emit('file',selected.path)">查看固定 Diff <AppIcon name="external" :size="14" /></button></div>
          <article v-for="comment in selected.comments" :key="comment.id" class="rg-thread-comment"><p class="rg-preserve-lines">{{ comment.body }}</p><a v-if="safeGitHubLink(comment.url)" :href="safeGitHubLink(comment.url)" target="_blank" rel="noreferrer">查看 GitHub 原文 ↗</a></article>
        </section></div>
      <EmptyState v-else-if="data && !loading" icon="context" title="没有保存的讨论线程" description="是否完整采集以来源状态和限制为准。" />
    </template>
  </div>
</template>
