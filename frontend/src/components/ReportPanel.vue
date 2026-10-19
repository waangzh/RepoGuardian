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
}>();
const emit = defineEmits<{ publish: []; file: [path: string]; finding: [key: string] }>();
const panelId = useId();
const githubLink = computed(() => {
  try { const url = new URL(props.task?.pr?.html_url || props.task?.pr_url || ''); return url.protocol === 'https:' && url.hostname === 'github.com' ? url.href : null; }
  catch { return null; }
});

const initialView = () => props.reviewMarkdown ? "comment" as const : props.userMarkdown ? "user" as const : "audit" as const;
const reportView = ref<ReportMode>(initialView());
const detail = ref<"compact" | "full">("compact");
const excludeResolved = ref(false);
const selection = ref<ReviewCommentCheck | null>(null);
const previewLoading = ref(false);
const previewRevision = ref(0);
let previewController: AbortController | null = null;
const customSelection = computed(() => detail.value !== "compact" || excludeResolved.value);
const visibleFindingCount = computed(() => customSelection.value ? selection.value?.included_finding_keys.length
  : props.plan ? props.plan.inline_drafts.length + props.plan.summary_findings.length : null);
const currentMarkdown = computed(() => reportView.value === "comment" ? (customSelection.value ? selection.value?.review_comment_markdown : props.reviewMarkdown)
  : reportView.value === "user" ? props.userMarkdown : props.markdown);
const currentLabel = computed(() => reportView.value === "comment" ? "PR 交付摘要"
  : reportView.value === "user" ? "详细审查报告" : "技术审计记录");
