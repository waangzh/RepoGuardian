<script setup lang="ts">
import { computed } from "vue";
import type { TaskRuntimeReport } from "../../types/review";
import { retryableRuntimeUnits, runtimeLabel } from "../../utils/taskRuntimePresentation";
const props = defineProps<{ report: TaskRuntimeReport; terminal: boolean; retryingUnitId: string | null }>();
const emit = defineEmits<{ retry: [unitId: string] }>();
const retryUnits = computed(() => retryableRuntimeUnits(props.report));
function claim(value: Record<string, unknown>): string {
  const candidate = value.candidate as Record<string, unknown> | undefined;
  return String(candidate?.claim || "未命名候选");
}
</script>

<template>
  <article class="overview-card">
    <header><div><h2>约定范围检查</h2><p>task-goals-v1 · {{ report.scope_complete ? '初始范围已检查' : '初始范围存在缺口' }}</p></div></header>
    <p>{{ runtimeLabel(report.termination_reason) }} · 计量{{ runtimeLabel(report.usage_integrity) }}</p>
    <p>已发送 {{ report.sent_calls }} 次 · 已结算 {{ report.settled_tokens.toLocaleString() }} tokens · {{ report.unknown_attempts }} 次用量未知</p>
    <div class="files-table-wrap"><table class="files-table"><thead><tr><th>固定范围</th><th>状态</th><th>说明</th></tr></thead><tbody>
      <tr v-for="goal in report.initial_scope_coverage" :key="String(goal.goal_id)">
        <td><code>{{ goal.target_ref }}<template v-if="goal.target_kind === 'source_range'">:{{ goal.start_line }}–{{ goal.end_line }}</template></code></td>
        <td>{{ runtimeLabel(goal.status) }}</td><td>{{ goal.reason }}</td>
      </tr>
    </tbody></table></div>
    <button v-for="unit in terminal ? retryUnits : []" :key="unit" class="button button--secondary button--compact" type="button"
      :disabled="retryingUnitId === unit" @click="emit('retry', unit)">重新排队缺口 · {{ unit }}</button>
    <h3>调查命题</h3>
    <p v-if="!report.admitted_investigations.length">当前没有准入调查。</p>
    <p v-for="item in report.admitted_investigations" :key="String(item.work_item_id)">{{ item.proposition }} · {{ runtimeLabel(item.status) }}<template v-if="item.answer"> · {{ runtimeLabel(item.answer) }}</template></p>
    <h3>缺陷结果</h3>
    <p v-if="!report.findings.length">当前没有候选发现；检查范围和缺口以上表为准。</p>
    <p v-for="item in report.findings" :key="String(item.key)">{{ runtimeLabel(item.status) }} · {{ claim(item) }}<template v-if="item.reason"> · {{ item.reason }}</template></p>
    <h3 v-if="report.unscheduled_leads.length">未执行线索</h3>
    <p v-for="item in report.unscheduled_leads" :key="String(item.lead_id)">{{ item.proposition }} · {{ item.reason }}</p>
  </article>
</template>
