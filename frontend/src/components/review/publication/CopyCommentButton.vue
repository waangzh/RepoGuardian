<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { useReviewCommentCopy } from "../../../composables/useReviewCommentCopy";
import AppIcon from "../../common/AppIcon.vue";
import ReviewCommentConfirmation from "./ReviewCommentConfirmation.vue";

const props = withDefaults(defineProps<{
  reviewId: string;
  reviewedHeadSha: string | null;
  markdown: string;
  detail?: "compact" | "full";
  // 标签由情景矩阵给出：失败/用户停止时必须是“复制部分审查记录”，避免暗示已完成审查。
  label?: string;
  primary?: boolean;
  // 复制内容是否只代表部分记录；预览头与状态说明保持一致。
  partial?: boolean;
}>(), { detail: "compact", label: "复制 PR 评论", primary: true, partial: false });
// 复制前的快照检查结果回传给页面，供概览切换到“旧代码版本”情景；不改变复制行为。
const emit = defineEmits<{ snapshot: [status: "current" | "stale" | "unknown"]; rereview: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const { copied, feedback, checking, pendingCopy, headCheck, previewMarkdown, cancelPending, copyCheckedComment, copyAcknowledged, retryPending } = useReviewCommentCopy({
  reviewId: () => props.reviewId, reviewedHeadSha: () => props.reviewedHeadSha, markdown: () => props.markdown,
  options: () => ({ detail: props.detail || "compact", exclude_resolved: false }),
});
// 只回传实际检查结果；剪贴板写入成功不代表 Head 与快照一致。
watch(headCheck, result => {
  if (result) emit("snapshot", result.status);
});
// DOMPurify 依赖浏览器 DOM；SSR/测试环境没有 document 时保留空预览正文，不影响复制。
const rendered = computed(() => typeof DOMPurify.sanitize !== "function" ? ""
  : DOMPurify.sanitize(marked.parse(previewMarkdown.value || "", { async: false }) as string,
    { USE_PROFILES: { html: true }, FORBID_TAGS: ["style"] }));
async function copy() { await copyCheckedComment(); if (copied.value) dialog.value?.close(); }
function close() { cancelPending(); dialog.value?.close(); }
async function acknowledge() { await copyAcknowledged(); if (copied.value) dialog.value?.close(); }
async function retry() { await retryPending(); if (copied.value) dialog.value?.close(); }
function rereview() { close(); emit("rereview"); }
</script>
<template>
  <button type="button" class="button button--compact" :class="primary ? 'button--primary' : 'button--secondary'" :disabled="!markdown || checking" @click="dialog?.showModal()">
    <AppIcon name="send" :size="15" />{{ checking ? "检查 PR 版本中…" : copied ? `已${label}` : label }}
  </button>
  <p v-if="feedback" class="runtime-caption" role="status">{{ feedback }}</p>
  <dialog ref="dialog" class="comment-preview-dialog" :aria-label="pendingCopy ? '确认历史记录交付' : 'PR 评论预览'" @cancel="cancelPending" @click.self="close">
    <ReviewCommentConfirmation v-if="pendingCopy" :pending="pendingCopy" :feedback="feedback" @cancel="close" @acknowledge="acknowledge" @rereview="rereview" @retry="retry" />
    <template v-else>
      <header>
        <div><strong>{{ partial ? "部分审查记录预览" : "PR 评论预览" }}</strong><p>{{ partial ? "这不是一次完整审查的结论，只包含停止或失败前已保留的记录；复制前仍会核对 PR 当前 Head。" : "这是将复制到剪贴板的完整内容；复制前会核对 PR 当前 Head。" }}</p></div>
        <button type="button" class="icon-button" aria-label="关闭评论预览" @click="close"><AppIcon name="x" :size="16" /></button>
      </header>
      <article class="markdown-body comment-preview-dialog__body" v-html="rendered" />
      <footer>
        <button type="button" class="button button--ghost button--compact" @click="close">取消</button>
        <button type="button" class="button button--primary button--compact" :disabled="checking" @click="copy">
          {{ checking ? "检查 PR 版本中…" : label }}
        </button>
      </footer>
      <p v-if="feedback" class="runtime-caption" role="status">{{ feedback }}</p>
    </template>
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
