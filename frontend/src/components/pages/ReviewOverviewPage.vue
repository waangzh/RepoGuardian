<script setup lang="ts">
import { computed } from "vue";
import type { ReviewTask } from "../../types/review";
import type { ReviewSummary } from "../../types/publication";
import type { RuntimeFindingRow, TaskRuntimeSummary } from "../../utils/taskRuntimePresentation";
import type { ReviewSection } from "../../types/workspace";
import type { SituationActionKey } from "../../utils/reviewSituation";
import { reviewSituation } from "../../utils/reviewSituation";
import { runtimeLabel } from "../../utils/taskRuntimePresentation";
import OverviewVerdict from "../review/OverviewVerdict.vue";
import OverviewResults from "../review/OverviewResults.vue";
import OverviewNextSteps from "../review/OverviewNextSteps.vue";
import PrChangeSummary from "../review/PrChangeSummary.vue";
import AppIcon from "../common/AppIcon.vue";
import "../../styles/review-overview.css";

const props = defineProps<{
  task: ReviewTask;
  report?: string | null;
  summaryPlan: ReviewSummary | null;
  summary: TaskRuntimeSummary | null;
  findings: RuntimeFindingRow[];
  warnings: string[];
  terminal: boolean;
  stale?: boolean;
  summaryError?: boolean;
  summaryLoading?: boolean;
}>();
const emit = defineEmits<{
  navigate: [section: ReviewSection];
  finding: [key: string];
  file: [path: string];
  rereview: [];
  snapshot: [status: "current" | "stale" | "unknown"];
  action: [key: Exclude<SituationActionKey, "rereview">];
  reload: [];
}>();
// 只采用显式快照检查的结果，页面加载不访问 GitHub 或假定 Head 仍是最新。
const situation = computed(() => reviewSituation(props.summaryPlan, { stale: props.stale }));
const execution = computed(() => runtimeLabel(props.summary?.execution.status || props.task.status));

function runAction(key: SituationActionKey) {
  if (key === "rereview") emit("rereview");
  else emit("action", key);
}
</script>

<template>
  <div class="rg-overview">
    <div class="ov-main">
      <OverviewVerdict :plan="summaryPlan" :situation="situation" :execution="execution" :summary-error="summaryError" :summary-loading="summaryLoading" @navigate="emit('navigate', $event)" />
      <OverviewResults :plan="summaryPlan" :runtime="summary" :warnings="warnings" :stale="stale" @navigate="emit('navigate', $event)" />
      <section class="ov-panel ov-understanding" aria-labelledby="overview-understanding-title">
        <header class="ov-section-head">
          <div><h2 id="overview-understanding-title">这次 PR 改变了什么？</h2><p>将作者意图和静态证据分开，避免用 PR 描述替代事实。</p></div>
          <button type="button" class="ov-link" @click="emit('navigate', 'files')">查看 Diff <AppIcon name="external" :size="14" /></button>
        </header>
        <PrChangeSummary compact :task="task" :report="report" :understanding="summaryPlan?.digest.change_understanding" :author-limitation="summaryPlan?.digest.author_context_limitation" @file="emit('file', $event)" @evidence="emit('navigate', 'report')" />
      </section>
    </div>
    <OverviewNextSteps :situation="situation" :plan="summaryPlan" :task="task" :execution="execution" :summary-error="summaryError" :summary-loading="summaryLoading" @reload="emit('reload')" @action="runAction" @navigate="emit('navigate', $event)" @snapshot="emit('snapshot', $event)" />
  </div>
</template>
