<script setup lang="ts">
import { computed } from "vue";
import type { ReviewSummary } from "../../../types/publication";
import type { ReviewSection } from "../../../types/workspace";
import type { RuntimeFindingRow, TaskRuntimeSummary } from "../../../utils/taskRuntimePresentation";
import { runtimeLabel } from "../../../utils/taskRuntimePresentation";
import { overviewFacts, type FindingMetric } from "../../../utils/overviewPresentation";
import type { FindingEntryFilter } from "../../../utils/reviewSituation";
import type { FindingSource } from "../../../utils/findingWorkbench";
import AppIcon from "../../common/AppIcon.vue";

const props = defineProps<{ plan: ReviewSummary | null; runtime: TaskRuntimeSummary | null; findings: RuntimeFindingRow[]; stale?: boolean }>();
const emit = defineEmits<{
  navigate: [section: ReviewSection];
  finding: [key: string];
  filter: [filter: FindingEntryFilter, source: FindingSource];
}>();
const facts = computed(() => overviewFacts(props.plan, props.runtime));
const highlights = computed(() => {
  const blockers = props.plan?.digest.merge_decision?.blocking_finding_keys || [];
  const rank = (row: RuntimeFindingRow) => blockers.includes(row.key) ? 0 : row.group === "confirmed" ? 1 : 2;
  return props.findings.filter(row => row.group !== "dismissed").sort((a, b) => rank(a) - rank(b)).slice(0, 3);
});

/** 数量构成必须与发现页筛选一一对应：每个数字都能展开成完整条目。 */
interface MetricBreakdown { label: string; count: number; filter: FindingEntryFilter; source: FindingSource }
function breakdown(metric: FindingMetric, filter: FindingEntryFilter): MetricBreakdown[] {
  // 只有来源数量之和与总数一致时才拆分；否则不给出无法对账的构成。
  if (metric.total === null || !metric.verifiable) return [];
  const rows: MetricBreakdown[] = [];
  if (metric.current) rows.push({ label: "本轮 Diff", count: metric.current, filter, source: "current" });
  if (metric.historical) rows.push({ label: "历史复核", count: metric.historical, filter, source: "historical" });
  return rows;
}
const blockerRows = computed(() => breakdown(facts.value.blockerMetric, "blockers"));
const suggestionRows = computed(() => breakdown(facts.value.suggestionMetric, "suggestions"));
const blockerNote = computed(() => {
  if (facts.value.blockers === null) return "摘要未提供准确数量";
  if (!facts.value.blockers) return "没有达到高优先级标准的证据";
  return facts.value.blockerMetric.merged ? "同一缺陷只计一次；指向同一缺陷的历史讨论已合并" : "按审查策略建议合并前处理";
});
const suggestionNote = computed(() => {
  if (facts.value.suggestionMetric.total === null) return "摘要未提供准确数量";
  if (!facts.value.suggestionMetric.total) return "没有已确认的非高优先级建议";
  return facts.value.suggestionMetric.merged ? "同一缺陷只计一次；指向同一缺陷的历史讨论已合并" : "按实际影响决定是否处理";
});
</script>

<template>
  <section class="ov-results" aria-labelledby="overview-results-title">
    <header class="ov-section-head"><div><h2 id="overview-results-title">问题与建议</h2></div><button type="button" class="ov-link" @click="emit('navigate', 'findings')">查看全部问题与证据 <AppIcon name="external" :size="14" /></button></header>
    <div class="ov-panel ov-metrics">
      <div class="ov-metric">
        <button type="button" class="ov-metric__head" @click="emit('filter', 'blockers', 'all')">
          <span class="ov-metric__title"><AppIcon name="shield" :size="16" />高优先级问题</span>
          <strong :class="{ 'ov-count-danger': facts.blockers !== null && facts.blockers > 0 }">{{ facts.blockers ?? "未知" }}</strong>
          <small>{{ blockerNote }}</small>
        </button>
        <div v-if="blockerRows.length" class="ov-metric__sources">
          <button v-for="row in blockerRows" :key="row.source" type="button" class="ov-source" @click="emit('filter', row.filter, row.source)">{{ row.label }} <strong>{{ row.count }}</strong> 项<AppIcon name="arrow-right" :size="12" /></button>
        </div>
      </div>
      <div class="ov-metric">
        <button type="button" class="ov-metric__head" @click="emit('filter', 'unresolved', 'current')">
          <span class="ov-metric__title"><AppIcon name="help" :size="16" />待确认疑点</span>
          <strong>{{ facts.unresolved ?? "未知" }}</strong>
          <small>尚未确认缺陷 · 不含关联线索</small>
        </button>
      </div>
      <div class="ov-metric">
        <button type="button" class="ov-metric__head" @click="emit('filter', 'suggestions', 'all')">
          <span class="ov-metric__title"><AppIcon name="info" :size="16" />改进建议</span>
          <strong>{{ facts.suggestionMetric.total ?? "未知" }}</strong>
          <small>{{ suggestionNote }}</small>
        </button>
        <div v-if="suggestionRows.length" class="ov-metric__sources">
          <button v-for="row in suggestionRows" :key="row.source" type="button" class="ov-source" @click="emit('filter', row.filter, row.source)">{{ row.label }} <strong>{{ row.count }}</strong> 项<AppIcon name="arrow-right" :size="12" /></button>
        </div>
      </div>
    </div>
    <p v-if="stale" class="ov-note"><AppIcon name="info" :size="16" /><span>以上数量来自旧快照，不适用于最新 Head。</span></p>
    <div v-if="highlights.length" class="ov-panel ov-findings">
      <button v-for="row in highlights" :key="row.key" type="button" class="ov-finding" @click="emit('finding', row.key)">
        <span><strong>{{ row.title }}</strong><small>{{ runtimeLabel(row.status) }}<template v-if="row.filePath"> · {{ row.filePath }}{{ row.line ? `:${row.line}` : "" }}</template></small></span>
        <AppIcon name="arrow-right" :size="16" />
      </button>
    </div>
  </section>
</template>
