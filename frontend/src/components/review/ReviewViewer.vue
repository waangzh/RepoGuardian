<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type {
  ChangedFile,
  DiffLine,
  EvidenceAnchor,
  ReviewFileStatus,
  ReviewIssue,
  ReviewTask,
  ReviewUnit,
  ReviewUnitResult,
} from "../../types/review";
import AppIcon from "../common/AppIcon.vue";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";
import ReportPanel from "../ReportPanel.vue";
import ExecutionTimeline from "./ExecutionTimeline.vue";

type ReviewTab = "overview" | "review" | "files" | "activity";
type SeverityFilter = "all" | ReviewIssue["severity"];
type StatusFilter = "all" | "confirmed" | "needs_human";

interface CodeRow {
  key: string;
  oldLine: number | null;
  newLine: number | null;
  content: string;
  kind: DiffLine["kind"] | "evidence";
  highlighted: boolean;
}

const props = defineProps<{
  task: ReviewTask;
  report: string | null;
  statusText: string;
  taskDuration: string;
  cancelling: boolean;
  retryingUnitId: string | null;
}>();

const emit = defineEmits<{
  cancel: [];
  newReview: [];
  retry: [unitId: string];
}>();

const activeTab = ref<ReviewTab>("overview");
const severityFilter = ref<SeverityFilter>("all");
const statusFilter = ref<StatusFilter>("all");
const categoryFilter = ref("all");
const fileStatusFilter = ref("all");
const fileGroupFilter = ref("all");
const selectedUnitId = ref(props.task.review_units[0]?.id || "");
const selectedIssueId = ref(props.task.issues[0]?.id || "");

watch(() => props.task.id, () => {
  activeTab.value = "overview";
  selectedUnitId.value = props.task.review_units[0]?.id || "";
  selectedIssueId.value = props.task.issues[0]?.id || "";
});

const terminal = computed(() => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(props.task.status));
const coverage = computed(() => props.task.coverage || {
  changed_files: props.task.changed_files.length,
  eligible_files: props.task.changed_files.length,
  reviewed_files: 0,
  partial_files: 0,
  skipped_files: 0,
  failed_files: 0,
  coverage_rate: 0,
  completed_units: 0,
  total_units: props.task.review_units.length,
  unit_coverage_rate: 0,
  files: [],
  units: [],
});
const fileCoveragePercent = computed(() => Math.round(coverage.value.coverage_rate * 1000) / 10);
const unitCoveragePercent = computed(() => Math.round(coverage.value.unit_coverage_rate * 1000) / 10);
const confirmedIssues = computed(() => props.task.issues.filter((issue) => issue.status === "confirmed"));
const confirmedIssueCount = computed(() => confirmedIssues.value.length);
const needsHumanIssueCount = computed(() => props.task.issues.filter((issue) => issue.status === "needs_human").length);
const highPriorityCount = computed(() => confirmedIssues.value.filter((issue) => ["critical", "high"].includes(issue.severity)).length);
const resolvedEvidenceCount = computed(() => props.task.issues.filter((issue) => issue.primary_evidence.resolution_status !== "unresolved").length);
const severityCounts = computed(() => ({
  critical: props.task.issues.filter((issue) => issue.severity === "critical").length,
  high: props.task.issues.filter((issue) => issue.severity === "high").length,
  medium: props.task.issues.filter((issue) => issue.severity === "medium").length,
  low: props.task.issues.filter((issue) => issue.severity === "low").length,
}));
const confirmedSeverityCounts = computed(() => ({
  medium: confirmedIssues.value.filter((issue) => issue.severity === "medium").length,
  low: confirmedIssues.value.filter((issue) => issue.severity === "low").length,
}));
const categories = computed(() => Array.from(new Set(props.task.issues.map((issue) => issue.category))).sort());

const latestValidation = computed(() => {
  const projectCI = [...(props.task.validation || [])].reverse().find((item) => item.backend === "project_ci");
  return projectCI || props.task.validation?.at(-1) || null;
});
const ciPresentation = computed(() => {
  const validation = latestValidation.value;
  if (validation) {
    const labels: Record<string, string> = {
      passed: "已通过",
      failed: "未通过",
      timed_out: "已超时",
      cancelled: "已取消",
      infrastructure_error: "基础设施异常",
      unsupported: "不支持",
      inconclusive: "结论不明确",
    };
    return { value: labels[validation.status] || validation.status, detail: validation.backend === "project_ci" ? "Project CI" : validation.backend, status: validation.status };
  }
  if (props.task.validation_backend === "project_ci") return { value: terminal.value ? "无结果" : "运行中", detail: "Project CI", status: terminal.value ? "inconclusive" : "running" };
  return { value: "未配置", detail: "严格只读审查", status: "pending" };
});

