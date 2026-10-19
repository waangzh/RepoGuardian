<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { checkReviewComment, previewReviewComment } from "../api/client";
import type { ReviewCommentCheck } from "../types/publication";
import DOMPurify from "dompurify";
import { marked } from "marked";
import AppIcon from "./common/AppIcon.vue";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";

const props = defineProps<{
  markdown: string | null | undefined;
  userMarkdown?: string | null;
  reviewMarkdown?: string | null;
  reviewId?: string;
  reviewedHeadSha?: string | null;
  commentsMarkdown?: string;
  filenamePrefix?: string;
}>();

const copied = ref(false);
const feedback = ref("");
const initialView = () => props.reviewMarkdown ? "comment" as const : props.userMarkdown ? "user" as const : "audit" as const;
const reportView = ref<"user" | "comment" | "audit">(initialView());
const detail = ref<"compact" | "full">("compact");
const excludeResolved = ref(false);
const selection = ref<ReviewCommentCheck | null>(null);
const previewLoading = ref(false);
let previewController: AbortController | null = null;
const customSelection = computed(() => detail.value !== "compact" || excludeResolved.value);
const currentMarkdown = computed(() => reportView.value === "comment" ? (customSelection.value ? selection.value?.review_comment_markdown : props.reviewMarkdown)
  : props.userMarkdown && reportView.value === "user" ? props.userMarkdown : props.markdown);
const currentLabel = computed(() => reportView.value === "comment" ? "PR 评论"
  : props.userMarkdown && reportView.value === "user" ? "详细审查报告" : "技术审计记录");
const checking = ref(false);
const pendingCopy = ref<ReviewCommentCheck | null>(null);
let controller: AbortController | null = null;
function resetCopy() { controller?.abort(); controller = null; checking.value = false; pendingCopy.value = null; copied.value = false; feedback.value = ""; }
watch([currentMarkdown, () => props.reviewId, () => props.reviewedHeadSha], resetCopy);
watch([() => props.reviewMarkdown, () => props.userMarkdown, () => props.reviewId], () => { reportView.value = initialView(); });
watch([detail, excludeResolved, () => props.reviewId, () => props.reviewedHeadSha, () => props.reviewMarkdown], async () => {
  resetCopy(); previewController?.abort(); selection.value = null; previewLoading.value = false;
  if (!customSelection.value || !props.reviewId || !props.reviewMarkdown) return;
  const request = new AbortController(); previewController = request; previewLoading.value = true;
  const id = props.reviewId; const head = props.reviewedHeadSha;
  try {
    const result = await previewReviewComment(id, { detail: detail.value, exclude_resolved: excludeResolved.value }, request.signal);
    if (request.signal.aborted || previewController !== request) return;
    if (result.review_id !== id || result.reviewed_head_sha !== (head ?? null) || result.detail !== detail.value
        || !result.review_comment_markdown || !Array.isArray(result.omitted_finding_keys)) throw new Error("选择预览身份不匹配");
    selection.value = result;
  } catch { if (!request.signal.aborted) feedback.value = "选择预览加载失败，请重新选择；尚未复制。"; }
  finally { if (previewController === request) previewLoading.value = false; }
}, { flush: "sync" });
onBeforeUnmount(() => { resetCopy(); previewController?.abort(); });
const reportDialog = ref<HTMLDialogElement | null>(null);

const renderedMarkdown = computed(() => {
  const html = marked.parse(currentMarkdown.value || "", {
    async: false,
    gfm: true,
    breaks: false,
  }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style"],
  });
});

async function copyMarkdown() {
  if (!currentMarkdown.value || checking.value) return;
  if (reportView.value !== "comment") { await writeClipboard(currentMarkdown.value); return; }
  if (!props.reviewId) { feedback.value = "缺少审查身份，无法检查复制快照。"; return; }
  const id = props.reviewId;
  const head = props.reviewedHeadSha;
  const request = new AbortController();
  controller?.abort(); controller = request;
  pendingCopy.value = null; checking.value = true; copied.value = false; feedback.value = "正在检查远端 Head…";
  try {
    const result = await checkReviewComment(id, request.signal, { detail: detail.value, exclude_resolved: excludeResolved.value });
    if (request.signal.aborted || controller !== request || props.reviewId !== id || props.reviewedHeadSha !== head) return;
    if (result.review_id !== id || result.reviewed_head_sha !== (head ?? null)
        || !["current", "stale", "unknown"].includes(result.status) || !result.review_comment_markdown
        || (customSelection.value && result.detail !== detail.value)
        || (result.status === "current" && (!head || result.latest_head_sha !== head))) throw new Error("快照不匹配");
    if (result.status === "current") await writeClipboard(result.review_comment_markdown);
    else { pendingCopy.value = result; feedback.value = result.status === "stale"
      ? "PR 已有新提交：当前评论基于旧 Head。建议重新审查，或明确复制带旧快照说明的评论。"
      : "未能确认远端 Head：无法判断评论是否过期。可重试，或明确复制带状态说明的评论。"; }
  } catch { if (!request.signal.aborted) feedback.value = "快照检查失败，请重试；尚未复制。"; }
  finally { if (controller === request) { checking.value = false; controller = null; } }
}