const { copied, feedback, checking, pendingCopy, headCheck, checkHead, cancelPending, resetCopy, writeClipboard, copyCheckedComment, copyAcknowledged, checkedComment } = useReviewCommentCopy({
  reviewId: () => props.reviewId, reviewedHeadSha: () => props.reviewedHeadSha, markdown: () => currentMarkdown.value,
  options: () => ({ detail: detail.value, exclude_resolved: excludeResolved.value }),
});
watch([() => props.reviewMarkdown, () => props.userMarkdown, () => props.reviewId], () => { reportView.value = initialView(); });
watch(reportView, resetCopy, { flush: 'sync' });
watch([detail, excludeResolved, previewRevision, () => props.reviewId, () => props.reviewedHeadSha, () => props.reviewMarkdown], async () => {
  resetCopy(); previewController?.abort(); selection.value = null; previewLoading.value = false;
  if (!customSelection.value || !props.reviewId || !props.reviewMarkdown) return;
  const request = new AbortController(); previewController = request; previewLoading.value = true;
  const id = props.reviewId; const head = props.reviewedHeadSha;
  try {
    const result = await previewReviewComment(id, { detail: detail.value, exclude_resolved: excludeResolved.value }, request.signal);
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
watch(pendingCopy, async value => { await nextTick(); if (value) confirmationDialog.value?.showModal(); else confirmationDialog.value?.close(); });
const busy = computed(() => checking.value || (reportView.value === 'comment' && previewLoading.value));
const headLabel = computed(() => checking.value ? '检查中' : ({ current: '与快照一致', stale: 'PR 有新提交', unknown: '无法确认' })[headCheck.value?.status || 'unknown']);
const headNote = computed(() => headCheck.value ? `检查于 ${new Date(headCheck.value.checked_at).toLocaleString('zh-CN')}。复制或导出时将再次核对当前 Head。` : '尚未核对远端版本。复制或导出前将由服务端检查 PR 当前 Head。');

async function copyMarkdown() {
  if (!currentMarkdown.value || checking.value) return;
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
  if (!currentMarkdown.value || checking.value) return;
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
</script>

<template>
  <section id="review-report" class="rd-workspace" :class="{ 'rd-workspace--embedded': embedded }">
    <header v-if="!embedded" class="rd-heading">
      <div><div class="rd-eyebrow">Review delivery workspace</div><h1>审查报告与交付</h1><p>先确认结论与局限，再生成适合不同读者的交付内容。</p></div>
      <div class="rd-top-actions"><a v-if="githubLink && !embedded" class="btn secondary" :href="githubLink" target="_blank" rel="noreferrer"><AppIcon name="external" :size="15" />在 GitHub 查看 PR</a><button type="button" class="btn secondary" :disabled="!currentMarkdown || busy" @click="exportMarkdown"><AppIcon name="download" :size="15" />导出报告</button></div>
    </header>
    <ReportContext v-if="task" :task="task" :plan="plan" :embedded="embedded" :stale="headCheck?.status === 'stale'" />
    <section class="rd-shell" aria-label="审查报告工作台">
      <ReportModeTabs v-model="reportView" :panel-id="panelId" />
      <div class="rd-layout"><section :id="panelId" class="rd-main" role="tabpanel" :aria-label="currentLabel" tabindex="0">
        <div class="rd-view-title"><div><h2>{{ currentLabel }}</h2><p>{{ reportView === 'comment' ? '面向 PR 作者和 Reviewer 的短报告；适合复制为 GitHub 评论或导出 Markdown。' : reportView === 'user' ? '面向开发者的分析报告：先读结论，再追踪代码观察、证据、局限与建议。' : '供排障、复核与历史追溯使用。先读结构化摘要，原始字段按需展开。' }}</p></div><span class="badge" :class="reportView === 'comment' ? 'blue' : reportView === 'user' ? 'success' : 'neutral'">{{ reportView === 'comment' ? '默认交付' : reportView === 'user' ? '证据导向' : '高级内容' }}</span></div>
        <p v-if="feedback" class="rd-notice rd-feedback" role="status">{{ feedback }}</p>
        <div v-if="previewLoading && reportView === 'comment'" class="rd-notice" role="status">正在生成所选内容预览…</div>
        <ReportReadableView v-else-if="reportView === 'user' && plan" :plan="plan" :pr-url="task?.pr_url" @file="emit('file', $event)" @finding="emit('finding', $event)" />
        <AuditSummaryView v-else-if="reportView === 'audit' && plan" :task="task" :plan="plan" :markdown="markdown" @file="emit('file', $event)" />
        <div v-else-if="!currentMarkdown"><EmptyState icon="report" title="当前报告尚不可用" description="请结合任务状态判断，或切换到其他报告查看历史记录。" /><button v-if="reportView === 'comment' && customSelection && reviewId" type="button" class="btn secondary" @click="previewRevision += 1">重试加载所选预览</button></div>
        <div v-else class="rd-doc">
          <div v-if="reportView === 'comment'" class="rd-notice"><AppIcon name="info" :size="17" /><div><strong>这是一份审查摘要，不是合并批准。</strong> 复制或导出前将核对 PR 当前 Head；过期或未知时需要明确确认限制。</div></div>
          <p v-if="selection && reportView === 'comment'" class="rd-context-tip" role="status">包含 {{ selection.included_finding_keys.length }} 项，明确省略 {{ selection.omitted_finding_keys.length }} 项；保留 {{ selection.retained_blocking_keys.length }} 项已处理阻断问题。</p>
          <div :class="{ 'rd-comment': reportView === 'comment' }">
            <header v-if="reportView === 'comment'" class="rd-comment-header"><div><strong>评论正文预览</strong><small>以下是 GitHub PR 评论将呈现的核心内容</small></div><span class="badge neutral">Markdown</span></header>
            <PrCommentPreview v-if="reportView === 'comment'" :content="currentMarkdown" :finding-count="visibleFindingCount" />
            <ReportMarkdown v-else :content="currentMarkdown" />
          </div>
          <button v-if="commentsMarkdown && reportView === 'audit'" type="button" class="rd-text-button" @click="exportComments">导出逐条评论草稿（技术用途，不检查 Head）</button>
        </div>
<button v-if="commentsMarkdown && reportView === 'audit' && plan" type="button" class="rd-text-button" @click="exportComments">导出逐条评论草稿（技术用途，不检查 Head）</button>
      </section><ReportDeliverySidebar :task="task" :plan="plan" v-model:detail="detail" v-model:exclude-resolved="excludeResolved" :mode="reportView" :available="Boolean(currentMarkdown) && !previewLoading" :busy="busy" :copied="copied" :head-label="headCheck || checking ? headLabel : '尚未检查'" :head-tone="headCheck?.status === 'current' ? 'success' : headCheck ? 'warning' : 'neutral'" :head-note="headNote" :can-check="Boolean(reviewId && reviewMarkdown)" :can-publish="Boolean(task)" @copy="copyMarkdown" @download="exportMarkdown" @check="checkHead" @publish="emit('publish')" @full="reportDialog?.showModal()" /></div>
    </section>
    <dialog ref="reportDialog" class="rd-dialog" aria-label="完整 Markdown 报告" @click.self="reportDialog?.close()"><header><strong>{{ currentLabel }} · 完整正文</strong><button type="button" class="icon-button" aria-label="关闭报告" @click="reportDialog?.close()"><AppIcon name="x" :size="16" /></button></header><ReportMarkdown :content="currentMarkdown || ''" /></dialog>
    <dialog ref="confirmationDialog" class="rd-dialog rd-confirm-dialog" aria-labelledby="rd-confirm-title" @cancel="cancelPending" @click.self="cancelPending"><header><strong id="rd-confirm-title">{{ pendingCopy?.status === 'stale' ? 'PR 已有新提交' : '无法确认远端 Head' }}</strong><button type="button" class="icon-button" aria-label="取消交付" @click="cancelPending"><AppIcon name="x" :size="16" /></button></header><div class="rd-dialog-content"><p>当前内容仅适用于审查时的快照。继续交付会保留服务端返回的旧快照或未知状态说明。</p><p class="rd-notice warn">{{ feedback }}</p><code>{{ pendingCopy?.reviewed_head_sha || '未记录 Head' }}</code></div><footer><button type="button" class="btn secondary" @click="cancelPending">取消</button><button type="button" class="btn primary" @click="copyAcknowledged">仍{{ deliveryAction === 'copy' ? '复制' : '导出' }}带限制说明的评论</button></footer></dialog>
  </section>
</template>
