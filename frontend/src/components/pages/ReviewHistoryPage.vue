<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'; import { deleteReview, listReviews } from '../../api/client'; import type { Run } from '../../types/review'; import { terminal } from '../../utils/nativeReview'; import AppIcon from '../common/AppIcon.vue'; import EmptyState from '../common/EmptyState.vue'; import StatusBadge from '../common/StatusBadge.vue';
defineProps<{ busy?: boolean }>();
const emit = defineEmits<{ open: [id: string]; create: []; deleted: [id: string] }>();
const tasks = ref<Run[]>([]), page = ref(1), total = ref(0), search = ref(''), query = ref(''), loading = ref(false), error = ref(''), deleting = ref(false);
let controller: AbortController | null = null;
let generation = 0;
let disposed = false;
async function load() { if (disposed) return; controller?.abort();
const owner = ++generation; controller = new AbortController(); loading.value = true; error.value = '';
try { const result = await listReviews(query.value, page.value, controller.signal);
if (owner !== generation) return; tasks.value = result.items; total.value = result.total; } catch (cause) { if (owner === generation && !controller.signal.aborted) error.value = cause instanceof Error ? cause.message : '历史读取失败'; } finally { if (owner === generation) loading.value = false; } }
function find() { query.value = search.value.trim(); page.value = 1; tasks.value = []; void load(); } async function remove(item: Run) { if (deleting.value || !terminal(item.status) || !confirm('删除该审查记录？正在发布或等待对账的记录不能删除。')) return; deleting.value = true;
try { await deleteReview(item.id); emit('deleted', item.id);
if (tasks.value.length === 1 && page.value > 1) page.value--; await load(); } catch (cause) { error.value = cause instanceof Error ? cause.message : '删除失败'; } finally { deleting.value = false; } } onMounted(load); onBeforeUnmount(() => { disposed = true; generation++; controller?.abort(); });
</script>
<template>
<div class="page-head">
<div>
<h1>审查历史</h1>
<p class="title-sub">查看固定快照、审查结论与执行状态。</p>
</div>
<button class="btn primary" :disabled="busy" @click="$emit('create')">
<AppIcon name="plus" />新建审查</button>
</div>
<form class="workspace-toolbar" @submit.prevent="find">
<label class="rg-search-box">
<AppIcon name="search" />
<input v-model="search" class="input" placeholder="搜索仓库、PR 标题或任务" />
</label>
<button class="btn secondary" :disabled="loading || deleting">搜索</button>
<button type="button" class="btn secondary" :disabled="loading || deleting" @click="load">刷新</button>
<span class="muted small">共 {{ total }} 条记录</span>
</form>
<p v-if="error" class="notice warn space-top" role="alert">{{ error }}</p>
<section class="history-panel panel space-top" :aria-busy="loading">
<p v-if="loading" class="workspace-loading" role="status">正在读取审查记录…</p>
<EmptyState v-else-if="!tasks.length" icon="history" title="暂无匹配的审查任务" description="创建一条审查任务，或调整搜索条件。" action-label="发起审查" :disabled="busy" @action="$emit('create')" />
<div v-else class="history-list">
<article v-for="item in tasks" :key="item.id" class="rg-history-entry">
<div class="rg-history-icon">
<AppIcon name="branch" :size="22" />
</div>
<div class="rg-history-copy">
<button class="rg-history-title" :disabled="busy" @click="$emit('open', item.id)">#{{ item.pr.number }} {{ item.pr.title }}</button>
<p class="muted small">{{ item.snapshot.repository }} {{ new Date(item.created_at).toLocaleString() }}</p>
<code>{{ item.snapshot.head_sha.slice(0,12) }}</code>
</div>
<StatusBadge :status="item.status" />
<div class="head-actions">
<button class="btn secondary sm" :disabled="busy" @click="$emit('open', item.id)">查看记录</button>
<button v-if="terminal(item.status)" class="btn ghost sm" :disabled="deleting || busy" @click="remove(item)">删除</button>
</div>
</article>
</div>
</section>
<div class="rg-pagination">
<button class="btn secondary sm" :disabled="page === 1 || loading || deleting" @click="page--;load()">上一页</button>
<span class="muted small">第 {{ page }} 页 · 每页 20 条</span>
<button class="btn secondary sm" :disabled="page * 20 >= total || loading || deleting" @click="page++;load()">下一页</button>
</div>
</template>
