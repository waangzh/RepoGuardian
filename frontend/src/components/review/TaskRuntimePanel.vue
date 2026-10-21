<script setup lang="ts">
import { computed } from "vue";
import type { TaskRuntimeReport } from "../../types/review";
import { retryableRuntimeUnits, runtimeLabel, relationshipGroups, relationshipEvidence, investigationRelationships, taskRuntimeSummary, runtimeWarnings, runtimeFindingRows, filterRuntimeFindings, asRecord } from "../../utils/taskRuntimePresentation";
import StatusBadge from "../common/StatusBadge.vue";
import EmptyState from "../common/EmptyState.vue";
import AppIcon from "../common/AppIcon.vue";
const props = defineProps<{ report: TaskRuntimeReport; terminal: boolean; retryingUnitId: string | null; view?: "overview" | "checks" }>();
const emit = defineEmits<{ retry: [unitId: string]; "open-checks": []; "open-finding": [key: string] }>();
const retryUnits = computed(() => retryableRuntimeUnits(props.report));
const summary = computed(() => taskRuntimeSummary(props.report));
const warnings = computed(() => runtimeWarnings(props.report));
const activeFindings = computed(() => filterRuntimeFindings(runtimeFindingRows(props.report), "active"));
const leadGroups = computed(() => relationshipGroups(props.report.unscheduled_leads));
const hintGroups = computed(() => relationshipGroups(props.report.relationship_hints));
function investigationTitle(value: Record<string, unknown>, index: number): string {
  const relation = asRecord(investigationRelationships(value)[0]?.relationship);
  if (relation.source_file && relation.target_file) {
    const filename = (path: unknown) => String(path).split(/[\\/]/).pop();
    return `${filename(relation.source_file)} → ${filename(relation.target_file)} 的关联检查`;
  }
  const title = String(value.proposition || `补充调查 ${index + 1}`);
  return title.length > 90 ? `${title.slice(0, 90)}…` : title;
}
</script>

