<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount, nextTick, useId } from "vue";
import { previewReviewComment } from "../api/client";
import { useReviewCommentCopy } from "../composables/useReviewCommentCopy";
import type { PublicationPlan, ReviewCommentCheck } from "../types/publication";
import type { ReviewTask } from '../types/review';
import AppIcon from "./common/AppIcon.vue";
import EmptyState from "./common/EmptyState.vue";
import ReportModeTabs, { type ReportMode } from './report/ReportModeTabs.vue';
import ReportContext from './report/ReportContext.vue';
import ReportMarkdown from './report/ReportMarkdown.vue';
import ReportDeliverySidebar from './report/ReportDeliverySidebar.vue';
import ReportReadableView from './report/ReportReadableView.vue';
import AuditSummaryView from './report/AuditSummaryView.vue';
import PrCommentPreview from './report/PrCommentPreview.vue';
import ReviewCommentConfirmation from './review/ReviewCommentConfirmation.vue';
import '../styles/report-delivery.css';

const props = defineProps<{
  markdown: string | null | undefined;
  userMarkdown?: string | null;
  reviewMarkdown?: string | null;
  reviewId?: string;
  reviewedHeadSha?: string | null;
  commentsMarkdown?: string;
  filenamePrefix?: string;
  task?: ReviewTask;
  plan?: PublicationPlan | null;
  embedded?: boolean;
  stale?: boolean;
  refreshing?: boolean;
}>();
const emit = defineEmits<{ publish: []; file: [path: string]; finding: [key: string]; rereview: []; snapshot: [status: "current" | "stale" | "unknown"] }>();
const panelId = useId();
const githubLink = computed(() => {
  try { const url = new URL(props.task?.pr?.html_url || props.task?.pr_url || ''); return url.protocol === 'https:' && url.hostname === 'github.com' ? url.href : null; }
  catch { return null; }
});

const initialView = () => props.reviewMarkdown ? "comment" as const : props.userMarkdown ? "user" as const : "audit" as const;
const reportView = ref<ReportMode>(initialView());
const detail = ref<"compact" | "full">("compact");
const excludeResolved = ref(false);
const includeDiscussed = ref(false);
watch(() => props.reviewId, () => {
  includeDiscussed.value = false; detail.value = "compact"; excludeResolved.value = false;
});
const selection = ref<ReviewCommentCheck | null>(null);
const previewLoading = ref(false);
const previewRevision = ref(0);
let previewController: AbortController | null = null;
const customSelection = computed(() => detail.value !== "compact" || excludeResolved.value || includeDiscussed.value);
const visibleFindingCount = computed(() => headCheck.value?.included_finding_keys.length ?? (customSelection.value ? selection.value?.included_finding_keys.length
  : props.plan ? [...props.plan.inline_drafts, ...props.plan.summary_findings].length : null));
const sourceMarkdown = computed(() => reportView.value === "comment" ? (customSelection.value ? selection.value?.review_comment_markdown : props.reviewMarkdown)
  : reportView.value === "user" ? props.userMarkdown : props.markdown);
const currentLabel = computed(() => reportView.value === "comment" ? "PR 评论草稿"
  : reportView.value === "user" ? "详细审查报告" : "技术审计导出");
