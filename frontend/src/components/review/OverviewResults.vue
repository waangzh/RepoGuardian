<script setup lang="ts">
import { computed } from "vue";
import type { ReviewSummary } from "../../types/publication";
import type { ReviewSection } from "../../types/workspace";
import type { RuntimeFindingRow, TaskRuntimeSummary } from "../../utils/taskRuntimePresentation";
import { runtimeLabel } from "../../utils/taskRuntimePresentation";
import { overviewFacts } from "../../utils/overviewPresentation";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{ plan: ReviewSummary | null; runtime: TaskRuntimeSummary | null; findings: RuntimeFindingRow[]; stale?: boolean }>();
const emit = defineEmits<{ navigate: [section: ReviewSection]; finding: [key: string] }>();
const facts = computed(() => overviewFacts(props.plan, props.runtime));
const suggestions = computed(() => {
  const decision = props.plan?.digest.merge_decision;
  return decision?.nonblocking_finding_keys
    ? decision.nonblocking_independent_count
      ?? decision.nonblocking_finding_keys.length + (decision.nonblocking_historical_keys?.length ?? 0) : null;
});
const highlights = computed(() => {
  const blockers = props.plan?.digest.merge_decision?.blocking_finding_keys || [];
  const rank = (row: RuntimeFindingRow) => blockers.includes(row.key) ? 0 : row.group === "confirmed" ? 1 : 2;
  return props.findings.filter(row => row.group !== "dismissed").sort((a, b) => rank(a) - rank(b)).slice(0, 3);
});
</script>

<template>
  <section class="ov-results" aria-labelledby="overview-results-title">
    <header class="ov-section-head"><div><h2 id="overview-results-title">问题与建议</h2></div><button type="button" class="ov-link" @click="emit('navigate', 'findings')">查看全部问题与证据 <AppIcon name="external" :size="14" /></button></header>
    <div class="ov-panel ov-metrics">
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="shield" :size="16" />高优先级问题</span>
        <strong :class="{ 'ov-count-danger': facts.blockers !== null && facts.blockers > 0 }">{{ facts.blockers ?? "未知" }}</strong>
        <small>{{ facts.blockers === null ? "摘要未提供准确数量" : facts.blockers > 0 ? "按审查策略建议合并前处理 · 同一缺陷只计一次" : "没有达到高优先级标准的证据" }}</small>
      </button>
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="help" :size="16" />待确认疑点</span>
        <strong>{{ facts.unresolved ?? "未知" }}</strong>
        <small>尚未确认缺陷 · 不含关联线索</small>
      </button>
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="info" :size="16" />改进建议</span>
        <strong>{{ suggestions ?? "未知" }}</strong>
        <small>按实际影响决定是否处理</small>
      </button>
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
