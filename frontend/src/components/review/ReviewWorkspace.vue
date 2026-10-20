<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ReviewTask } from "../../types/review";
import { reviewSections, type ReviewSection } from "../../types/workspace";
import { usePublicationPlan } from "../../composables/usePublicationPlan";
import { useReviewWorkspace } from "../../composables/useReviewWorkspace";
import ReviewOverviewPage from "../pages/ReviewOverviewPage.vue";
import ReviewFindingsPage from "../pages/ReviewFindingsPage.vue";
import ReviewScopePage from "../pages/ReviewScopePage.vue";
import ReviewFilesPage from "../pages/ReviewFilesPage.vue";
import ReviewActivityPage from "../pages/ReviewActivityPage.vue";
import ReviewProgressPage from "../pages/ReviewProgressPage.vue";
import GitHubPublicationPanel from "./GitHubPublicationPanel.vue";
import ReviewViewer from "./ReviewViewer.vue";
import ReportPanel from "../ReportPanel.vue";
import StatusBadge from "../common/StatusBadge.vue";
import EmptyState from "../common/EmptyState.vue";
const props = defineProps<{ task: ReviewTask; report: string | null; section: ReviewSection; statusText: string; duration: string; cancelling: boolean; retryingUnitId: string | null }>();
const emit = defineEmits<{ navigate: [section: ReviewSection]; cancel: []; create: []; rereview: []; retry: [unitId: string] }>();
const { runtime, summary: runtimeSummary, findings, warnings, terminal } = useReviewWorkspace(() => props.task);
const { summary, plan, loading, error, ensurePlan, reload } = usePublicationPlan(() => props.task);
const focusKey = ref("");
const focusedFile = ref('');
const publicationFindings = computed(() => [...(plan.value?.inline_drafts || []), ...(plan.value?.summary_findings || [])]);
const comments = computed(() => publicationFindings.value.map(item => item.body_markdown).join('\n\n---\n\n'));
const legacy = computed(() => props.task.engine !== 'task_review_v1');
watch(() => props.task.id, () => { focusKey.value = ''; focusedFile.value = ''; });
// 完整计划只在需要逐条正文或发布时读取；概览使用轻量摘要。
watch([() => props.section, () => summary.value], () => {
  if (summary.value && ['report', 'findings', 'publish'].includes(props.section)) ensurePlan();
}, { immediate: true });
watch(() => props.section, section => { if (section === 'publish') ensurePlan(); });
function openFinding(key: string) { focusKey.value = key; emit('navigate', 'findings'); }
function openFile(path: string) { focusedFile.value = path; emit('navigate', 'files'); }
const githubLink = computed(() => {
  try { const url = new URL(props.task.pr?.html_url || props.task.pr_url); return url.protocol === 'https:' && url.hostname === 'github.com' ? url.href : null; }
  catch { return null; }
});
</script>
<template>
  <header v-if="terminal || section !== 'overview'" class="pr-title"><div class="min0"><div class="eyebrow">{{ task.pr ? `${task.pr.owner} / ${task.pr.repo} / PR #${task.pr.number}` : `REVIEW / ${task.id.slice(0, 12)}` }}</div><h1>{{ task.pr?.title || '审查任务' }}</h1><div class="meta"><code>{{ task.pr?.base.ref || 'base' }}</code><span class="faint">←</span><code>{{ task.pr?.head.ref || 'head' }}</code><StatusBadge :status="task.status" :label="statusText" /><small>{{ duration }}{{ runtime ? ` · ${runtime.sent_calls} 次已发送模型调用` : '' }}</small></div></div><div class="head-actions"><a v-if="githubLink" class="btn secondary" :href="githubLink" target="_blank" rel="noreferrer">GitHub ↗</a><button type="button" class="btn secondary" :disabled="!terminal" @click="emit('create')">新建审查</button><button type="button" class="btn secondary" :disabled="!terminal" @click="emit('rereview')">重新审查此 PR</button><button type="button" class="btn primary" @click="emit('navigate', 'report')">查看报告</button></div></header>
  <nav v-if="terminal || section !== 'overview'" class="subnav" aria-label="审查结果导航"><button v-for="item in reviewSections" :key="item.id" type="button" :class="{ active: section === item.id, 'tab-action': item.id === 'publish' }" :aria-current="section === item.id ? 'page' : undefined" @click="emit('navigate', item.id)">{{ item.label }}{{ item.id === 'findings' && findings.length ? ` (${findings.length})` : '' }}</button></nav>
  <p v-if="legacy" class="notice warn">此任务来自已退役的历史引擎，仅供查看；不会提供取消或重试。完整历史证据可在技术记录中展开。</p>
  <p v-if="loading" class="muted small space-bottom" role="status">正在读取后端结论…</p><p v-if="error" class="notice warn" role="alert">{{ error }} <button type="button" class="link" @click="reload">重试加载</button></p>
  <ReviewProgressPage v-if="section === 'overview' && !terminal && !legacy" :task="task" :runtime="runtime" :cancelling="cancelling" @cancel="emit('cancel')" @activity="emit('navigate', 'activity')" />
  <ReviewOverviewPage v-else-if="section === 'overview'" :task="task" :report="report" :summary-plan="summary" :summary="runtimeSummary" :findings="findings" :warnings="warnings" :terminal="terminal" @navigate="emit('navigate', $event)" @finding="openFinding" @file="openFile" @rereview="emit('rereview')" />
  <ReviewFindingsPage v-else-if="section === 'findings'" :key="task.id" :task="task" :findings="findings" :focus-key="focusKey" :publication-findings="publicationFindings" @activity="emit('navigate', 'activity')" />
  <ReviewScopePage v-else-if="section === 'scope'" :task="task" :runtime="runtime" :terminal="terminal && !legacy" :retrying-unit-id="retryingUnitId" @retry="emit('retry', $event)" />
  <ReviewFilesPage v-else-if="section === 'files'" :key="task.id" :task="task" :findings="findings" :initial-path="focusedFile" @finding="openFinding" />
  <template v-else-if="section === 'activity'"><ReviewActivityPage :task="task" :runtime="runtime" :duration="duration" /><details v-if="legacy" class="card card-pad space-top"><summary>完整历史审查、证据与契约记录</summary><ReviewViewer :task="task" :report="report" :status-text="statusText" :task-duration="duration" :cancelling="false" :retrying-unit-id="null" @new-review="emit('create')" /></details></template>
  <template v-else-if="section === 'report'"><p v-if="!plan && !error && terminal" class="muted small" role="status">正在生成完整审查报告…</p><ReportPanel v-else :key="task.id" embedded :task="task" :plan="plan" :markdown="report || task.report_markdown" :user-markdown="plan?.user_report_markdown" :review-markdown="plan?.review_comment_markdown" :review-id="task.id" :reviewed-head-sha="plan?.snapshot.head_sha" :comments-markdown="comments" :filename-prefix="`repoguardian-${task.pr?.number || task.id.slice(0, 8)}`" @publish="emit('navigate', 'publish')" @file="openFile" @finding="openFinding" /></template>
  <template v-else-if="section === 'publish'"><div class="page-head"><div><h1>发布到 GitHub</h1><p class="title-sub">选择内容 → 服务端预检 → 预览与二次确认 → 写入与远端对账。</p></div><span class="badge neutral">发布状态以服务端回执为准</span></div><GitHubPublicationPanel v-if="!legacy && terminal && plan?.content_eligible" :key="task.id" :review-id="task.id" :plan="plan" open /><section v-else class="card card-pad"><EmptyState icon="shield" title="当前结果不可发布" :description="plan?.blocking_reasons.join('；') || (legacy ? '历史引擎仅供查看，请新建审查。' : !terminal ? '请等待审查结束。' : '后端尚未提供合格发布计划，请检查计划加载状态。')" /></section></template>
  <footer class="app-footer"><span>只读静态审查 · 证据可追溯 · 结论仅适用于本次快照</span><code>{{ task.pr?.head.sha?.slice(0, 12) || task.id.slice(0, 12) }}</code></footer>
</template>
