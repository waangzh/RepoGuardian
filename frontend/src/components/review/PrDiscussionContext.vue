<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import type { DiscussionItem, DiscussionPage, DiscussionStats } from "../../types/prDiscussion";
import { getPrDiscussions } from "../../api/client";
const props = defineProps<{ reviewId: string; stats?: DiscussionStats | null }>();
const listing = ref<DiscussionPage | null>(null), selected = ref<DiscussionItem | null>(null);
const busy = ref(false), error = ref(""), offset = ref(0);
let controller: AbortController | null = null, generation = 0;
const labels: Record<string, string> = { still_present: "问题仍存在", not_present: "当前快照中该主张不成立",
  fixed: "已核验修复", insufficient_evidence: "证据不足", not_checked: "未核验", background: "背景说明" };
const fields = { claims: "问题主张", conditions: "适用条件", objections: "反驳与例外",
  repair_claims: "修复声明", decisions: "明确决定", open_questions: "待核验问题" };
function reset() { generation++; controller?.abort(); listing.value = null; selected.value = null; error.value = ""; busy.value = false; offset.value = 0; }
watch(() => props.reviewId, reset);
onBeforeUnmount(reset);
async function load(page = 1, item?: DiscussionItem, commentOffset = 0) {
  controller?.abort(); controller = new AbortController();
  const signal = controller.signal, version = ++generation, id = props.reviewId;
  busy.value = true; error.value = "";
  try {
    const result = await getPrDiscussions(id, item ? { discussion_id: item.id, comment_offset: commentOffset } : { page }, signal);
    if (signal.aborted || version !== generation || id !== props.reviewId) return;
    if (item) { selected.value = result.items[0] || null; offset.value = commentOffset; }
    else { listing.value = result; selected.value = null; }
  } catch { if (!signal.aborted && version === generation) error.value = "讨论快照加载失败，可重试；未将失败视为空讨论。"; }
  finally { if (!signal.aborted && version === generation) busy.value = false; }
}
function safeLink(value: string | null) {
  try { const url = new URL(value || ""); return url.protocol === "https:" && url.hostname === "github.com" ? url.href : undefined; }
  catch { return undefined; }
}
</script>
<template>
  <section class="pr-discussions" aria-label="已有 PR 讨论">
    <h3>已有 PR 讨论</h3>
    <p v-if="!stats || stats.status === 'not_recorded'">未记录历史讨论，不能据此判断该 PR 没有讨论。</p>
    <template v-else>
      <p>已参考 {{ stats.referenced }} 项相关讨论／包含 {{ stats.referenced_comments }} 条评论。</p>
      <p>已采集 {{ stats.collected }} 项／{{ stats.comments }} 条评论 · 已核验 {{ stats.checked }} 项 · 未核验 {{ stats.unchecked }} 项</p>
      <p>当前变更的审查结论不包含历史事项的全面核验；线程已解决不代表代码问题已修复。</p>
      <p v-if="stats.status !== 'complete'">讨论采集{{ stats.status === 'unavailable' ? '不可用' : '不完整' }}，仅参考已保存内容。</p>
      <ul v-if="stats.limitations.length"><li v-for="(value, index) in stats.limitations" :key="index">{{ value }}</li></ul>
      <button type="button" class="button button--ghost button--compact" :disabled="busy" @click="load(listing?.page || 1)">{{ listing ? '刷新已保存讨论' : '查看讨论与核验详情' }}</button>
    </template>
    <p v-if="busy" role="status">正在读取已保存讨论…</p><p v-if="error" role="alert">{{ error }}</p>
    <template v-if="listing">
      <ul><li v-for="item in listing.items" :key="item.id"><button type="button" class="text-button" :disabled="busy" @click="load(1, item)">{{ item.path || item.id }} · {{ labels[item.recheck.status] }}</button>
        <small>线程{{ item.resolved === null ? '状态未知' : item.resolved ? '已解决' : '未解决' }} · {{ item.comment_count }} 条评论</small></li></ul>
      <div class="pr-discussions__actions"><button type="button" :disabled="busy || listing.page <= 1" @click="load(listing.page - 1)">上一页</button><span>第 {{ listing.page }} 页</span><button type="button" :disabled="busy || listing.page * listing.page_size >= listing.total" @click="load(listing.page + 1)">下一页</button></div>
    </template>
    <article v-if="selected" aria-label="讨论详情">
      <h4>{{ selected.path || selected.id }}</h4><p>代码核验：{{ labels[selected.recheck.status] }}。{{ selected.recheck.reason }}</p>
      <p>平台状态：{{ selected.resolved === null ? '未知' : selected.resolved ? '已解决' : '未解决' }}；{{ selected.outdated === null ? '过期状态未知' : selected.outdated ? '位置已过期' : '位置未标为过期' }}</p>
      <a v-if="safeLink(selected.url)" :href="safeLink(selected.url)" target="_blank" rel="noreferrer">在 GitHub 查看原讨论 ↗</a>
      <div v-if="selected.summary"><h4>讨论摘要（未经代码核验的陈述）</h4><p v-if="selected.summary.partial">摘要仅覆盖部分原文，请核对以下来源。</p>
        <template v-for="(title, key) in fields" :key="key"><h5>{{ title }}</h5><ul><li v-for="text in selected.summary[key]" :key="text">{{ text }}</li></ul></template>
        <p v-for="(quote, id) in selected.summary.quotes" :key="id">{{ id }}：{{ quote }}</p></div>
      <h4>冻结原文</h4><div v-for="comment in selected.comments" :key="comment.id"><p>{{ comment.author }} · {{ comment.id }} · {{ comment.created_at }}</p><pre>{{ comment.body }}</pre></div>
      <div class="pr-discussions__actions"><button type="button" :disabled="busy || offset === 0" @click="load(1, selected, Math.max(0, offset - 20))">前 20 条评论</button><button type="button" :disabled="busy || offset + selected.comments.length >= selected.comment_count" @click="load(1, selected, offset + 20)">后 20 条评论</button></div>
      <details v-for="(evidence, index) in selected.evidence" :key="index"><summary>{{ evidence.file_path }} · {{ evidence.side || 'head' }} L{{ evidence.start_line }}–{{ evidence.end_line }}</summary><pre>{{ evidence.content }}</pre></details>
      <ul><li v-for="value in selected.recheck.limitations" :key="value">{{ value }}</li></ul>
    </article>
  </section>
</template>
<style scoped>
.pr-discussions { min-width: 0; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); line-height: 1.7; overflow-wrap: anywhere; }
.pr-discussions h3 { margin-top: 0; }.pr-discussions li { margin: 12px 0; }.pr-discussions small { display: block; color: var(--text-muted); }
.pr-discussions__actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 12px 0; }
.pr-discussions pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; max-height: 420px; overflow: auto; }
.pr-discussions article { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border); }
</style>