<template>
  <article class="overview-card runtime-panel" :class="view === 'checks' ? 'runtime-panel--checks' : 'runtime-panel--overview'">
    <template v-if="view !== 'checks'">
      <header><div><h2>审查结论</h2><p>{{ runtimeLabel(report.execution_status) }} · 结论仅适用于本次审查快照</p></div><StatusBadge :status="warnings.length ? 'warning' : 'info'" :label="warnings.length ? '存在检查限制' : '查看本轮结果'" /></header>
      <div class="runtime-panel__body">
        <div class="result-counts runtime-stats">
          <div><span>代码证据已确认的问题</span><strong>{{ summary.findings.confirmed }}</strong><small>有代码证据支持，不代表审查通过</small></div>
          <div><span>待确认疑点</span><strong>{{ summary.findings.unresolved }}</strong><small>尚未确认缺陷，仍需补充依据</small></div>
          <div><span>约定目标已检查</span><strong>{{ summary.scope.examined }}<small> / {{ summary.scope.total }}</small></strong><small>{{ summary.scope.incomplete ? `${summary.scope.incomplete} 项尚未完成` : '不代表所有业务路径均已验证' }}</small></div>
        </div>
        <section v-if="warnings.length" class="runtime-warning-block" aria-label="审查限制与未决事项">
          <h3><AppIcon name="alert" :size="17" />仍需关注</h3>
          <ul class="runtime-warnings"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul>
        </section>
        <section class="runtime-section">
          <div class="runtime-section__heading"><h3>需要处理的发现</h3><button v-if="activeFindings.length" type="button" class="text-button" @click="emit('open-finding', activeFindings[0]!.key)">查看全部 {{ activeFindings.length }} 项 →</button></div>
          <EmptyState v-if="!activeFindings.length" :icon="report.execution_status === 'running' ? 'clock' : 'context'" :title="report.execution_status === 'running' ? '正在检查变更' : '本轮没有需处理的发现'" description="已确认问题与待确认候选会显示在这里；检查限制以上方提示为准。" />
          <button v-for="item in activeFindings.slice(0, 5)" :key="item.key" type="button" class="finding-summary" @click="emit('open-finding', item.key)">
            <StatusBadge :status="item.status" :label="runtimeLabel(item.status)" /><span class="finding-summary__text"><strong>{{ item.title }}</strong><span>{{ item.failureScenario || item.reason || '查看结论、代码依据与处理建议' }}</span></span><span aria-hidden="true">→</span>
          </button>
        </section>
        <footer class="runtime-panel__footer"><span>初始范围、调查与缺陷结论分别记录</span><button type="button" class="button button--secondary button--compact" @click="emit('open-checks')">检查范围与依据 →</button></footer>
      </div>
    </template>
    <template v-else>
      <header><div><h2>检查范围与依据</h2><p>了解已检查的内容，以及仍缺少依据的部分</p></div><StatusBadge :status="summary.scope.incomplete ? 'warning' : 'info'" :label="summary.scope.incomplete ? `${summary.scope.incomplete} 项范围缺口` : `${summary.scope.examined} 项目标已检查`" /></header>
      <div class="runtime-panel__body">
        <section class="runtime-section"><div class="runtime-section__heading"><h3>约定检查范围</h3><span>{{ summary.scope.examined }} / {{ summary.scope.total }} 项已检查</span></div>
          <p class="runtime-caption">以下状态只描述约定目标的检查情况，不表示代码正确性证明。</p>
          <div class="runtime-scope-wrap"><table class="runtime-scope-table"><thead><tr><th>文件与范围</th><th>状态</th><th>检查说明</th></tr></thead><tbody>
            <tr v-for="goal in report.initial_scope_coverage" :key="String(goal.goal_id)">
              <td><code>{{ goal.target_ref }}<template v-if="goal.target_kind === 'source_range'">:{{ goal.start_line }}–{{ goal.end_line }}</template></code></td>
              <td><StatusBadge :status="goal.status === 'examined' ? 'info' : 'warning'" :label="runtimeLabel(goal.status)" /></td>
              <td><details><summary>查看说明</summary><p>{{ goal.reason || '此记录未提供检查说明。' }}</p></details></td>
            </tr>
          </tbody></table></div>
          <p v-if="!summary.scope.total" class="runtime-caption">尚无范围记录，检查完整性未知。</p>
          <div v-if="terminal && retryUnits.length" class="runtime-retry-actions"><button v-for="unit in retryUnits" :key="unit" class="button button--secondary button--compact" type="button" :disabled="retryingUnitId === unit" @click="emit('retry', unit)">{{ retryingUnitId === unit ? '重新排队中…' : '重新检查缺口' }}<span class="runtime-unit-id">{{ unit }}</span></button></div>
        </section>
        <section class="runtime-section"><div class="runtime-section__heading"><h3>补充调查</h3><span>{{ summary.investigations.answered }} 项已有答案 / 共 {{ summary.investigations.total }} 项</span></div>
          <p class="runtime-caption">调查回答与缺陷发现分别记录；支持某个命题不直接等于发现缺陷。</p>
          <p v-if="!report.admitted_investigations.length" class="runtime-caption">本轮没有已准入的补充调查。</p>
          <details v-for="(item, index) in report.admitted_investigations" :key="String(item.work_item_id)" class="runtime-investigation" :open="item.status !== 'answered'">
            <summary><span>{{ investigationTitle(item, index) }}</span><StatusBadge :status="item.status === 'answered' ? 'info' : 'warning'" :label="runtimeLabel(item.status)" /></summary>
            <div class="runtime-investigation__body"><p v-if="item.answer" class="runtime-caption">命题回答：{{ runtimeLabel(item.answer) }}</p><p class="runtime-answer">{{ item.reason || '此记录未提供结论依据。' }}</p>
              <ul v-if="Array.isArray(item.limitations) && item.limitations.length" class="runtime-limitations"><li v-for="(limitation, i) in item.limitations" :key="i">{{ limitation }}</li></ul>
              <details class="runtime-technical"><summary>原始检查问题与关联证据</summary><p>{{ item.proposition }}</p><p v-for="lead in investigationRelationships(item)" :key="String(lead.lead_id)">{{ relationshipEvidence(lead) }}</p></details>
            </div>
          </details>
        </section>
        <details v-if="leadGroups.length || hintGroups.length" class="runtime-disclosure"><summary>关联线索与未执行记录 <span>{{ report.unscheduled_leads.length + (report.relationship_hints?.length || 0) }} 条</span></summary>
          <div class="runtime-disclosure__body"><p class="runtime-caption">关联线索尚未核验，不计作已完成调查或已确认问题。</p>
            <details v-for="group in [...leadGroups, ...hintGroups]" :key="group.key" class="runtime-technical"><summary>{{ group.label }} · {{ group.members.length }} 条</summary><p>{{ runtimeLabel(group.status) }} · {{ group.reason }}</p><p v-for="(item, i) in group.members" :key="String(item.lead_id || i)">{{ relationshipEvidence(item) }}</p></details>
          </div>
        </details>
        <details class="runtime-disclosure"><summary>运行与计量详情 <span>{{ report.sent_calls }} 次调用</span></summary><div class="runtime-disclosure__body"><dl class="runtime-run-facts"><div><dt>终止原因</dt><dd>{{ runtimeLabel(report.termination_reason) }}</dd></div><div><dt>已结算 Tokens</dt><dd>{{ report.settled_tokens.toLocaleString() }}</dd></div><div><dt>计量状态</dt><dd>{{ runtimeLabel(report.usage_integrity) }} · {{ report.unknown_attempts }} 次用量未知</dd></div><div><dt>覆盖版本</dt><dd>task-goals-v1</dd></div></dl><p v-if="report.execution_status === 'failed'" role="alert">{{ report.failure_summary || '技术失败；此记录未提供详细原因。' }}</p></div></details>
      </div>
    </template>
  </article>
</template>