const purposeSummary = computed(() => {
  const body = props.task.pr?.body?.trim();
  if (body) return body.replace(/[#*_>`\[\]]/g, "").split(/\n\s*\n/)[0].slice(0, 520);
  const unitSummaries = Array.from(new Set(
    props.task.review_unit_results
      .map((item) => item.plan?.change_summary?.trim())
      .filter((item): item is string => Boolean(item)),
  ));
  if (unitSummaries.length) return unitSummaries.join("；").slice(0, 520);
  return `本次 Pull Request 修改 ${props.task.changed_files.length} 个文件，并被划分为 ${props.task.review_units.length} 个语义相关的变更组进行证据化审查。`;
});

function unitResult(unitId: string): ReviewUnitResult | undefined {
  return props.task.review_unit_results.find((item) => item.review_unit_id === unitId);
}

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

function unitTitle(unit: ReviewUnit): string {
  const symbol = unit.changed_symbols.find(Boolean);
  if (symbol) return symbol.length > 42 ? `${symbol.slice(0, 39)}…` : symbol;
  const name = basename(unit.primary_files[0] || unit.id).replace(/\.[^.]+$/, "");
  return name.replace(/[-_]+/g, " ");
}

function unitFiles(unit: ReviewUnit): string[] {
  return Array.from(new Set([...unit.primary_files, ...unit.related_files]));
}

function issueCountForUnit(unitId: string): number {
  return props.task.issues.filter((issue) => issue.review_unit_id === unitId || issue.source_review_unit_ids.includes(unitId)).length;
}

const selectedUnit = computed(() => props.task.review_units.find((unit) => unit.id === selectedUnitId.value) || props.task.review_units[0] || null);
const selectedUnitResult = computed(() => selectedUnit.value ? unitResult(selectedUnit.value.id) : undefined);

const filteredIssues = computed(() => {
  const issues = [...props.task.issues].filter((issue) => {
    if (selectedUnitId.value && issue.review_unit_id !== selectedUnitId.value && !issue.source_review_unit_ids.includes(selectedUnitId.value)) return false;
    if (severityFilter.value === "high" && !["critical", "high"].includes(issue.severity)) return false;
    if (severityFilter.value !== "all" && severityFilter.value !== "high" && issue.severity !== severityFilter.value) return false;
    if (statusFilter.value !== "all" && issue.status !== statusFilter.value) return false;
    if (categoryFilter.value !== "all" && issue.category !== categoryFilter.value) return false;
    return true;
  });
  const order: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
  return issues.sort((left, right) => order[left.severity] - order[right.severity] || right.confidence - left.confidence);
});

watch(filteredIssues, (issues) => {
  if (!issues.some((issue) => issue.id === selectedIssueId.value)) selectedIssueId.value = issues[0]?.id || "";
}, { immediate: true });

const selectedIssue = computed(() => filteredIssues.value.find((issue) => issue.id === selectedIssueId.value) || filteredIssues.value[0] || null);
const selectedChangedFile = computed<ChangedFile | null>(() => {
  const path = selectedIssue.value?.primary_evidence.file_path || selectedUnit.value?.primary_files[0];
  return props.task.changed_files.find((file) => file.file_path === path || file.old_file_path === path) || null;
});

function rowsFromEvidence(evidence: EvidenceAnchor): CodeRow[] {
  const target = evidence.existing_code.split("\n");
  const start = evidence.resolved_start_line || 1;
  const beforeStart = Math.max(1, start - evidence.context_before.length);
  const useBase = evidence.resolved_side === "base" || (!evidence.resolved_side && evidence.expected_side === "base");
  const row = (key: string, line: number, content: string, highlighted: boolean): CodeRow => ({
    key,
    oldLine: useBase ? line : null,
    newLine: useBase ? null : line,
    content,
    kind: "evidence",
    highlighted,
  });
  return [
    ...evidence.context_before.map((content, index) => row(`b-${index}`, beforeStart + index, content, false)),
    ...target.map((content, index) => row(`t-${index}`, start + index, content, true)),
    ...evidence.context_after.map((content, index) => row(`a-${index}`, start + target.length + index, content, false)),
  ];
}

const codeRows = computed<CodeRow[]>(() => {
  const issue = selectedIssue.value;
  const file = selectedChangedFile.value;
  if (!issue) return [];
  if (file) {
    const evidence = issue.primary_evidence;
    const line = evidence.resolved_start_line;
    const useBase = evidence.resolved_side === "base" || (!evidence.resolved_side && evidence.expected_side === "base");
    const hunkId = issue.resolved_location?.hunk_id || evidence.expected_hunk_id;
    const hunk = file.hunks.find((item) => hunkId && item.hunk_id === hunkId)
      || file.hunks.find((item) => {
        const start = useBase ? item.old_start : item.new_start;
        const length = useBase ? item.old_length : item.new_length;
        return Boolean(line && line >= start && line < start + Math.max(length, 1));
      })
      || file.hunks[0];
    if (hunk?.lines.length) {
      return hunk.lines.map((item, index) => ({
        key: `${hunk.hunk_id}-${index}`,
        oldLine: item.old_line_no ?? null,
        newLine: item.new_line_no ?? null,
        content: item.content,
        kind: item.kind,
        highlighted: (() => {
          const itemLine = useBase ? item.old_line_no : item.new_line_no;
          return Boolean(line && itemLine && itemLine >= line && itemLine <= (evidence.resolved_end_line || line));
        })(),
      }));
    }
  }
  return rowsFromEvidence(issue.primary_evidence);
});

function chooseUnit(unitId: string) {
  selectedUnitId.value = unitId;
  const issue = props.task.issues.find((item) => item.review_unit_id === unitId || item.source_review_unit_ids.includes(unitId));
  selectedIssueId.value = issue?.id || "";
}

function openIssue(issue: ReviewIssue) {
  activeTab.value = "review";
  selectedUnitId.value = issue.review_unit_id;
  selectedIssueId.value = issue.id;
}

function openUnit(unitId: string) {
  activeTab.value = "review";
  chooseUnit(unitId);
}

function unitNames(unitIds: string[]): string {
  return unitIds
    .map((id) => props.task.review_units.find((unit) => unit.id === id))
    .filter((unit): unit is ReviewUnit => Boolean(unit))
    .map(unitTitle)
    .join("、") || "—";
}

function openRawReport() {
  document.getElementById("raw-review-report")?.setAttribute("open", "");
  document.getElementById("raw-review-report")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function severityLabel(severity: ReviewIssue["severity"]): string {
  return ({ critical: "严重", high: "高", medium: "中", low: "低" })[severity];
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    reviewed: "已审查",
    unknown: "未知",
    partial: "部分完成",
    pending: "等待中",
    excluded_binary: "已排除 · 二进制",
    excluded_generated: "已排除 · 生成文件",
    excluded_sensitive: "已排除 · 敏感文件",
    unsupported: "不支持",
    timed_out: "已超时",
    model_failed: "模型失败",
    budget_exhausted: "预算耗尽",
  };
  return labels[status] || status;
}

function fileStatusTone(status: ReviewFileStatus): string {
  if (status === "reviewed") return "passed";
  if (["partial", "budget_exhausted", "timed_out"].includes(status)) return "warning";
  if (["model_failed", "unsupported"].includes(status)) return "failed";
  return "pending";
}

const fileRows = computed(() => {
  const coverageMap = new Map(coverage.value.files.map((item) => [item.file_path, item]));
  return props.task.changed_files.map((file) => {
    const item = coverageMap.get(file.file_path);
    return {
      ...file,
      eligible: item?.eligible ?? true,
      status: item?.status || (terminal.value ? "unknown" : "pending") as ReviewFileStatus,
      reviewUnitIds: item?.review_unit_ids || props.task.review_units.filter((unit) => unitFiles(unit).includes(file.file_path)).map((unit) => unit.id),
      reason: item?.reason,
    };
  }).filter((file) => {
    if (fileStatusFilter.value !== "all" && file.status !== fileStatusFilter.value) return false;
    if (fileGroupFilter.value !== "all" && !file.reviewUnitIds.includes(fileGroupFilter.value)) return false;
    return true;
  });
});

const attentionIssues = computed(() => [...props.task.issues]
  .filter((issue) => issue.status !== "dismissed")
  .sort((left, right) => ({ critical: 0, high: 1, medium: 2, low: 3 })[left.severity] - ({ critical: 0, high: 1, medium: 2, low: 3 })[right.severity])
  .slice(0, 4));

function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function formatTokens(tokens: number): string {
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(tokens >= 10000 ? 1 : 2)}k` : String(tokens);
}

const maxOperationCalls = computed(() => Math.max(1, ...props.task.model_usage_summary.by_operation.map((item) => item.stats.calls)));
</script>

<template>
  <main class="review-viewer">
    <header class="review-hero">
      <div class="review-hero__crumbs">{{ task.pr?.owner || "Repository" }} / {{ task.pr?.repo || "等待仓库" }} / PR #{{ task.pr?.number || "—" }}</div>
      <div class="review-hero__row">
        <div class="review-hero__identity">
          <h1>{{ task.pr?.title || "正在读取 Pull Request" }}</h1>
          <div class="review-hero__meta">
            <code>{{ task.pr?.base.ref || "base" }}</code><span>←</span><code>{{ task.pr?.head.ref || "head" }}</code>
            <StatusBadge :status="task.status" :label="statusText" />
            <span v-if="taskDuration">{{ taskDuration }}</span><span>{{ task.model || "默认模型" }}</span>
          </div>
        </div>
        <div class="review-hero__actions">
          <a v-if="task.pr?.html_url" class="button button--secondary button--compact" :href="task.pr.html_url" target="_blank" rel="noreferrer">在 GitHub 查看 ↗</a>
          <button v-if="!terminal" type="button" class="button button--secondary button--compact" :disabled="cancelling" @click="emit('cancel')">{{ cancelling ? "取消中…" : "取消审查" }}</button>
          <button v-else type="button" class="button button--primary button--compact" @click="emit('newReview')">＋ 新建审查</button>
        </div>
      </div>
    </header>

    <nav class="review-tabs" aria-label="审查结果导航">
      <button v-for="tab in ([['overview', '概览'], ['review', `审查 ${task.issues.length || ''}`], ['files', `文件 ${task.changed_files.length || ''}`], ['activity', '活动']] as const)" :key="tab[0]" type="button" :class="{ 'is-active': activeTab === tab[0] }" :aria-current="activeTab === tab[0] ? 'page' : undefined" @click="activeTab = tab[0]">
        {{ tab[1] }}
      </button>
    </nav>

    <section v-if="task.status === 'waiting_for_human'" class="human-waiting-banner" role="status">
      <StatusBadge status="needs_human" label="等待人工确认" />
      <strong>{{ task.human_request?.questions?.[0] || "审查需要补充产品或业务语义后才能继续" }}</strong>
      <span>任务已安全暂停在 checkpoint</span>
    </section>

    <section v-if="activeTab === 'overview'" class="review-tab-panel overview-view">
      <div class="review-overview-metrics">
        <article class="review-metric-card">
          <span class="review-metric-card__icon is-primary"><AppIcon name="context" :size="20" /></span>
          <p>文件覆盖率</p><strong>{{ fileCoveragePercent }}%</strong><small>{{ coverage.reviewed_files }} / {{ coverage.eligible_files }} 个可审查文件</small>
        </article>
        <article class="review-metric-card">
          <span class="review-metric-card__icon is-warning"><AppIcon name="units" :size="20" /></span>
          <p>变更组覆盖率</p><strong>{{ unitCoveragePercent }}%</strong><small>{{ coverage.completed_units }} / {{ coverage.total_units }} 个已完成</small>
        </article>
        <article class="review-metric-card">
          <span class="review-metric-card__icon is-danger"><AppIcon name="alert" :size="20" /></span>
          <p>已确认发现</p><strong>{{ confirmedIssueCount }}</strong><small>{{ highPriorityCount }} 高 · {{ confirmedSeverityCounts.medium }} 中 · {{ confirmedSeverityCounts.low }} 低<span v-if="needsHumanIssueCount"> · {{ needsHumanIssueCount }} 待人工</span></small>
        </article>
        <article class="review-metric-card">
          <span class="review-metric-card__icon is-info"><AppIcon name="server" :size="20" /></span>
          <p>Project CI</p><strong class="review-metric-card__status">{{ ciPresentation.value }}</strong><small>{{ ciPresentation.detail }}</small>
        </article>
      </div>

      <div class="overview-grid">
        <article class="overview-card overview-card--change">
          <header><div><h2>这次改了什么</h2><p>PR 目的与确定性变更结构</p></div><StatusBadge :status="task.status" /></header>
          <p class="change-summary">{{ purposeSummary }}</p>
          <div class="change-facts"><span><b>{{ task.changed_files.length }}</b> 个变更文件</span><span><b>+{{ task.changed_files.reduce((sum, file) => sum + file.additions, 0) }}</b> 新增</span><span><b>-{{ task.changed_files.reduce((sum, file) => sum + file.deletions, 0) }}</b> 删除</span></div>
        </article>

        <article class="overview-card overview-card--attention">
          <header><div><h2>需要你关注</h2><p>按严重程度和置信度排序</p></div><button v-if="task.issues.length" type="button" class="text-button" @click="activeTab = 'review'">查看全部 →</button></header>
          <EmptyState v-if="!attentionIssues.length" icon="check-circle" title="没有达到报告门槛的问题" description="仍建议人工复核关键业务路径。" />
          <button v-for="issue in attentionIssues" v-else :key="issue.id" type="button" class="attention-row" @click="openIssue(issue)">
            <StatusBadge :status="issue.severity" :label="severityLabel(issue.severity)" />
            <span><strong>{{ issue.title }}</strong><code>{{ issue.primary_evidence.file_path }}<template v-if="issue.primary_evidence.resolved_start_line">:{{ issue.primary_evidence.resolved_start_line }}</template></code></span>
            <small>{{ Math.round(issue.confidence * 100) }}%</small>
          </button>
        </article>

        <article class="overview-card overview-card--groups">
          <header><div><h2>变更组</h2><p>按代码意图组织，而不是按文件名排序</p></div><span>{{ task.review_units.length }} 组</span></header>
          <button v-for="unit in task.review_units" :key="unit.id" type="button" class="overview-group-row" @click="openUnit(unit.id)">
            <span class="overview-group-row__mark"><AppIcon name="units" :size="15" /></span>
            <span><strong>{{ unitTitle(unit) }}</strong><small>{{ unitResult(unit.id)?.plan?.change_summary || unit.grouping_reason }}</small></span>
            <span>{{ unitFiles(unit).length }} 文件</span>
            <b :class="{ 'is-clean': issueCountForUnit(unit.id) === 0 }">{{ issueCountForUnit(unit.id) ? `${issueCountForUnit(unit.id)} 个发现` : '✓ 清洁' }}</b>
          </button>
        </article>

        <article class="overview-card overview-card--health">
          <header><div><h2>审查健康度</h2><p>覆盖率、证据链和运行警告</p></div><StatusBadge :status="task.status" /></header>
          <div class="health-row"><span>可审查文件</span><i><b :style="{ width: `${fileCoveragePercent}%` }" /></i><strong>{{ coverage.reviewed_files }}/{{ coverage.eligible_files }}</strong></div>
          <div class="health-row"><span>变更组完成</span><i><b :style="{ width: `${unitCoveragePercent}%` }" /></i><strong>{{ coverage.completed_units }}/{{ coverage.total_units }}</strong></div>
          <div class="health-row"><span>证据链已定位</span><i><b :style="{ width: `${task.issues.length ? (resolvedEvidenceCount / task.issues.length) * 100 : 100}%` }" /></i><strong>{{ resolvedEvidenceCount }}/{{ task.issues.length }}</strong></div>
          <div v-if="task.warnings.length" class="health-warning"><AppIcon name="alert" :size="16" /><span><strong>{{ task.warnings.length }} 条运行警告</strong>{{ task.warnings[0] }}</span></div>
          <div class="health-usage"><span>模型使用</span><code>{{ task.model_usage_summary.overall.calls }} 次调用 · {{ formatTokens(task.model_usage_summary.overall.actual_total_tokens || task.model_usage_summary.overall.accounted_tokens_estimate) }} tokens · {{ formatDuration(task.model_usage_summary.overall.latency_ms_p50 || 0) }} p50</code></div>
        </article>
      </div>
    </section>

    <section v-else-if="activeTab === 'review'" class="review-tab-panel findings-view">
      <div class="finding-toolbar">
        <div class="filter-chips" aria-label="严重程度筛选">
          <button type="button" :class="{ 'is-active': severityFilter === 'all' }" @click="severityFilter = 'all'">全部 {{ task.issues.length }}</button>
          <button type="button" data-tone="danger" :class="{ 'is-active': severityFilter === 'high' }" @click="severityFilter = 'high'">高 {{ severityCounts.high + severityCounts.critical }}</button>
          <button type="button" data-tone="warning" :class="{ 'is-active': severityFilter === 'medium' }" @click="severityFilter = 'medium'">中 {{ severityCounts.medium }}</button>
          <button type="button" data-tone="info" :class="{ 'is-active': severityFilter === 'low' }" @click="severityFilter = 'low'">低 {{ severityCounts.low }}</button>
          <button type="button" :class="{ 'is-active': statusFilter === 'confirmed' }" @click="statusFilter = statusFilter === 'confirmed' ? 'all' : 'confirmed'">已确认</button>
          <button type="button" data-tone="purple" :class="{ 'is-active': statusFilter === 'needs_human' }" @click="statusFilter = statusFilter === 'needs_human' ? 'all' : 'needs_human'">需人工</button>
        </div>
        <label>类别<select v-model="categoryFilter"><option value="all">全部</option><option v-for="category in categories" :key="category" :value="category">{{ category }}</option></select></label>
      </div>

      <div class="finding-workspace">
        <aside class="change-group-nav">
          <header><div><h2>变更组</h2><p>语义相关的改动故事</p></div><strong>{{ fileCoveragePercent }}%</strong></header>
          <button v-for="unit in task.review_units" :key="unit.id" type="button" :class="{ 'is-active': selectedUnit?.id === unit.id }" @click="chooseUnit(unit.id)">
            <strong>{{ unitTitle(unit) }}</strong><p>{{ unitResult(unit.id)?.plan?.change_summary || unit.grouping_reason }}</p><span>{{ unitFiles(unit).length }} 文件 · {{ issueCountForUnit(unit.id) ? `${issueCountForUnit(unit.id)} 个发现` : '清洁' }}</span>
          </button>
          <div v-if="selectedUnit" class="change-group-files">
            <span>组内文件</span><code v-for="file in unitFiles(selectedUnit)" :key="file">{{ file }}</code>
            <details><summary>运行细节</summary><p>{{ selectedUnit.complexity }} · {{ selectedUnit.estimated_tokens.toLocaleString() }} 估算 tokens</p><p>{{ selectedUnit.risk_tags.join(' · ') || '无风险标签' }}</p><button v-if="['failed', 'timed_out'].includes(selectedUnitResult?.status || '')" type="button" class="button button--secondary button--compact" :disabled="retryingUnitId === selectedUnit.id" @click="emit('retry', selectedUnit.id)">{{ retryingUnitId === selectedUnit.id ? '重试中…' : '重试变更组' }}</button></details>
          </div>
        </aside>

        <article class="diff-viewer">
          <header><code>{{ selectedChangedFile?.file_path || selectedIssue?.primary_evidence.file_path || "选择一个发现查看代码" }}</code><span v-if="selectedChangedFile">+{{ selectedChangedFile.additions }} −{{ selectedChangedFile.deletions }}</span></header>
          <div v-if="selectedIssue && codeRows.length" class="code-table" role="table" aria-label="问题代码证据">
            <div v-for="row in codeRows" :key="row.key" class="code-row" :data-kind="row.kind" :class="{ 'is-highlighted': row.highlighted }" role="row">
              <span>{{ row.oldLine ?? '' }}</span><span>{{ row.newLine ?? '' }}</span><i>{{ row.kind === 'added' ? '+' : row.kind === 'deleted' ? '−' : ' ' }}</i><code>{{ row.content }}</code>
            </div>
          </div>
          <EmptyState v-else-if="!selectedIssue" icon="check-circle" title="这个变更组没有需报告的问题" description="你仍可以在文件页确认覆盖范围，或切换到其他变更组。" />
          <EmptyState v-else icon="code" title="代码上下文尚未定位" description="该发现保留在摘要中，等待人工确认或更精确的证据。" />
          <div v-if="selectedIssue" class="inline-finding-note"><StatusBadge :status="selectedIssue.severity" label="发现" /><strong>锚定到 {{ selectedIssue.primary_evidence.resolved_start_line ? `第 ${selectedIssue.primary_evidence.resolved_start_line} 行` : '当前证据片段' }}</strong><p>{{ selectedIssue.failure_scenario }}</p></div>
          <div v-if="filteredIssues.length > 1" class="finding-switcher">
            <button v-for="issue in filteredIssues" :key="issue.id" type="button" :class="{ 'is-active': selectedIssue?.id === issue.id }" @click="selectedIssueId = issue.id"><span :data-severity="issue.severity" />{{ issue.title }}</button>
          </div>
        </article>

        <aside class="finding-evidence-panel">
          <template v-if="selectedIssue">
            <header><div><h2>发现与证据</h2><p>{{ selectedIssue.category }}</p></div><StatusBadge :status="selectedIssue.severity" :label="severityLabel(selectedIssue.severity)" /></header>
            <div class="finding-badges"><StatusBadge :status="selectedIssue.status" /><StatusBadge :status="selectedIssue.placement" /></div>
            <h3>{{ selectedIssue.title }}</h3>
            <p class="finding-confidence">正确性 · {{ Math.round(selectedIssue.confidence * 100) }}% 置信度</p>
            <dl class="evidence-facts">
              <div><dt>证据定位</dt><dd>{{ selectedIssue.primary_evidence.resolution_status }} · {{ selectedIssue.primary_evidence.resolution_method }} · {{ selectedIssue.primary_evidence.provenance || '未知来源' }}</dd></div>
              <div><dt>为什么重要</dt><dd>{{ selectedIssue.affected_behavior }}</dd></div>
              <div><dt>失败场景</dt><dd>{{ selectedIssue.failure_scenario }}</dd></div>
              <div><dt>建议</dt><dd>{{ selectedIssue.recommendation }}</dd></div>
            </dl>
            <section v-if="selectedIssue.supporting_evidence.length" class="supporting-evidence">
              <h4>辅助证据</h4>
              <article v-for="evidence in selectedIssue.supporting_evidence" :key="`${evidence.file_path}:${evidence.resolved_start_line}`"><code>{{ evidence.file_path }}<template v-if="evidence.resolved_start_line">:{{ evidence.resolved_start_line }}</template></code><pre>{{ evidence.existing_code }}</pre></article>
            </section>
            <p v-if="selectedIssue.related_tests.length" class="related-tests"><span>相关测试</span><code>{{ selectedIssue.related_tests.join(' · ') }}</code></p>
          </template>
          <template v-else-if="task.human_request">
            <header><div><h2>人工确认请求</h2><p>自动审查已安全暂停</p></div><StatusBadge status="needs_human" /></header>
            <h3>{{ task.human_request.questions[0] }}</h3>
            <dl class="evidence-facts"><div><dt>缺失信息</dt><dd>{{ task.human_request.missing_information.join('；') }}</dd></div><div><dt>已知证据</dt><dd>{{ task.human_request.known_evidence.join('；') }}</dd></div></dl>
          </template>
          <EmptyState v-else icon="check-circle" title="没有选中的发现" description="请选择包含发现的变更组。" />
        </aside>
      </div>
    </section>

    <section v-else-if="activeTab === 'files'" class="review-tab-panel files-view">
      <div class="file-metrics">
        <article><p>变更文件</p><strong>{{ coverage.changed_files || task.changed_files.length }}</strong><small>{{ coverage.eligible_files }} 可审查 · {{ coverage.skipped_files }} 已排除</small></article>
        <article><p>已审查</p><strong>{{ coverage.reviewed_files }}</strong><small>{{ coverage.partial_files }} 个部分完成</small></article>
        <article><p>部分 / 失败</p><strong>{{ coverage.partial_files }} / {{ coverage.failed_files }}</strong><small>需要人工确认覆盖范围</small></article>
        <article><p>覆盖率</p><strong>{{ fileCoveragePercent }}%</strong><small>变更组 {{ unitCoveragePercent }}%</small></article>
      </div>
      <div class="files-layout">
        <aside class="coverage-navigator">
          <header><h2>覆盖导航</h2><p>按变更组和状态筛选</p></header>
          <button type="button" :class="{ 'is-active': fileGroupFilter === 'all' }" @click="fileGroupFilter = 'all'">全部文件 <b>{{ task.changed_files.length }}</b></button>
          <button v-for="unit in task.review_units" :key="unit.id" type="button" :class="{ 'is-active': fileGroupFilter === unit.id }" @click="fileGroupFilter = unit.id">{{ unitTitle(unit) }} <b>{{ unitFiles(unit).length }}</b></button>
          <hr><span>文件状态</span>
          <button v-for="state in ([['reviewed', '已审查'], ['partial', '部分完成'], ['excluded_sensitive', '敏感排除'], ['timed_out', '已超时']] as const)" :key="state[0]" type="button" :class="{ 'is-active': fileStatusFilter === state[0] }" @click="fileStatusFilter = fileStatusFilter === state[0] ? 'all' : state[0]">{{ state[1] }} <b>{{ coverage.files.filter(item => item.status === state[0]).length }}</b></button>
        </aside>
        <article class="files-table-card">
          <header><div><h2>变更文件</h2><p>覆盖状态、所属变更组与排除原因</p></div><span>{{ fileRows.length }} / {{ task.changed_files.length }}</span></header>
          <div class="files-table-wrap">
            <table class="files-table"><thead><tr><th>文件</th><th>变更组</th><th>Delta</th><th>覆盖状态</th><th>Owner Units</th></tr></thead><tbody>
              <tr v-for="file in fileRows" :key="file.file_path" :class="{ 'is-clickable': file.reviewUnitIds.length }" @click="file.reviewUnitIds[0] && openUnit(file.reviewUnitIds[0])">
                <td><code>{{ file.file_path }}</code><small v-if="file.reason">{{ file.reason }}</small></td>
                <td>{{ unitNames(file.reviewUnitIds) }}</td>
                <td><b class="text-success">+{{ file.additions }}</b> <b class="text-danger">−{{ file.deletions }}</b></td>
                <td><StatusBadge :status="fileStatusTone(file.status)" :label="statusLabel(file.status)" /></td>
                <td><code>{{ file.reviewUnitIds.join(', ') || '—' }}</code></td>
              </tr>
            </tbody></table>
          </div>
          <EmptyState v-if="!fileRows.length" icon="file-code" title="没有匹配的文件" description="请调整变更组或覆盖状态筛选。" />
        </article>
      </div>
    </section>

    <section v-else class="review-tab-panel activity-view">
      <article class="run-manifest-card">
        <div><span>Run Manifest · {{ task.run_manifest?.schema_version || '实时任务' }}</span><h2>可审计的审查运行记录</h2><code>{{ task.id }} · {{ task.pr?.owner }}/{{ task.pr?.repo }} #{{ task.pr?.number }}</code></div>
        <StatusBadge :status="task.status" /><button v-if="report || task.report_markdown" type="button" class="button button--secondary button--compact" @click="openRawReport">查看原始报告</button>
      </article>
      <div class="activity-grid">
        <ExecutionTimeline :steps="task.steps || []" :task-status="task.status" :events="task.agent_events || []" :static-results="task.static_results || []" :test-results="task.test_results || []" />
        <aside class="run-summary-card">
          <header><h2>运行摘要</h2><p>模型调用、Token 与覆盖清单</p></header>
          <div class="run-summary-metrics"><span><small>耗时</small><strong>{{ task.run_manifest ? formatDuration(task.run_manifest.duration_ms) : taskDuration || '进行中' }}</strong></span><span><small>模型调用</small><strong>{{ task.model_usage_summary.overall.calls }}</strong></span><span><small>Tokens</small><strong>{{ formatTokens(task.model_usage_summary.overall.actual_total_tokens || task.model_usage_summary.overall.accounted_tokens_estimate) }}</strong></span><span><small>发现</small><strong>{{ task.issues.length }}</strong></span></div>
          <h3>按操作统计</h3>
          <div v-for="operation in task.model_usage_summary.by_operation" :key="operation.key" class="operation-row"><code>{{ operation.key }}</code><i><b :style="{ width: `${operation.stats.calls / maxOperationCalls * 100}%` }" /></i><span>{{ operation.stats.calls }} 次 · {{ formatTokens(operation.stats.actual_total_tokens || operation.stats.accounted_tokens_estimate) }}</span></div>
          <h3>覆盖清单</h3>
          <dl class="manifest-coverage"><div><dt>文件覆盖率</dt><dd>{{ coverage.reviewed_files }}/{{ coverage.eligible_files }} · {{ fileCoveragePercent }}%</dd></div><div><dt>变更组覆盖率</dt><dd>{{ coverage.completed_units }}/{{ coverage.total_units }} · {{ unitCoveragePercent }}%</dd></div><div><dt>警告</dt><dd>{{ task.warnings.length }}</dd></div></dl>
          <p v-for="warning in task.warnings" :key="warning" class="manifest-warning"><AppIcon name="alert" :size="14" />{{ warning }}</p>
        </aside>
      </div>
      <details v-if="report || task.report_markdown" id="raw-review-report" class="raw-report-details"><summary>原始 Markdown 报告与导出</summary><ReportPanel :markdown="report || task.report_markdown" /></details>
    </section>

    <footer class="app-footer"><span>只读审查 · 证据可追溯 · 不写回目标仓库</span><span><i /> RepoGuardian Review Viewer</span></footer>
  </main>
</template>
