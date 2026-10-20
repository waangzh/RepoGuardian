<script setup lang="ts">
import type { DiscussionItem } from "../../../types/prDiscussion";
import { discussionTitle, recheckLabel, recheckTone, sourceLabels, type DiscussionFilter } from "../../../utils/prDiscussionPresentation";
import AppIcon from "../../common/AppIcon.vue";
defineProps<{ items: DiscussionItem[]; total: number; page: number; pages: number; selectedId?: string; query: string; filter: DiscussionFilter; loading: boolean; failed: boolean }>();
const emit = defineEmits<{ select: [item: DiscussionItem]; query: [query: string]; filter: [filter: DiscussionFilter]; page: [page: number] }>();
const filters: { id: DiscussionFilter; label: string }[] = [
  { id: "all", label: "全部" }, { id: "unverified", label: "未核验" },
  { id: "verified", label: "已核验" }, { id: "inline", label: "行级评论" },
];
</script>
<template>
  <section class="pd-panel pd-threads" aria-label="讨论线程">
    <header class="pd-thread-head">
      <div class="pd-row pd-space"><h2>讨论线程 <small>{{ total }} 项</small></h2><span class="pd-caps">固定快照</span></div>
      <label class="pd-search"><AppIcon name="search" :size="18" /><input type="search" aria-label="搜索讨论" placeholder="搜索讨论主张、文件或编号" :value="query" :disabled="loading || failed" @input="emit('query', ($event.target as HTMLInputElement).value)" /></label>
      <div class="pd-filters" role="group" aria-label="筛选讨论"><button v-for="item in filters" :key="item.id" type="button" class="pd-filter" :class="{ active: filter === item.id }" :aria-pressed="filter === item.id" :disabled="loading || failed" @click="emit('filter', item.id)">{{ item.label }}</button></div>
    </header>
    <div v-if="loading" class="pd-empty" role="status"><AppIcon name="clock" :size="24" /><h3>正在读取讨论快照…</h3><p>载入已保存的线程与首条原文。</p></div>
    <div v-else-if="failed" class="pd-empty"><AppIcon name="alert" :size="24" /><h3>讨论快照尚未加载</h3><p>请重试读取，加载失败不能视为没有讨论。</p></div>
    <div v-else-if="!items.length" class="pd-empty"><AppIcon name="search" :size="24" /><h3>{{ query || filter !== 'all' ? '没有匹配的讨论' : '没有可显示的讨论' }}</h3><p>{{ query || filter !== 'all' ? '尝试清除搜索或切换状态筛选。' : '请结合上方采集状态与限制判断。' }}</p></div>
    <div v-else class="pd-thread-list">
      <button v-for="item in items" :key="item.id" type="button" class="pd-thread" :class="{ active: selectedId === item.id }" :aria-pressed="selectedId === item.id" @click="emit('select', item)">
        <span class="pd-row pd-space"><strong>{{ discussionTitle(item) }}</strong><AppIcon name="arrow-right" :size="15" /></span>
        <span class="pd-snippet">{{ item.summary?.claims[0] || item.recheck.reason || '未记录讨论摘要，请查看冻结原文。' }}</span>
        <span class="pd-pill" :class="recheckTone(item.recheck.status)">{{ recheckLabel(item.recheck.status) }}</span>
        <span class="pd-meta">{{ sourceLabels[item.source] }} · {{ item.path || '整个 PR' }} · {{ item.comment_count }} 条评论</span>
      </button>
    </div>
    <footer class="pd-pagebar"><span>第 {{ page }} / {{ pages }} 页 · {{ total }} 个线程</span><div class="pd-row"><button type="button" class="pd-btn" :disabled="loading || page <= 1" @click="emit('page', page - 1)">上一页</button><button type="button" class="pd-btn" :disabled="loading || page >= pages" @click="emit('page', page + 1)">下一页</button></div></footer>
  </section>
</template>
