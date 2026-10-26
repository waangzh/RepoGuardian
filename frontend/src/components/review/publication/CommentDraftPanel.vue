<script setup lang="ts">
import { computed } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { PublicationFinding } from "../../../types/publication";
import { useReviewCommentCopy } from "../../../composables/useReviewCommentCopy";
const props = defineProps<{ finding: PublicationFinding; reviewId: string; reviewedHeadSha: string | null }>();
const markdown = computed(() => props.finding.body_markdown);
const { copied, feedback, checking, pendingCopy, previewMarkdown, copyCheckedComment, copyAcknowledged } = useReviewCommentCopy({
  reviewId: () => props.reviewId, reviewedHeadSha: () => props.reviewedHeadSha, markdown: () => markdown.value,
  options: () => ({ detail: "compact", exclude_resolved: false, finding_key: props.finding.finding_key }),
});
const renderedMarkdown = computed(() => DOMPurify.sanitize(marked.parse(previewMarkdown.value || "", { async: false }) as string, {
  USE_PROFILES: { html: true }, FORBID_TAGS: ["style"],
}));
</script>
<template>
  <section v-if="markdown" class="comment-draft" aria-label="PR 评论草稿">
    <header><div><h3>PR 评论草稿</h3><p>所选问题评论 · 发布状态见 GitHub 发布页</p></div><button type="button" class="button button--secondary button--compact" :disabled="checking" @click="copyCheckedComment">{{ checking ? '检查快照中…' : copied ? '已复制' : '复制评论' }}</button></header>
    <div class="comment-draft__preview markdown-body" v-html="renderedMarkdown" />
    <p class="runtime-caption" role="status">{{ feedback || '复制前检查远端 Head；仅复制所选问题，其他问题与未决事项见完整报告。' }}</p>
    <button v-if="pendingCopy" type="button" class="button button--secondary button--compact" @click="copyAcknowledged">{{ pendingCopy.status === 'stale' ? '仍复制带旧快照说明的评论' : '仍复制带未知状态说明的评论' }}</button>
    <details class="runtime-technical"><summary>Markdown 原文（交付时核对远端 Head）</summary><textarea readonly aria-label="评论草稿 Markdown" :value="previewMarkdown" /></details>
  </section>
</template>
