<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { getFindings, getPublicationPlan, getReport } from '../../api/client';
import type { ReviewTask } from '../../types/review';
import type { FindingView } from '../../types/reviewPlan';
import type { PublicationPlan } from '../../types/publication';
import { reviewPrimarySections, reviewSecondarySections, type ReviewSection } from '../../types/workspace';
import { executionErrorLabel, phaseLabel, resumeLabel, safeGitHubLink, terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
import ReviewStepper from '../common/ReviewStepper.vue';
import ReviewOverviewPage from '../pages/ReviewOverviewPage.vue';
import ReviewFindingsPage from '../pages/ReviewFindingsPage.vue';
import ReviewScopePage from '../pages/ReviewScopePage.vue';
import ReviewFilesPage from '../pages/ReviewFilesPage.vue';
import ReviewActivityPage from '../pages/ReviewActivityPage.vue';
import ReviewDiscussionsPage from '../pages/ReviewDiscussionsPage.vue';
import ReviewReportPage from '../pages/ReviewReportPage.vue';
const props = defineProps<{ task: ReviewTask; section?: ReviewSection; busy?: boolean }>();
const emit = defineEmits<{ navigate: [value: ReviewSection]; cancel: []; resume: []; rereview: [] }>();
const localSection = ref<ReviewSection>('overview');
const resumeAction = computed(() => resumeLabel(props.task));
const tab = computed({ get: () => props.section ?? localSection.value, set: value => { localSection.value = value; emit('navigate',value); } });
const plan = ref<PublicationPlan | null>(null), findings = ref<FindingView[]>([]), report = ref('');
const error = ref(''), loading = ref(false), findingFilter = ref('all'), focusKey = ref(''), focusedFile = ref('');
let controller: AbortController | null = null;
let generation = 0;
async function load() {
  const id = props.task.id, owner = ++generation;
  controller?.abort(); const request = new AbortController(); controller = request;
  loading.value = true; error.value = '';
  try {
    const [nextPlan,nextFindings,nextReport] = await Promise.all([
      getPublicationPlan(id,request.signal), getFindings(id,request.signal), getReport(id,request.signal).catch(() => ''),
    ]);
    if (owner !== generation) return;
    if (nextPlan.review_id !== id || nextPlan.snapshot.head_sha !== props.task.snapshot.head_sha
      || nextPlan.snapshot.effective_base_sha !== props.task.snapshot.effective_base_sha) {
      throw new Error('报告身份或固定快照与当前审查不一致');
    }
    plan.value = nextPlan; findings.value = nextFindings.items; report.value = nextReport;
  } catch (cause) { if (owner === generation && !request.signal.aborted) error.value = cause instanceof Error ? cause.message : '结果读取失败'; }
  finally { if (owner === generation) loading.value = false; }
}
watch(() => props.task.id, () => {
  generation++; controller?.abort(); plan.value = null; findings.value = []; report.value = '';
  error.value = ''; findingFilter.value = 'all'; focusKey.value = ''; focusedFile.value = ''; localSection.value = 'overview';
});
watch(() => [props.task.id,props.task.status,props.task.updated_at], () => {
  if (terminal(props.task.status)) void load();
  else {
    generation++; controller?.abort(); loading.value = false;
    plan.value = null; findings.value = []; report.value = '';
  }
}, { immediate: true });
function openFinding(id: string) { focusKey.value = id; findingFilter.value = 'all'; tab.value = 'findings'; }
function filterFindings(status: string) { focusKey.value = ''; findingFilter.value = status; tab.value = 'findings'; }
function openFile(path: string) { focusedFile.value = path; tab.value = 'files'; }
function updated(items: FindingView[]) { findings.value = items; void load(); }
function exportReport() {
  const content = report.value || plan.value?.user_report_markdown;
  if (!content) return;
  const url = URL.createObjectURL(new Blob([content],{type:'text/markdown;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url;
  link.download = `RepoGuardian-${props.task.id}-${props.task.snapshot.head_sha.slice(0,8)}.md`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url),0);
}
onBeforeUnmount(() => { generation++; controller?.abort(); });
</script>
<template>
  <ReviewStepper :current="terminal(task.status) ? 3 : 2" />
  <header class="pr-title"><div class="min0"><div class="eyebrow">{{ task.pr.owner }} / {{ task.pr.repo }} / PR #{{ task.pr.number }}</div><h1>{{ task.pr.title }}</h1><div class="meta"><StatusBadge :status="task.status" /><span class="muted small">{{ phaseLabel(task.phase) }}</span><code>{{ task.snapshot.effective_base_sha.slice(0,8) }} ← {{ task.snapshot.head_sha.slice(0,8) }}</code></div></div>
    <div class="head-actions"><a v-if="safeGitHubLink(task.pr.html_url)" class="btn secondary" :href="safeGitHubLink(task.pr.html_url)" target="_blank" rel="noreferrer">GitHub <AppIcon name="external" :size="14" /></a><button v-if="!terminal(task.status)" class="btn secondary" :disabled="busy" @click="$emit('cancel')">停止审查</button><button v-if="resumeAction" class="btn secondary" :disabled="busy" @click="$emit('resume')">{{ resumeAction }}</button><button class="btn secondary" :disabled="!terminal(task.status) || busy" @click="$emit('rereview')">重新审查此 PR</button></div>
  </header>
  <nav class="subnav" aria-label="审查结果导航"><button v-for="item in reviewPrimarySections" :key="item.id" :class="{ active: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id">{{ item.label }}{{ item.id === 'findings' && findings.length ? ` (${findings.length})` : '' }}</button><span class="tab-spacer" /><div class="tab-group" role="group" aria-label="补充证据"><button v-for="item in reviewSecondarySections" :key="item.id" :class="{ active: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id">{{ item.label }}</button></div></nav>
  <p v-if="error" class="notice warn space-bottom" role="alert">{{ error }} <button class="link" @click="load">重试读取结果</button></p>
  <p v-if="task.error" class="notice warn space-bottom">{{ executionErrorLabel(task.error) }}。错误类别：<code>{{ task.error }}</code>；缺失与未知不能视为通过。</p>
  <p v-if="resumeAction === '补查失败分支'" class="muted small space-bottom">补查只重跑失败分支，沿用本轮固定快照、剩余调用预算和总期限；最多一次。</p>
  <ReviewOverviewPage v-if="tab === 'overview'" :task="task" :plan="plan" :loading="loading" @navigate="tab = $event" @filter="filterFindings" />
  <ReviewFindingsPage v-else-if="tab === 'findings'" :key="task.id" :task="task" :findings="findings" :limitations="plan?.outcome.review_limitations || []" :focus-key="focusKey" :initial-filter="findingFilter" :loading="loading" @updated="updated" @file="openFile" />
  <ReviewScopePage v-else-if="tab === 'scope'" :task="task" :plan="plan" />
  <ReviewFilesPage v-else-if="tab === 'files'" :task="task" :findings="findings" :initial-path="focusedFile" @finding="openFinding" />
  <ReviewActivityPage v-else-if="tab === 'activity'" :task="task" />
  <ReviewDiscussionsPage v-else-if="tab === 'discussions'" :task="task" @file="openFile" />
  <ReviewReportPage v-else-if="tab === 'report'" :task="task" :plan="plan" :report="report" :loading="loading" @export="exportReport" />
  <footer class="app-footer"><span>只读静态审查 · 未执行目标代码、测试或构建 · 结论仅适用于本次快照</span><code>{{ task.snapshot.head_sha.slice(0,12) }}</code></footer>
</template>
