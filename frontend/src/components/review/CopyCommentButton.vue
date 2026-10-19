<script setup lang="ts">
import { computed, ref } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { useReviewCommentCopy } from "../../composables/useReviewCommentCopy";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{
  reviewId: string;
  reviewedHeadSha: string | null;
  markdown: string;
  detail?: "compact" | "full";
}>();
const dialog = ref<HTMLDialogElement | null>(null);
const { copied, feedback, checking, pendingCopy, copyCheckedComment, copyAcknowledged } = useReviewCommentCopy({
  reviewId: () => props.reviewId, reviewedHeadSha: () => props.reviewedHeadSha, markdown: () => props.markdown,
  options: () => ({ detail: props.detail || "compact", exclude_resolved: false }),
});
const rendered = computed(() => DOMPurify.sanitize(marked.parse(props.markdown || "", { async: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ["style"] }));
async function copy() { await copyCheckedComment(); if (copied.value) dialog.value?.close(); }
</script>
<template>
  <button type="button" class="button button--primary button--compact" :disabled="!markdown || checking" @click="dialog?.showModal()">
    <AppIcon name="send" :size="15" />{{ checking ? "检查 PR 版本中…" : copied ? "已复制 PR 评论" : "复制 PR 评论" }}
  </button>
  <p v-if="feedback" class="runtime-caption" role="status">{{ feedback }}</p>
  <button v-if="pendingCopy" type="button" class="button button--secondary button--compact" @click="copyAcknowledged">
    {{ pendingCopy.status === 'stale' ? "仍复制带旧快照说明的评论" : "仍复制带未知状态说明的评论" }}
  </button>
  <dialog ref="dialog" class="comment-preview-dialog" aria-label="PR 评论预览" @click.self="dialog?.close()">
    <header>
      <div><strong>PR 评论预览</strong><p>这是将复制到剪贴板的完整内容；复制前会核对 PR 当前 Head。</p></div>
      <button type="button" class="icon-button" aria-label="关闭评论预览" @click="dialog?.close()"><AppIcon name="x" :size="16" /></button>
    </header>
    <article class="markdown-body comment-preview-dialog__body" v-html="rendered" />
    <footer>
      <button type="button" class="button button--ghost button--compact" @click="dialog?.close()">取消</button>
      <button type="button" class="button button--primary button--compact" :disabled="checking" @click="copy">
        {{ checking ? "检查 PR 版本中…" : "复制评论" }}
      </button>
    </footer>
    <p v-if="feedback" class="runtime-caption" role="status">{{ feedback }}</p>
  </dialog>
</template>
<style scoped>
.comment-preview-dialog { width: min(760px, calc(100vw - 32px)); max-height: 84vh; padding: 22px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--text-primary); }
.comment-preview-dialog::backdrop { background: #17232980; }
.comment-preview-dialog header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.comment-preview-dialog header p { margin: 6px 0 0; color: var(--text-secondary); font-size: 13px; }
.comment-preview-dialog__body { max-height: 60vh; overflow: auto; margin: 16px 0; padding: 18px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); }
.comment-preview-dialog footer { display: flex; justify-content: flex-end; gap: 10px; }
</style>
