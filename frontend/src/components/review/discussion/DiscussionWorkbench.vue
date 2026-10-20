<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from "vue";
import type { DiscussionStore } from "../../../composables/usePrDiscussions";
import type { DiscussionItem, DiscussionStats } from "../../../types/prDiscussion";
import { discussionNotice, discussionTitle, matchesDiscussion, safeDiscussionLink, type DiscussionFilter } from "../../../utils/prDiscussionPresentation";
import AppIcon from "../../common/AppIcon.vue";
import DiscussionThreadList from "./DiscussionThreadList.vue";
import DiscussionDetail from "./DiscussionDetail.vue";
import "../../../styles/pr-discussions.css";
const props = defineProps<{ store: DiscussionStore; initialStats?: DiscussionStats | null; prUrl?: string; headSha?: string; canOpenFile?: boolean; backLabel?: string }>();
const emit = defineEmits<{ back: []; file: [path: string] }>();
const query = ref(""), filter = ref<DiscussionFilter>("all"), page = ref(1), heading = ref<HTMLElement>(), detail = ref<HTMLElement>();
const stats = computed(() => props.store.stats.value || props.initialStats);
const filtered = computed(() => props.store.items.value.filter(item => matchesDiscussion(item, filter.value, query.value)));
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / 8)));
const visible = computed(() => filtered.value.slice((page.value - 1) * 8, page.value * 8));
const metrics = computed(() => [
  { label: "已采集线程", value: stats.value?.collected, icon: "context" },
  { label: "相关评论", value: stats.value?.comments, icon: "file" },
  { label: "已有核验结论", value: stats.value?.checked, icon: "check" },
  { label: "尚未核验充分", value: stats.value?.unchecked, icon: "help" },
]);
watch([query, filter], () => { page.value = 1; });
watch(visible, items => {
  if (!items.some(item => item.id === props.store.selected.value?.id)) {
    if (items[0]) void props.store.select(items[0]);
    else props.store.clearSelection();
  }
});
async function select(item: DiscussionItem) {
  void props.store.select(item);
  await nextTick();
  if (window.matchMedia("(max-width: 760px)").matches) { detail.value?.scrollIntoView({ behavior: "smooth", block: "start" }); detail.value?.focus({ preventScroll: true }); }
}
function reset() { query.value = ""; filter.value = "all"; page.value = 1; }
onMounted(() => {
  heading.value?.focus({ preventScroll: true });
  void props.store.load().then(() => { if (!props.store.selected.value && visible.value[0]) void props.store.select(visible.value[0]); });
});
onBeforeUnmount(() => props.store.cancel());
</script>
<template>
  <section class="pd pd-workbench" aria-label="历史讨论核验">
    <header class="pd-workhead"><div><div class="pd-row pd-breadcrumb"><button type="button" class="pd-btn ghost" @click="emit('back')">‹ {{ backLabel || '返回结果概览' }}</button><span>/ 历史讨论核验</span></div><h1 ref="heading" tabindex="-1">已有 PR 讨论</h1><p>查看历史主张、GitHub 线程状态和本轮快照下的代码核验依据。</p></div><div class="pd-row pd-actions"><button type="button" class="pd-btn" @click="reset"><AppIcon name="history" :size="15" />重置筛选</button><a v-if="safeDiscussionLink(prUrl)" class="pd-btn" :href="safeDiscussionLink(prUrl)" target="_blank" rel="noopener noreferrer">打开 GitHub PR <AppIcon name="external" :size="14" /></a></div></header>
    <div class="pd-topstats"><div v-for="metric in metrics" :key="metric.label" class="pd-panel pd-topstat"><div class="pd-caps"><AppIcon :name="metric.icon" :size="13" />{{ metric.label }}</div><b>{{ stats && !['not_recorded', 'unavailable'].includes(stats.status) ? metric.value ?? '未知' : '—' }}</b><p>{{ metric.label === '已有核验结论' ? '已获特定静态核验结论' : '基于本轮保存的讨论快照' }}</p></div></div>
    <div class="pd-notice" :class="{ amber: !stats || stats.status !== 'complete' }"><AppIcon :name="!stats || stats.status !== 'complete' ? 'alert' : 'shield'" :size="17" /><div>{{ discussionNotice(stats) }}<p v-if="stats && !['not_recorded', 'unavailable'].includes(stats.status)">已参考 {{ stats.referenced }} 项相关讨论，包含 {{ stats.referenced_comments }} 条评论。</p><ul v-if="stats?.limitations.length"><li v-for="(value, index) in stats.limitations" :key="index">{{ value }}</li></ul></div></div>
    <div v-if="store.error.value" class="pd-notice amber" role="alert">{{ store.error.value }}<button type="button" class="pd-btn" @click="store.load">重试读取快照</button></div>
    <div class="pd-board">
      <DiscussionThreadList :items="visible" :total="filtered.length" :page="page" :pages="pages" :query="query" :filter="filter" :selected-id="store.selected.value?.id" :loading="store.loading.value" :failed="!!store.error.value" @query="query = $event" @filter="filter = $event" @page="page = $event" @select="select" />
      <div ref="detail" class="pd-detail-container" tabindex="-1"><DiscussionDetail :item="store.selected.value" :title="store.selected.value ? discussionTitle(store.items.value.find(item => item.id === store.selected.value?.id) || store.selected.value) : ''" :loading="store.detailLoading.value" :error="store.detailError.value" :offset="store.commentOffset.value" :can-open-file="canOpenFile" @comments="store.selected.value && store.select(store.selected.value, $event)" @retry="store.selected.value && store.select(store.selected.value, store.commentOffset.value)" @file="emit('file', $event)" /></div>
    </div>
    <footer class="pd-work-footer"><span>静态审查 · 历史讨论仅作上下文 · 结论只适用于本轮快照</span><span v-if="headSha" class="pd-mono">Head {{ headSha.slice(0, 12) }}</span><span v-else>Head 未记录</span></footer>
  </section>
</template>
