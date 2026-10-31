<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { getFindings, getPublicationPlan, getReport, getUnits } from '../../api/client';
import type { Candidate, Finding, FindingDispositions, Report, Run, WorkItem } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import { reviewPrimarySections, type ReviewSection } from '../../types/workspace';
import { executionErrorLabel, phaseLabel, safeGitHubLink, terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
import ReviewActivityPage from '../pages/ReviewActivityPage.vue';
import ReviewFilesPage from '../pages/ReviewFilesPage.vue';
import ReviewFindingsPage from '../pages/ReviewFindingsPage.vue';
import ReviewOverviewPage from '../pages/ReviewOverviewPage.vue';
import ReviewReportPage from '../pages/ReviewReportPage.vue';

const props = defineProps<{ task: Run; section?: ReviewSection; busy?: boolean }>();
const emit = defineEmits<{ navigate: [value: ReviewSection]; cancel: []; retry: []; overviewRetry: []; rereview: [] }>();

const localSection = ref<ReviewSection>('overview');
const tab = computed({
  get: () => props.section ?? localSection.value,
  set: value => {
    localSection.value = value;
    emit('navigate', value);
  },
});
const plan = ref<PublicationPlan | null>(null);
const findings = ref<Finding[]>([]);
const candidates = ref<Candidate[]>([]);
const dispositions = ref<FindingDispositions>({});
const units = ref<WorkItem[]>([]);
const report = ref<Report | null>(null);
const markdown = ref('');
const error = ref('');
const loading = ref(false);
let controller: AbortController | null = null;
let generation = 0;

async function load() {
  const id = props.task.id;
  const owner = ++generation;
  controller?.abort();
  const request = new AbortController();
  controller = request;
  loading.value = true;
  error.value = '';

  try {
    const [nextFindings, nextReport, nextUnits, nextPlan] = await Promise.all([
      getFindings(id, request.signal),
      getReport(id, request.signal),
      getUnits(id, request.signal),
      getPublicationPlan(id, request.signal).catch(() => null),
    ]);
    if (owner !== generation) return;

    findings.value = nextFindings.findings;
    candidates.value = nextFindings.candidates;
    dispositions.value = nextFindings.dispositions;
    report.value = nextReport.report;
    markdown.value = nextReport.markdown;
    units.value = nextUnits;
    plan.value = nextPlan;
  } catch (cause) {
    if (owner === generation && !request.signal.aborted) {
      error.value = cause instanceof Error ? cause.message : '结果读取失败';
    }
  } finally {
    if (owner === generation) loading.value = false;
  }
}

watch(() => [props.task.id, props.task.updated_at], () => { void load(); }, { immediate: true });
onBeforeUnmount(() => {
  generation += 1;
  controller?.abort();
});

function exportReport() {
  if (!markdown.value) return;

  const url = URL.createObjectURL(new Blob([markdown.value], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'RepoGuardian-' + props.task.id + '-' + props.task.snapshot.head_sha.slice(0, 8) + '.md';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
</script>

<template>
  <header class="pr-title">
    <div class="min0">
      <div class="eyebrow">{{ task.pr.owner }} / {{ task.pr.repo }} / PR #{{ task.pr.number }}</div>
      <h1>{{ task.pr.title }}</h1>
      <div class="meta">
        <StatusBadge :status="task.status" />
        <span class="muted small">{{ phaseLabel(task.phase) }}</span>
        <code>{{ task.snapshot.base_sha.slice(0, 8) }} → {{ task.snapshot.head_sha.slice(0, 8) }}</code>
      </div>
    </div>
    <div class="head-actions">
      <a v-if="safeGitHubLink(task.pr.html_url)" class="btn secondary" :href="safeGitHubLink(task.pr.html_url)" target="_blank" rel="noreferrer">
        GitHub <AppIcon name="external" :size="14" />
      </a>
      <button v-if="!terminal(task.status)" class="btn secondary" :disabled="busy" @click="$emit('cancel')">停止审查</button>
      <button v-if="task.status === 'failed' || task.status === 'partial'" class="btn secondary" :disabled="busy" @click="$emit('retry')">重试审查</button>
      <button class="btn secondary" :disabled="busy" @click="$emit('rereview')">重新审查此 PR</button>
    </div>
  </header>

  <nav class="subnav" aria-label="审查结果导航">
    <button v-for="item in reviewPrimarySections" :key="item.id" :class="{ active: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id">
      {{ item.label }}{{ item.id === 'findings' && findings.length ? ' (' + findings.length + ')' : '' }}
    </button>
  </nav>

  <p v-if="error" class="notice warn space-bottom" role="alert">
    {{ error }} <button class="link" @click="load">重试读取结果</button>
  </p>
  <p v-if="task.error_code" class="notice warn space-bottom">
    {{ executionErrorLabel(task.error_code) }}。错误类别：<code>{{ task.error_code }}</code>；缺失与未知不能视为通过。
  </p>

  <ReviewOverviewPage v-if="tab === 'overview'" :task="task" :report="report" :findings="findings" :loading="loading" @navigate="tab = $event" @retry-overview="$emit('overviewRetry')" />
  <ReviewFilesPage v-else-if="tab === 'changes'" :task="task" :findings="findings" @finding="tab = 'findings'" />
  <ReviewFindingsPage v-else-if="tab === 'findings'" :task="task" :findings="findings" :candidates="candidates" :dispositions="dispositions" :loading="loading" @update:dispositions="dispositions = $event" />
  <ReviewActivityPage v-else-if="tab === 'activity'" :task="task" :units="units" :report="report" />
  <ReviewReportPage v-else :task="task" :plan="plan" :report="markdown" :loading="loading" @export="exportReport" />

  <footer v-if="tab !== 'publish'" class="app-footer">
    <span>只读静态审查 · 未执行目标代码、测试或构建 · 结论仅适用于本次快照</span>
    <code>{{ task.snapshot.head_sha.slice(0, 12) }}</code>
  </footer>
</template>