async function writeClipboard(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    copied.value = true;
    feedback.value = "当前报告已复制。";
  } catch { copied.value = false; feedback.value = "复制失败，请查看完整报告或导出 Markdown 文件。"; }
}

async function copyAcknowledged() {
  const result = pendingCopy.value;
  pendingCopy.value = null;
  if (result) await writeClipboard(result.review_comment_markdown);
}

function exportMarkdown(comments = false) {
  const content = comments ? props.commentsMarkdown : currentMarkdown.value;
  if (!content) return;
  const prefix = (props.filenamePrefix || "repoguardian").replace(/[^a-z0-9_-]/gi, "-").slice(0, 80);
  const kind = comments ? "comments" : reportView.value === "comment" ? "pr-comment" : props.userMarkdown && reportView.value === "user" ? "report" : "audit";
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${prefix}-${kind}.md`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <section id="review-report" class="panel report-panel">
    <PanelHeader icon="report" :title="currentLabel" subtitle="预览、复制与本地导出">
      <div class="report-actions">
        <button type="button" class="button button--ghost button--compact" :disabled="!currentMarkdown" @click="reportDialog?.showModal()">查看完整报告</button>
        <button type="button" class="button button--secondary button--compact" :disabled="!currentMarkdown || checking" @click="copyMarkdown">{{ checking ? "检查快照中…" : copied ? "已复制" : reportView === 'comment' ? "复制 PR 评论" : "复制当前报告" }}</button>
        <button type="button" class="button button--primary button--compact" :disabled="!currentMarkdown" @click="exportMarkdown()">导出当前报告</button>
      </div>
    </PanelHeader>
    <div v-if="userMarkdown || reviewMarkdown" class="report-mode-switch" aria-label="报告内容选择">
      <button v-if="reviewMarkdown" type="button" :aria-pressed="reportView === 'comment'" @click="reportView = 'comment'">PR 评论 · 默认交付</button>
      <button v-if="userMarkdown" type="button" :aria-pressed="reportView === 'user'" @click="reportView = 'user'">详细报告</button>
      <details><summary>高级选项</summary><button type="button" :aria-pressed="reportView === 'audit'" @click="reportView = 'audit'">查看技术审计记录</button></details>
    </div>
    <div v-if="reportView === 'comment' && reviewMarkdown" class="report-mode-switch" aria-label="PR 评论复制选项">
      <label>内容密度 <select v-model="detail" aria-label="评论内容密度"><option value="compact">精简版 · 保留全部问题正文</option><option value="full">完整版 · 含全部范围记录</option></select></label>
      <label><input v-model="excludeResolved" type="checkbox" />排除已处理的非阻断事项</label>
      <span v-if="previewLoading" role="status">正在生成选择预览…</span>
      <span v-else-if="selection">包含 {{ selection.included_finding_keys.length }} 项，明确省略 {{ selection.omitted_finding_keys.length }} 项；保留 {{ selection.retained_blocking_keys.length }} 项已处理阻断问题。</span>
    </div>
    <p class="runtime-caption">{{ reportView === 'comment' ? '精简版保留全部已选问题正文；省略事项逐项列明，阻断问题始终保留。复制前检查远端 Head。' : reportView === 'user' ? '用于理解变更、核对问题和未决风险。' : '格式化执行与证据记录；不代表全部原始事件，历史正文保持原样。' }}</p>
    <p v-if="feedback" class="report-feedback" role="status">{{ feedback }}</p>
    <button v-if="pendingCopy" type="button" class="button button--secondary button--compact" @click="copyAcknowledged">{{ pendingCopy.status === 'stale' ? '仍复制带旧快照说明的 PR 评论' : '仍复制带未知状态说明的 PR 评论' }}</button>
    <EmptyState v-if="!currentMarkdown" icon="report" title="报告尚未生成" description="当前没有可用的报告记录；请结合任务状态判断。" />
    <article v-else class="report-document markdown-body" v-html="renderedMarkdown" />
    <dialog ref="reportDialog" class="report-dialog" @click.self="reportDialog?.close()">
      <header><strong>完整 Markdown 报告</strong><button type="button" class="icon-button" aria-label="关闭报告" @click="reportDialog?.close()"><AppIcon name="x" :size="16" /></button></header>
      <article class="report-document report-document--full markdown-body" v-html="renderedMarkdown" />
    </dialog>
  </section>
</template>
