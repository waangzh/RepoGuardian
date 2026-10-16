<script setup lang="ts">
import { computed } from "vue";
import type { TaskRuntimeReport } from "../../types/review";
import { retryableRuntimeUnits, runtimeLabel, relationshipGroups, relationshipEvidence, investigationRelationships, taskRuntimeSummary, runtimeWarnings, runtimeFindingRows, filterRuntimeFindings } from "../../utils/taskRuntimePresentation";
const props = defineProps<{ report: TaskRuntimeReport; terminal: boolean; retryingUnitId: string | null; view?: "overview" | "checks" }>();
const emit = defineEmits<{ retry: [unitId: string]; "open-checks": []; "open-finding": [key: string] }>();
const retryUnits = computed(() => retryableRuntimeUnits(props.report));
const summary = computed(() => taskRuntimeSummary(props.report));
const warnings = computed(() => runtimeWarnings(props.report));
const activeFindings = computed(() => filterRuntimeFindings(runtimeFindingRows(props.report), "active"));
const leadGroups = computed(() => relationshipGroups(props.report.unscheduled_leads));
const hintGroups = computed(() => relationshipGroups(props.report.relationship_hints));
function claim(value: Record<string, unknown>): string {
  const candidate = value.candidate as Record<string, unknown> | undefined;
  return String(candidate?.claim || "未命名候选");
}
</script>

<template>
  <article class="overview-card">
    <template v-if="view !== 'checks'">
      <header><div><h2>审查结果</h2><p>{{ runtimeLabel(report.execution_status) }} · {{ report.scope_complete ? '初始范围已检查' : '初始范围存在缺口' }}</p></div></header>
      <p class="result-counts">{{ summary.findings.confirmed }} 项已确认 · {{ summary.findings.unresolved }} 项待确认 · {{ summary.scope.incomplete }} 项范围缺口</p>
      <p v-if="report.execution_status === 'failed'" role="alert">失败原因：{{ report.failure_summary || '技术失败；旧报告未记录具体原因' }}</p>
      <ul v-if="warnings.length" class="runtime-warnings" aria-label="审查限制与未决事项"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul>
      <h3>需要处理的发现</h3>
      <p v-if="!activeFindings.length">{{ report.execution_status === 'running' ? '审查进行中，发现与检查范围仍在更新。' : '当前没有已确认或待确认的发现；检查限制以上方提示为准。' }}</p>
      <button v-for="item in activeFindings" :key="item.key" type="button" class="finding-summary" @click="emit('open-finding', item.key)">
        <strong>{{ runtimeLabel(item.status) }} · {{ item.title }}</strong>
        <span v-if="item.failureScenario">{{ item.failureScenario }}</span>
        <span v-if="item.reason">{{ item.reason }}</span>
        <span>查看详情 →</span>
      </button>
      <button type="button" class="text-button" @click="$emit('open-checks')">查看检查范围与证据 →</button>
    </template>
    <template v-else>
    <header><div><h2>约定范围检查</h2><p>task-goals-v1 · {{ report.scope_complete ? '初始范围已检查' : '初始范围存在缺口' }}</p></div></header>
    <p>{{ runtimeLabel(report.termination_reason) }} · 计量{{ runtimeLabel(report.usage_integrity) }}</p>
    <p v-if="report.execution_status === 'failed'" role="alert">失败原因：{{ report.failure_summary || '技术失败；旧报告未记录具体原因' }}</p>
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
    <div v-for="item in report.admitted_investigations" :key="String(item.work_item_id)">
      <p>{{ item.proposition }} · {{ runtimeLabel(item.status) }}<template v-if="item.answer"> · {{ runtimeLabel(item.answer) }}</template></p>
      <p v-if="item.reason">结论依据：{{ item.reason }}</p>
      <ul v-if="Array.isArray(item.limitations) && item.limitations.length"><li v-for="(limitation, index) in item.limitations" :key="index">限制：{{ limitation }}</li></ul>
      <details v-if="investigationRelationships(item).length"><summary>技术详情与关联证据</summary><p v-for="lead in investigationRelationships(item)" :key="String(lead.lead_id)">{{ relationshipEvidence(lead) }}</p></details>
    </div>
    <h3>缺陷结果</h3>
    <p v-if="!report.findings.length">当前没有候选发现；检查范围和缺口以上表为准。</p>
    <p v-for="item in report.findings" :key="String(item.key)">{{ runtimeLabel(item.status) }} · {{ claim(item) }}<template v-if="item.reason"> · {{ item.reason }}</template></p>
    <h3 v-if="report.unscheduled_leads.length">未执行线索</h3>
    <details v-for="group in leadGroups" :key="group.key">
      <summary>{{ group.label }} · {{ runtimeLabel(group.status) }} · {{ group.reason }} · {{ group.members.length }} 条</summary>
      <p v-for="(item, index) in group.members" :key="String(item.lead_id || index)">{{ item.proposition }} · {{ relationshipEvidence(item) }}</p>
    </details>
    <h3 v-if="hintGroups.length">关系取证提示</h3>
    <p v-if="hintGroups.length">以下关联尚未核验，不计作已完成调查。</p>
    <details v-for="group in hintGroups" :key="group.key">
      <summary>{{ group.label }} · {{ group.members.length }} 条</summary>
      <p v-for="(item, index) in group.members" :key="String(item.lead_id || index)">{{ relationshipEvidence(item) }}</p>
    </details>
    </template>
  </article>
</template>
