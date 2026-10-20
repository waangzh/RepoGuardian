<script setup lang="ts">
import { computed, ref, useId, watch } from "vue";
import type { DiscussionItem } from "../../../types/prDiscussion";
import { recheckLabel, recheckTone, safeDiscussionLink, sourceLabels } from "../../../utils/prDiscussionPresentation";
import AppIcon from "../../common/AppIcon.vue";
import DiscussionEvidence from "./DiscussionEvidence.vue";
const props = defineProps<{ item: DiscussionItem | null; title: string; loading: boolean; error: string; offset: number; canOpenFile?: boolean }>();
const emit = defineEmits<{ comments: [offset: number]; retry: []; file: [path: string] }>();
const tabs = [{ id: "verification", label: "代码核验" }, { id: "summary", label: "讨论摘要" }, { id: "original", label: "原始评论" }, { id: "evidence", label: "证据与限制" }] as const;
type Tab = typeof tabs[number]["id"];
const tab = ref<Tab>("verification"), prefix = useId();
const fields = { claims: "问题主张", conditions: "适用条件", objections: "反驳与例外", repair_claims: "修复声明", decisions: "明确决定", open_questions: "待核验问题" };
const threadState = computed(() => props.item?.resolved === true ? "GitHub 已解决" : props.item?.resolved === false ? "GitHub 未解决" : "线程状态未知");
watch(() => props.item?.id, () => { tab.value = "verification"; });
function keyboard(event: KeyboardEvent) {
  const index = tabs.findIndex(t => t.id === tab.value);
  const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
  if (next < 0) return;
  event.preventDefault(); tab.value = tabs[next]!.id;
  (event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
}
</script>
<template>
  <section class="pd-panel pd-detail" aria-label="讨论详情" :aria-busy="loading">
    <div v-if="!item" class="pd-empty"><AppIcon name="file" :size="26" /><h3>选择一条讨论</h3><p>选择历史线程，查看代码核验结果、观点摘要、冻结原文和相关证据。</p></div>
    <template v-else>
      <header class="pd-detail-top">
        <div class="pd-chips"><span class="pd-pill" :class="recheckTone(item.recheck.status)">{{ recheckLabel(item.recheck.status) }}</span><span class="pd-pill gray">{{ threadState }}</span><span v-if="item.outdated === true" class="pd-pill amber">代码定位已过期</span><span v-if="!item.complete" class="pd-pill amber">原文采集不完整</span></div>
        <h2>{{ title }}</h2><p class="pd-caption">标题来自首条冻结原文摘录；无可用原文时使用讨论来源与文件定位。</p>
        <div class="pd-meta"><span>{{ sourceLabels[item.source] }}</span><span class="pd-mono">{{ item.id }}</span><span>{{ item.path || '整个 Pull Request' }}</span><span>{{ item.comment_count }} 条评论</span></div>
      </header>
      <div class="pd-tabs" role="tablist" aria-label="讨论详情分类" @keydown="keyboard"><button v-for="entry in tabs" :id="`${prefix}-${entry.id}`" :key="entry.id" type="button" role="tab" :aria-selected="tab === entry.id" :aria-controls="`${prefix}-panel`" :tabindex="tab === entry.id ? 0 : -1" class="pd-tab" :class="{ active: tab === entry.id }" @click="tab = entry.id">{{ entry.label }}</button></div>
      <div :id="`${prefix}-panel`" class="pd-detail-body" role="tabpanel" :aria-labelledby="`${prefix}-${tab}`" tabindex="0">
        <p v-if="loading" class="pd-notice" role="status">正在读取已保存的讨论详情…</p>
        <div v-else-if="error" class="pd-notice amber" role="alert">{{ error }}<button type="button" class="pd-btn" @click="emit('retry')">重试加载详情</button></div>
        <template v-else-if="tab === 'verification'">
          <h3 class="pd-section-heading"><AppIcon name="shield" :size="17" />当前代码核验结论</h3>
          <div class="pd-conclusion" :data-tone="recheckTone(item.recheck.status)"><h3><AppIcon name="help" :size="16" />{{ recheckLabel(item.recheck.status) }}</h3><p>{{ item.recheck.reason || '未记录核验原因。' }}</p></div>
          <dl class="pd-kv"><dt>GitHub 线程状态</dt><dd>{{ threadState }}。该状态不能证明问题已修复。{{ item.outdated === null ? '位置过期状态未知。' : item.outdated ? '原定位已过期。' : '定位未标为过期。' }}</dd>
            <dt>对应代码位置</dt><dd class="pd-mono">{{ item.evidence[0]?.file_path || item.path || '未记录文件定位' }}<template v-if="item.evidence[0]"> · {{ item.evidence[0].side || '侧别未知' }} L{{ item.evidence[0].start_line }}–{{ item.evidence[0].end_line }}</template></dd>
            <dt>讨论来源</dt><dd>{{ sourceLabels[item.source] }} · {{ item.comment_count }} 条评论</dd><dt>本轮审查关系</dt><dd>{{ item.referenced ? '已纳入本轮上下文；' : '未纳入本轮上下文；' }}历史意见单独核验，不会自动算作本轮已确认问题。</dd></dl>
          <div class="pd-row pd-actions"><button type="button" class="pd-btn" @click="tab = 'evidence'">查看代码依据 <AppIcon name="arrow-right" :size="15" /></button><button type="button" class="pd-btn" @click="tab = 'original'">阅读原始评论 <AppIcon name="arrow-right" :size="15" /></button></div>
          <p class="pd-aside">结论以此次审查保存的代码核验记录为准，历史意见不替代代码证据。</p>
        </template>
        <template v-else-if="tab === 'summary'">
          <h3 class="pd-section-heading"><AppIcon name="context" :size="17" />讨论观点整理</h3><p class="pd-caption">摘要帮助理解历史评论，不等于独立的代码证据。没有原始内容时不推断主张。</p>
          <p v-if="item.summary?.partial" class="pd-notice amber">摘要仅覆盖部分原文，请核对完整来源。</p>
          <div v-if="item.summary" class="pd-summary-groups"><template v-for="(label, field) in fields" :key="field"><section v-if="item.summary[field].length" class="pd-summary-block"><h3>{{ label }}</h3><ul><li v-for="(text, index) in item.summary[field]" :key="index">{{ text }}</li></ul></section></template><details v-if="Object.keys(item.summary.quotes).length" class="pd-limits"><summary>摘要引用来源</summary><p v-for="(quote, id) in item.summary.quotes" :key="id"><code>{{ id }}</code>：{{ quote }}</p></details><p v-if="item.summary.omitted_comment_ids.length" class="pd-caption">未纳入摘要的评论：{{ item.summary.omitted_comment_ids.join('、') }}</p></div>
          <p v-else class="pd-notice">未保存讨论摘要，请阅读冻结原文。</p><p class="pd-notice amber">历史主张是否正确，请返回“代码核验”与“证据与限制”核对。</p>
        </template>
        <template v-else-if="tab === 'original'">
          <h3 class="pd-section-heading"><AppIcon name="context" :size="17" />冻结原文</h3><p class="pd-caption">显示任务采集时保存的原始评论快照，并非实时 GitHub 线程；当前页面不会补采新评论。</p>
          <article v-for="(comment, index) in item.comments" :key="comment.id" class="pd-comment"><header><div class="pd-row"><span class="pd-avatar">{{ (comment.author || '?').slice(0, 1).toUpperCase() }}</span><div><strong>{{ comment.author || '作者未知' }}</strong><small>{{ comment.created_at || '评论时间未记录' }}</small></div></div><span class="pd-pill gray">评论 {{ offset + index + 1 }} / {{ item.comment_count }}</span></header><pre>{{ comment.body }}</pre><a v-if="safeDiscussionLink(comment.url)" class="pd-source" :href="safeDiscussionLink(comment.url)" target="_blank" rel="noopener noreferrer">在 GitHub 查看此评论 <AppIcon name="external" :size="13" /></a></article>
          <p v-if="!item.comments.length" class="pd-notice">当前记录未提供评论原文。</p>
          <div class="pd-row pd-space pd-actions"><button type="button" class="pd-btn" :disabled="offset === 0" @click="emit('comments', Math.max(0, offset - 20))">前 20 条评论</button><button type="button" class="pd-btn" :disabled="offset + item.comments.length >= item.comment_count" @click="emit('comments', offset + 20)">后 20 条评论</button></div>
          <a v-if="safeDiscussionLink(item.url)" class="pd-btn" :href="safeDiscussionLink(item.url)" target="_blank" rel="noopener noreferrer">在 GitHub 查看原讨论 <AppIcon name="external" :size="14" /></a>
        </template>
        <DiscussionEvidence v-else :item="item" :can-open-file="canOpenFile" @file="emit('file', $event)" />
      </div>
    </template>
  </section>
</template>
