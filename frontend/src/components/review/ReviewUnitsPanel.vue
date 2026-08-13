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
const planStatusLabel: Record<string, string> = {
  planned: "Unit Plan 已生成",
  skipped: "Unit Plan 已跳过",
  failed: "Unit Plan 已降级",
};
const skipReasonLabel: Record<string, string> = {
  small_low_risk_unit: "小型低风险变更",
  planning_disabled: "规划功能未启用",
  budget_insufficient: "规划预算不足",
  planning_failed: "规划生成或校验失败",
};
</script>

<template>
  <section class="panel review-units-panel">
    <PanelHeader icon="units" title="审查单元" subtitle="Review Units">
      <div class="panel-progress"><span>{{ completed }} / {{ units.length }} 完成</span><i><b :style="{ width: `${progress}%` }" /></i></div>
    </PanelHeader>
    <EmptyState v-if="!units.length" icon="units" title="正在规划审查单元" description="系统会根据文件、Hunk、符号与风险拆分可独立审查的单元。" />
    <div v-else class="unit-list">
      <article v-for="unit in units" :key="unit.id" class="unit-item">
        <div class="unit-item__main">
          <code>{{ unit.primary_files[0] }}</code>
          <p>{{ unit.grouping_reason }}</p>
          <span>{{ unit.related_files.length }} 个关联文件 · {{ unit.complexity }} · {{ unit.estimated_tokens.toLocaleString() }} tokens</span>
          <details v-if="resultMap.get(unit.id)?.plan_status" class="unit-plan">
            <summary>
              <StatusBadge
                :status="resultMap.get(unit.id)?.plan_status || 'skipped'"
                :label="planStatusLabel[resultMap.get(unit.id)?.plan_status || 'skipped']"
              />
              <span v-if="resultMap.get(unit.id)?.plan">{{ resultMap.get(unit.id)?.plan?.risk_hypotheses.length }} 个风险假设</span>
              <span v-else>{{ skipReasonLabel[resultMap.get(unit.id)?.plan_skip_reason || ''] || resultMap.get(unit.id)?.plan_skip_reason }}</span>
            </summary>
            <div v-if="resultMap.get(unit.id)?.plan" class="unit-plan__body">
              <strong>{{ resultMap.get(unit.id)?.plan?.change_summary }}</strong>
              <p>{{ resultMap.get(unit.id)?.plan?.review_objectives.join(" · ") }}</p>
              <ul v-if="resultMap.get(unit.id)?.plan?.risk_hypotheses.length">
                <li v-for="risk in resultMap.get(unit.id)?.plan?.risk_hypotheses" :key="risk.id">
                  <span :data-priority="risk.priority">{{ risk.priority }}</span>{{ risk.description }}
                </li>
              </ul>
            </div>
            <p v-else class="unit-plan__reason">
              {{ skipReasonLabel[resultMap.get(unit.id)?.plan_skip_reason || ''] || resultMap.get(unit.id)?.plan_error || "未生成 Unit Plan" }}
            </p>
          </details>
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