const { copied, feedback, checking, pendingCopy, headCheck, previewMarkdown, checkHead, cancelPending, resetCopy, writeClipboard, copyCheckedComment, copyAcknowledged, checkedComment, retryPending } = useReviewCommentCopy({
  reviewId: () => props.reviewId, reviewedHeadSha: () => props.reviewedHeadSha, markdown: () => sourceMarkdown.value,
  options: () => ({ detail: detail.value, exclude_resolved: excludeResolved.value, include_discussed: includeDiscussed.value }),
});
const currentMarkdown = computed(() => reportView.value === "comment" ? previewMarkdown.value : sourceMarkdown.value);
watch(headCheck, result => { if (result) emit("snapshot", result.status); });
const snapshotStale = computed(() => props.stale || headCheck.value?.status === "stale");
const reportUpdated = ref(false);
watch(() => props.reviewId, () => { reportView.value = initialView(); reportUpdated.value = false; });
watch([() => props.reviewMarkdown, () => props.userMarkdown, () => props.markdown, () => props.plan], (_, previous) => {
  if (previous.some(value => value != null)) reportUpdated.value = true;
});
watch(() => props.refreshing, value => { if (value) resetCopy(); });
watch(reportView, resetCopy, { flush: 'sync' });
watch([detail, excludeResolved, includeDiscussed, previewRevision, () => props.reviewId, () => props.reviewedHeadSha, () => props.reviewMarkdown], async () => {
  resetCopy(); previewController?.abort(); selection.value = null; previewLoading.value = false;
  if (!customSelection.value || !props.reviewId || !props.reviewMarkdown) return;
  const request = new AbortController(); previewController = request; previewLoading.value = true;
  const id = props.reviewId; const head = props.reviewedHeadSha;
  try {
    const result = await previewReviewComment(id, { detail: detail.value, exclude_resolved: excludeResolved.value, include_discussed: includeDiscussed.value }, request.signal);
    if (request.signal.aborted || previewController !== request) return;
    if (result.review_id !== id || result.reviewed_head_sha !== (head ?? null) || result.detail !== detail.value
          || !result.review_comment_markdown || !Array.isArray(result.omitted_finding_keys)
          || !Array.isArray(result.included_finding_keys) || !Array.isArray(result.retained_blocking_keys)) throw new Error("选择预览身份不匹配");
    selection.value = result;
  } catch { if (!request.signal.aborted) feedback.value = "选择预览加载失败，请重新选择；尚未复制。"; }
  finally { if (previewController === request) previewLoading.value = false; }
}, { flush: "sync" });
onBeforeUnmount(() => previewController?.abort());
const reportDialog = ref<HTMLDialogElement | null>(null);
const confirmationDialog = ref<HTMLDialogElement | null>(null);
const deliveryAction = ref<'copy' | 'download'>('copy');
watch(pendingCopy, async () => { await nextTick(); if (pendingCopy.value) confirmationDialog.value?.showModal(); else confirmationDialog.value?.close(); });
const busy = computed(() => props.refreshing || checking.value || (reportView.value === 'comment' && previewLoading.value));
const headLabel = computed(() => checking.value ? '检查中' : snapshotStale.value ? 'PR 已更新' : ({ current: '与快照一致', stale: 'PR 有新提交', unknown: '无法确认' })[headCheck.value?.status || 'unknown']);
const headNote = computed(() => headCheck.value ? `检查于 ${new Date(headCheck.value.checked_at).toLocaleString('zh-CN')}。${headCheck.value.status === 'unknown' ? '暂时无法确认 PR 是否有新提交，请点击“重试检查”。' : '复制或导出时将再次核对当前 Head。'}` : snapshotStale.value ? '已检测到 PR 更新；本轮记录仅适用于旧快照，建议重新审查。' : '尚未核对远端版本。复制或导出前将由服务端检查 PR 当前 Head。');

async function copyMarkdown() {
  if (!currentMarkdown.value || busy.value) return;
  if (reportView.value !== "comment") { await writeClipboard(currentMarkdown.value); return; }
  deliveryAction.value = 'copy';
  await copyCheckedComment();
}

