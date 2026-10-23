<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, provide, ref, watch } from "vue";
import type { ReviewTask } from "../../types/review";
import { reviewPrimarySections, reviewPublishSection, reviewSecondarySections, type ReviewSection } from "../../types/workspace";
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
import DiscussionWorkbench from "./discussion/DiscussionWorkbench.vue";
import { discussionWorkspaceKey, usePrDiscussions } from "../../composables/usePrDiscussions";
import { situationActionTarget, type FindingEntryFilter, type SituationActionKey } from "../../utils/reviewSituation";
import type { FindingSource } from "../../utils/findingWorkbench";
const props = defineProps<{ task: ReviewTask; report: string | null; section: ReviewSection; statusText: string; duration: string; cancelling: boolean; retryingUnitId: string | null }>();
const emit = defineEmits<{ navigate: [section: ReviewSection]; cancel: []; create: []; rereview: []; retry: [unitId: string] }>();
const { runtime, summary: runtimeSummary, findings, warnings, terminal } = useReviewWorkspace(() => props.task);
const { summary, plan, loading, detailLoading, error, ensurePlan, reload } = usePublicationPlan(() => props.task);
const focusKey = ref("");
const focusedFile = ref('');
const findingFilter = ref<FindingEntryFilter>("all");
const findingSource = ref<FindingSource>("all");
const scopeFilter = ref<"all" | "incomplete">("all");
const investigationsOpen = ref(false);
const activityFocus = ref<"limits" | "validation" | "error">();
function runSituationAction(key: Exclude<SituationActionKey, "rereview">) {
  const target = situationActionTarget(key);
  focusKey.value = "";
  findingFilter.value = target.findingFilter || "all";
  findingSource.value = "all";
  scopeFilter.value = target.scopeFilter || "all";
  investigationsOpen.value = !!target.investigations;
  activityFocus.value = target.activityFocus;
  emit("navigate", target.section);
}
// 概览指标按来源拆分：数量与发现页筛选一一对应，保证每个数字都能展开成完整条目。
function openFindingFilter(filter: FindingEntryFilter, source: FindingSource) {
  focusKey.value = "";
  findingFilter.value = filter;
  findingSource.value = source;
  emit("navigate", "findings");
}
const publicationFindings = computed(() => [...(plan.value?.inline_drafts || []), ...(plan.value?.summary_findings || [])]);
const comments = computed(() => publicationFindings.value.map(item => item.body_markdown).join('\n\n---\n\n'));
const legacy = computed(() => props.task.engine !== 'task_review_v1');
// 同一审查的旧快照事实跨页面保留；只有明确检查为 current 才能清除。
const stale = ref(false);
watch([() => props.task.id, () => props.task.snapshot?.head_sha], () => { stale.value = false; });
function updateSnapshot(status: "current" | "stale" | "unknown") {
  if (status !== "unknown") stale.value = status === "stale";
}
watch(() => props.task.id, () => {
  focusKey.value = ''; focusedFile.value = ''; findingFilter.value = "all"; findingSource.value = "all";
  scopeFilter.value = "all"; investigationsOpen.value = false; activityFocus.value = undefined;
});
// 完整计划只在需要逐条正文或发布时读取；概览使用轻量摘要。
watch([() => props.section, () => summary.value], () => {
  if (summary.value && ['report', 'findings', 'publish'].includes(props.section)) ensurePlan();
}, { immediate: true });
watch(() => props.section, section => { if (section === 'publish') ensurePlan(); });
function openFinding(key: string) { focusKey.value = key; findingFilter.value = "all"; findingSource.value = "all"; emit('navigate', 'findings'); }
function openFile(path: string) { focusedFile.value = path; emit('navigate', 'files'); }
const discussionOpen = ref(false);
const discussions = usePrDiscussions(() => props.task.id);
let discussionTrigger: HTMLElement | null = null;
provide(discussionWorkspaceKey, { store: discussions, open: () => {
  discussionTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  discussionOpen.value = true;
} });
async function closeDiscussions() {
  discussionOpen.value = false;
  await nextTick();
  if (discussionTrigger?.isConnected) discussionTrigger.focus();
  else document.querySelector<HTMLButtonElement>(".pd-summary .pd-btn.primary")?.focus();
}
watch([() => props.section, () => props.task.id], () => { discussionOpen.value = false; discussions.cancel(); });
onBeforeUnmount(discussions.cancel);
const githubLink = computed(() => {
  try { const url = new URL(props.task.pr?.html_url || props.task.pr_url); return url.protocol === 'https:' && url.hostname === 'github.com' ? url.href : null; }
  catch { return null; }
});
</script>
<template>
  <header v-if="terminal || section !== 'overview'" class="pr-title"><div class="min0"><div class="eyebrow">{{ task.pr ? `${task.pr.owner} / ${task.pr.repo} / PR #${task.pr.number}` : `审查 / ${task.id.slice(0, 12)}` }}</div><h1>{{ task.pr?.title || '审查任务' }}</h1><div class="meta"><code>{{ task.pr?.base.ref || 'base' }}</code><span class="faint">←</span><code>{{ task.pr?.head.ref || 'head' }}</code><StatusBadge :status="task.status" :label="statusText" /><small>{{ duration }}{{ runtime ? ` · ${runtime.sent_calls} 次已发送模型调用` : '' }}</small></div></div><div class="head-actions"><a v-if="githubLink" class="btn secondary" :href="githubLink" target="_blank" rel="noreferrer">GitHub ↗</a><button type="button" class="btn secondary" :disabled="!terminal" @click="emit('create')">新建审查</button><button type="button" class="btn secondary" :disabled="!terminal" @click="emit('rereview')">重新审查此 PR</button><button type="button" class="btn" :class="section === 'overview' ? 'secondary' : 'primary'" @click="emit('navigate', 'report')">查看报告</button></div></header>
  <nav v-if="terminal || section !== 'overview'" class="subnav" aria-label="审查结果导航"><button v-for="item in reviewPrimarySections" :key="item.id" type="button" :class="{ active: section === item.id }" :aria-current="section === item.id ? 'page' : undefined" @click="emit('navigate', item.id)">{{ item.label }}{{ item.id === 'findings' && findings.length ? ` (${findings.length})` : '' }}</button><span class="tab-spacer" aria-hidden="true" /><div class="tab-group" role="group" aria-label="补充证据"><button v-for="item in reviewSecondarySections" :key="item.id" type="button" :class="{ active: section === item.id }" :aria-current="section === item.id ? 'page' : undefined" @click="emit('navigate', item.id)">{{ item.label }}</button></div><button type="button" class="tab-action" :class="{ active: section === reviewPublishSection.id }" :aria-current="section === reviewPublishSection.id ? 'page' : undefined" @click="emit('navigate', reviewPublishSection.id)">{{ reviewPublishSection.label }}</button></nav>
  <p v-if="legacy" class="notice warn">此任务来自已退役的历史引擎，仅供查看；不会提供取消或重试。完整历史证据可在技术记录中展开。</p>
  <p v-if="loading" class="muted small space-bottom" role="status">正在读取审查结论…</p><p v-if="error" class="notice warn" role="alert">{{ error }} <button type="button" class="link" @click="reload">{{ summary ? '重试加载报告' : '重试加载摘要' }}</button></p>
  <DiscussionWorkbench v-if="discussionOpen" :store="discussions" :initial-stats="summary?.digest.discussion_stats" :pr-url="task.pr?.html_url || task.pr_url" :head-sha="task.snapshot?.head_sha || task.pr?.head.sha" :back-label="section === 'report' ? '返回审查报告' : '返回结果概览'" can-open-file @back="closeDiscussions" @file="discussionOpen = false; openFile($event)" />
  <ReviewProgressPage v-if="section === 'overview' && !terminal && !legacy" :task="task" :runtime="runtime" :cancelling="cancelling" @cancel="emit('cancel')" @activity="emit('navigate', 'activity')" />
  <ReviewOverviewPage v-else-if="section === 'overview'" v-show="!discussionOpen" :task="task" :report="report" :summary-plan="summary" :summary="runtimeSummary" :findings="findings" :warnings="warnings" :terminal="terminal" :stale="stale" :summary-error="!summary && !!error" :summary-loading="loading" @reload="reload" @action="runSituationAction" @snapshot="updateSnapshot" @navigate="emit('navigate', $event)" @finding="openFinding" @file="openFile" @rereview="emit('rereview')" @filter="openFindingFilter" />
  <ReviewFindingsPage v-else-if="section === 'findings'" :key="task.id" :task="task" :findings="findings" :focus-key="focusKey" :initial-filter="findingFilter" :initial-source="findingSource" :summary="summary" :blocking-keys="summary?.digest.merge_decision?.blocking_finding_keys" :suggestion-keys="summary?.digest.merge_decision?.nonblocking_finding_keys" :publication-findings="publicationFindings" @activity="emit('navigate', 'activity')" />
  <ReviewScopePage v-else-if="section === 'scope'" :task="task" :runtime="runtime" :initial-filter="scopeFilter" :investigations-open="investigationsOpen" :terminal="terminal && !legacy" :retrying-unit-id="retryingUnitId" @retry="emit('retry', $event)" />
  <ReviewFilesPage v-else-if="section === 'files'" :key="task.id" :task="task" :findings="findings" :initial-path="focusedFile" @finding="openFinding" />
  <template v-else-if="section === 'activity'"><ReviewActivityPage :task="task" :runtime="runtime" :duration="duration" :summary="summary" :initial-focus="activityFocus" /><details v-if="legacy" class="card card-pad space-top"><summary>完整历史审查、证据与契约记录</summary><ReviewViewer :task="task" :report="report" :status-text="statusText" :task-duration="duration" :cancelling="false" :retrying-unit-id="null" @new-review="emit('create')" /></details></template>
  <template v-else-if="section === 'report'"><p v-if="!plan && !error && terminal" class="muted small" role="status">正在生成完整审查报告…</p><div v-else v-show="!discussionOpen" style="display: contents"><ReportPanel :key="task.id" embedded :task="task" :plan="plan" :refreshing="detailLoading" :markdown="report || task.report_markdown" :user-markdown="plan?.user_report_markdown" :review-markdown="plan?.review_comment_markdown" :review-id="task.id" :reviewed-head-sha="plan?.snapshot.head_sha" :comments-markdown="comments" :filename-prefix="`repoguardian-${task.pr?.number || task.id.slice(0, 8)}`" :stale="stale" @snapshot="updateSnapshot" @rereview="emit('rereview')" @publish="emit('navigate', 'publish')" @file="openFile" @finding="openFinding" /></div></template>
  <template v-else-if="section === 'publish'"><div class="page-head"><div><h1>发布或复制到 GitHub</h1><p class="title-sub">复制评论：只写入剪贴板；直接发布：需要 GitHub 授权、服务端预检与二次确认。</p></div><span class="badge neutral">发布状态以服务端回执为准</span></div><GitHubPublicationPanel v-if="!legacy && terminal && plan?.content_eligible" :key="task.id" :review-id="task.id" :plan="plan" open /><section v-else class="card card-pad"><EmptyState icon="shield" title="当前结果不可发布" :description="plan?.blocking_reasons.join('；') || (legacy ? '历史引擎仅供查看，请新建审查。' : !terminal ? '请等待审查结束。' : '后端尚未提供合格发布计划，请检查计划加载状态。')" /></section></template>
  <footer class="app-footer"><span>只读静态审查 · 证据可追溯 · 结论仅适用于本次快照</span><code>{{ task.pr?.head.sha?.slice(0, 12) || task.id.slice(0, 12) }}</code></footer>
</template>
