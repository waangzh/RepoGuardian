<script setup lang="ts">
import { computed } from "vue";
import type { ReviewTask, ReviewUnit, ReviewUnitResult } from "../../types/review";
import EmptyState from "../common/EmptyState.vue";
import PanelHeader from "../common/PanelHeader.vue";
import StatusBadge from "../common/StatusBadge.vue";

const props = defineProps<{
  units: ReviewUnit[];
  results: ReviewUnitResult[];
  taskStatus?: ReviewTask["status"];
  retryingUnitId: string | null;
}>();

const emit = defineEmits<{ retry: [unitId: string] }>();
const resultMap = computed(() => new Map(props.results.map((item) => [item.review_unit_id, item])));
const completed = computed(() => props.results.filter((item) => item.status === "completed").length);
const progress = computed(() => props.units.length ? Math.round((completed.value / props.units.length) * 100) : 0);
const terminal = computed(() => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(props.taskStatus || ""));
</script>

<template>
  <section class="panel review-units-panel">
    <PanelHeader icon="▣" title="审查单元" subtitle="Review Units">
      <div class="panel-progress"><span>{{ completed }} / {{ units.length }} 完成</span><i><b :style="{ width: `${progress}%` }" /></i></div>
    </PanelHeader>
    <EmptyState v-if="!units.length" icon="☷" title="暂无审查单元" description="运行 Preview 后，系统将根据文件、Hunk、符号与风险确定性规划 Review Units。" />
    <div v-else class="unit-list">
      <article v-for="unit in units" :key="unit.id" class="unit-item">
        <div class="unit-item__main">
          <code>{{ unit.primary_files[0] }}</code>
          <p>{{ unit.grouping_reason }}</p>
          <span>{{ unit.related_files.length }} 个关联文件 · {{ unit.complexity }} · {{ unit.estimated_tokens.toLocaleString() }} tokens</span>
        </div>
        <div class="unit-item__status">
          <StatusBadge :status="resultMap.get(unit.id)?.status || 'pending'" />
          <small>{{ resultMap.get(unit.id)?.issues.length || 0 }} Issues</small>
          <button
            v-if="terminal && ['failed', 'timed_out'].includes(resultMap.get(unit.id)?.status || '')"
            type="button"
            class="button button--secondary button--compact"
            :disabled="retryingUnitId === unit.id"
            @click="emit('retry', unit.id)"
          >{{ retryingUnitId === unit.id ? "重试中…" : "重试" }}</button>
        </div>
      </article>
    </div>
  </section>
</template>