function downloadMarkdown(content: string, kind: string) {
  const prefix = (props.filenamePrefix || "repoguardian").replace(/[^a-z0-9_-]/gi, "-").slice(0, 80);
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${prefix}-${kind}.md`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportMarkdown() {
  if (!currentMarkdown.value || busy.value) return;
  // 外部分享类交付与复制共用快照检查；详细报告的历史导出不要求 Head 一致。
  if (reportView.value === "comment") {
    deliveryAction.value = 'download';
    await checkedComment(content => { downloadMarkdown(content, "pr-comment"); feedback.value = 'PR 评论 Markdown 已生成下载。'; });
    return;
  }
  downloadMarkdown(currentMarkdown.value, props.userMarkdown && reportView.value === "user" ? "report" : "audit");
}

function exportComments() {
  if (props.commentsMarkdown) downloadMarkdown(props.commentsMarkdown, "comments");
}
function rereview() { cancelPending(); emit("rereview"); }
</script>

<template>
  <label v-if="reportView === 'comment' && plan && [...plan.inline_drafts, ...plan.summary_findings].some(item => item.default_selection_reason === 'historical_duplicate')" class="rd-history-toggle"><input v-model="includeDiscussed" type="checkbox" />展开已有讨论问题的完整证据（默认保留风险摘要与讨论链接）</label>
  <section id="review-report" class="rd-workspace" :class="{ 'rd-workspace--embedded': embedded }">
    <header v-if="!embedded" class="rd-heading">
      <div><div class="rd-eyebrow">Review delivery workspace</div><h1>审查报告与交付</h1><p>先确认结论与局限，再生成适合不同读者的交付内容。</p></div>
      <div class="rd-top-actions"><a v-if="githubLink && !embedded" class="btn secondary" :href="githubLink" target="_blank" rel="noreferrer"><AppIcon name="external" :size="15" />在 GitHub 查看 PR</a><button type="button" class="btn secondary" :disabled="!currentMarkdown || busy" @click="exportMarkdown"><AppIcon name="download" :size="15" />导出报告</button></div>
    </header>
    <ReportContext v-if="task" :task="task" :plan="plan" :embedded="embedded" :stale="snapshotStale" />
    <section class="rd-shell" aria-label="审查报告工作台">
      <ReportModeTabs v-model="reportView" :panel-id="panelId" />
      <div class="rd-layout"><section :id="panelId" class="rd-main" role="tabpanel" :aria-label="currentLabel" tabindex="0">
        <div class="rd-view-title"><div><h2>{{ currentLabel }}</h2><p>{{ reportView === 'comment' ? '供 PR 作者和 Reviewer 阅读，可复制为 GitHub Markdown 评论，也可导出。' : reportView === 'user' ? '面向开发者的分析报告：结论、变更理解、代码依据、未发布记录、局限与建议，与后端 Markdown 覆盖同一组事实。' : '供排障、复核与历史追溯使用。先读结构化摘要，原始字段按需展开；与主导航“技术记录”运行页不同。' }}</p></div><span class="badge" :class="reportView === 'comment' ? 'blue' : reportView === 'user' ? 'success' : 'neutral'">{{ reportView === 'comment' ? '默认交付' : reportView === 'user' ? '证据导向' : '高级内容' }}</span></div>
        <p v-if="feedback" class="rd-notice rd-feedback" role="status">{{ feedback }}</p>
        <p class="rd-context-tip" role="status">{{ refreshing ? '正在更新报告，阅读位置将保留；更新后可继续交付。' : reportUpdated ? '报告已更新，已保留当前阅读位置与选择。' : '' }}</p>
        <div v-if="previewLoading && reportView === 'comment'" class="rd-notice" role="status">正在生成所选内容预览…</div>
        <ReportReadableView v-else-if="reportView === 'user' && plan" :plan="plan" :pr-url="task?.pr_url" :stale="snapshotStale" @file="emit('file', $event)" @finding="emit('finding', $event)" />
        <AuditSummaryView v-else-if="reportView === 'audit' && plan" :task="task" :plan="plan" :markdown="markdown" :stale="snapshotStale" @file="emit('file', $event)" />
        <div v-else-if="!currentMarkdown"><EmptyState icon="report" title="当前报告尚不可用" description="请结合任务状态判断，或切换到其他报告查看历史记录。" /><button v-if="reportView === 'comment' && customSelection && reviewId" type="button" class="btn secondary" @click="previewRevision += 1">重试加载所选预览</button></div>
        <div v-else class="rd-doc">
          <p v-if="snapshotStale && reportView !== 'comment'" class="rd-notice warn"><AppIcon name="history" :size="17" /><span><strong>历史快照内容。</strong>以下正文来自旧 Head，不适用于最新提交；请重新审查后再引用。</span></p>
          <div v-if="reportView === 'comment'" class="rd-notice"><AppIcon name="info" :size="17" /><div><strong>评论草稿不代表合并批准。</strong> 复制或导出前将核对 PR 当前 Head；过期或未知时需要明确确认限制。</div></div>
          <p v-if="selection && reportView === 'comment'" class="rd-context-tip" role="status">包含 {{ selection.included_finding_keys.length }} 项，明确省略 {{ selection.omitted_finding_keys.length }} 项；保留 {{ selection.retained_blocking_keys.length }} 项已处理的高优先级问题。</p>
          <div :class="{ 'rd-comment': reportView === 'comment' }">
            <header v-if="reportView === 'comment'" class="rd-comment-header"><div><strong>评论正文预览</strong><small>以下是 GitHub PR 评论将呈现的核心内容</small></div><span class="badge neutral">Markdown</span></header>
            <p v-if="reportView === 'comment' && snapshotStale" class="rd-notice warn rd-stale-notice"><AppIcon name="history" :size="17" /><span><strong>历史快照评论草稿。</strong>草稿结论来自旧 Head，不适用于最新提交；交付时会再次核对并要求明确确认。</span></p>
            <PrCommentPreview v-if="reportView === 'comment'" :content="currentMarkdown" :finding-count="visibleFindingCount" />
            <ReportMarkdown v-else :content="currentMarkdown" />
          </div>
        </div>
        <button v-if="commentsMarkdown && reportView === 'audit'" type="button" class="rd-text-button" @click="exportComments">导出逐条评论草稿（技术用途，不检查 Head）</button>
      </section><ReportDeliverySidebar :task="task" :plan="plan" v-model:detail="detail" v-model:exclude-resolved="excludeResolved" :mode="reportView" :available="Boolean(currentMarkdown) && !previewLoading" :busy="busy" :copied="copied" :head-label="headCheck || checking || snapshotStale ? headLabel : '尚未检查'" :head-tone="snapshotStale ? 'warning' : headCheck?.status === 'current' ? 'success' : headCheck ? 'warning' : 'neutral'" :head-note="headNote" :retry-check="headCheck?.status === 'unknown'" :can-check="Boolean(reviewId && reviewMarkdown)" :can-publish="Boolean(task)" :stale="snapshotStale" @copy="copyMarkdown" @download="exportMarkdown" @check="checkHead" @publish="emit('publish')" @full="reportDialog?.showModal()" /></div>
    </section>
    <dialog ref="reportDialog" class="rd-dialog" aria-label="完整 Markdown 报告" @click.self="reportDialog?.close()"><header><strong>{{ currentLabel }} · 完整正文</strong><span v-if="snapshotStale" class="badge warn">历史快照</span><button type="button" class="icon-button" aria-label="关闭报告" @click="reportDialog?.close()"><AppIcon name="x" :size="16" /></button></header><ReportMarkdown :content="currentMarkdown || ''" /></dialog>
    <dialog ref="confirmationDialog" class="rd-dialog rd-confirm-dialog" aria-label="确认历史记录交付" @cancel="cancelPending" @click.self="cancelPending"><ReviewCommentConfirmation v-if="pendingCopy" :pending="pendingCopy" :action="deliveryAction" :feedback="feedback" @cancel="cancelPending" @acknowledge="copyAcknowledged" @rereview="rereview" @retry="retryPending" /></dialog>
  </section>
</template>
