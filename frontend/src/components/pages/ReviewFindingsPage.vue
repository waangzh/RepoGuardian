<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { getFindings, setFindingDisposition } from '../../api/client';
import type { FindingView, FindingDisposition } from '../../types/reviewPlan';
import type { ReviewTask } from "../../types/review";
import type { PublicationFinding } from "../../types/publication";
import type { RuntimeFindingRow } from "../../utils/taskRuntimePresentation";
import { evidenceLabel, fileMatches, severityLabel } from "../../utils/workspacePresentation";
import ReviewDiff from "../review/ReviewDiff.vue";
import CommentDraftPanel from "../review/CommentDraftPanel.vue";
import EmptyState from "../common/EmptyState.vue";
import type { FindingEntryFilter } from "../../utils/reviewSituation";
const props = defineProps<{ task: ReviewTask; findings: RuntimeFindingRow[]; focusKey: string; publicationFindings: PublicationFinding[]; initialFilter?: FindingEntryFilter; blockingKeys?: string[]; suggestionKeys?: string[] }>();
const emit = defineEmits<{ activity: [] }>();
const status = ref("all");
const severity = ref("all");
const selection = ref(props.focusKey);
const copied = ref("");
const evidenceViews = ref<FindingView[]>([]);
const dispositionReason = ref("");
const saving = ref(false);
const dispositionError = ref("");
let findingController: AbortController | null = null;
onBeforeUnmount(() => findingController?.abort());
watch(() => [props.task.id, props.task.updated_at], async () => {
  findingController?.abort();
  const pending = new AbortController(); findingController = pending;
  try { const data = await getFindings(props.task.id, pending.signal); if (!pending.signal.aborted) evidenceViews.value = data.items; }
  catch { if (!pending.signal.aborted) dispositionError.value = "证据与人工处理状态读取失败"; }
}, { immediate: true });
const authoritative = computed(() => evidenceViews.value.find(item => item.id === selected.value?.key));
async function disposition(status: FindingDisposition['status']) {
  if (!selected.value || !dispositionReason.value.trim() || saving.value) return;
  saving.value = true; dispositionError.value = "";
  try { evidenceViews.value = (await setFindingDisposition(props.task.id, selected.value.key, {status, reason: dispositionReason.value.trim()})).items; dispositionReason.value = ""; }
  catch (err) { dispositionError.value = err instanceof Error ? err.message : "保存失败"; }
  finally { saving.value = false; }
}
function effectiveSeverity(row: RuntimeFindingRow) {
  return props.publicationFindings.find(item => item.finding_key === row.key)?.severity
    ?? evidenceViews.value.find(item => item.id === row.key)?.severity ?? row.issue?.severity;
}
const visible = computed(() => props.findings.filter(row => {
  const human = row.issue?.requires_human_confirmation || row.status === "needs_human";
  const stateMatch = status.value === "all" || (status.value === "human" ? human : status.value === "unresolved" ? row.group === "unresolved" || row.status === "needs_human" : status.value === "insufficient" ? row.group === "unresolved" && !human : row.group === status.value);
  const level = effectiveSeverity(row);
  const severityMatch = severity.value === "all" || (severity.value === "blockers"
    ? props.blockingKeys ? props.blockingKeys.includes(row.key) : level === "high" || level === "critical"
    : severity.value === "suggestions" ? props.suggestionKeys ? props.suggestionKeys.includes(row.key) : level === "medium" || level === "low"
      : level === severity.value);
  return stateMatch && severityMatch;
}));
const selected = computed(() => visible.value.find(row => row.key === selection.value) || visible.value[0] || null);
const issue = computed(() => selected.value?.issue || null);
const file = computed(() => props.task.changed_files.find(item => fileMatches(item, selected.value?.filePath || "")) || null);
const draft = computed(() => props.publicationFindings.find(item => item.finding_key === selected.value?.key || item.candidate_id === issue.value?.id));
watch([() => props.focusKey, () => props.initialFilter], ([key, filter]) => {
  selection.value = key;
  status.value = filter === "unresolved" ? "unresolved" : filter === "blockers" || filter === "suggestions" ? "confirmed" : "all";
  severity.value = filter === "blockers" || filter === "suggestions" ? filter : "all";
}, { immediate: true });
watch(() => selected.value?.key, () => { copied.value = ""; });
async function copyConclusion() {
  const row = selected.value;
  if (!row) return;
  try { await navigator.clipboard.writeText([row.title, evidenceLabel(row.status, row.issue), row.affectedBehavior, row.failureScenario, row.recommendation, row.reason].filter(Boolean).join("\n\n")); copied.value = "结论已复制"; }
  catch { copied.value = "复制失败，请通过报告导出。"; }
}
</script>
<template>
  <div class="page-head"><div><h1>问题与证据</h1><p class="title-sub">先阅读结论，再结合代码证据和未验证的假设判断是否需要处理。</p></div><span class="badge blue">{{ findings.length }} 条发现记录</span></div>
  <div class="review-workspace rg-findings">
    <section class="card card-pad rg-finding-list"><div class="card-header"><div><h2>发现列表</h2><p>严重程度与证据状态独立展示</p></div></div>
      <div class="rg-filters"><select v-model="status" class="select" aria-label="证据状态筛选"><option value="all">全部证据状态</option><option value="confirmed">静态核验已确认</option><option value="unresolved">未决问题 / 待核验</option><option value="human">待人工确认</option><option value="insufficient">证据不足 / 待核验</option><option value="dismissed">已排除</option><option value="unknown">状态未知 / 仅定位</option></select><select v-model="severity" class="select" aria-label="严重程度筛选"><option value="all">全部级别</option><option value="blockers">阻断问题（严重 / 高）</option><option value="suggestions">改进建议（中 / 低）</option><option value="critical">严重</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></div>
      <button v-for="row in visible" :key="row.key" type="button" class="rg-finding-row" :class="{ active: selected?.key === row.key }" :aria-pressed="selected?.key === row.key" @click="selection = row.key"><span class="hstack wrap"><span class="badge" :class="effectiveSeverity(row) === 'high' || effectiveSeverity(row) === 'critical' ? 'danger' : 'neutral'">{{ severityLabel(effectiveSeverity(row)) }}</span><span class="badge" :class="row.group === 'confirmed' ? 'blue' : row.group === 'unresolved' ? 'warn' : 'neutral'">{{ evidenceLabel(row.status, row.issue) }}</span></span><strong>{{ row.title }}</strong><code>{{ row.filePath || '尚无可核验代码定位' }}{{ row.line ? `:${row.line}` : '' }}</code></button>
      <EmptyState v-if="!visible.length" icon="search" title="当前分类没有发现" description="可切换筛选；没有发现不表示所有路径均已验证。" />
    </section>
    <ReviewDiff :file="file" :evidence="issue?.primary_evidence" />
    <aside v-if="selected" class="card card-pad rg-evidence"><div class="hstack wrap"><span class="badge" :class="selected.group === 'confirmed' ? 'blue' : 'warn'">{{ evidenceLabel(selected.status, issue) }}</span><span v-if="issue" class="badge blue">{{ issue.category }}</span></div><h3>{{ selected.title }}</h3><code class="muted break-anywhere">{{ selected.filePath || '位置未知' }}{{ selected.line ? `:${selected.line}` : '' }}</code>
      <dl><div><dt>可能影响</dt><dd>{{ selected.affectedBehavior || '此记录未提供影响说明。' }}</dd></div><div><dt>触发条件</dt><dd>{{ selected.failureScenario || '此记录未提供触发条件。' }}</dd></div><div v-if="selected.reason"><dt>结论限制</dt><dd>{{ selected.reason }}</dd></div><div><dt>处理建议</dt><dd>{{ selected.recommendation || '请结合上下文补充人工判断。' }}</dd></div><div v-if="issue?.assumptions.length"><dt>判断依赖的假设</dt><dd><ul><li v-for="assumption in issue.assumptions" :key="assumption">{{ assumption }}</li></ul></dd></div><div><dt>证据语义</dt><dd>静态核验、代码定位与动态验证是独立维度；此结论不代表测试复现或业务验收通过。</dd></div></dl>
      <details v-if="issue"><summary>定位与技术详情</summary><p>{{ issue.primary_evidence.resolution_status }} · {{ issue.primary_evidence.resolution_method }}</p><p>模型置信度 {{ Math.round(issue.confidence * 100) }}%（未经校准）</p><p v-if="issue.related_tests.length">相关测试：{{ issue.related_tests.join('、') }}（不表示已执行）</p><article v-for="(evidence, index) in issue.supporting_evidence" :key="index"><code>{{ evidence.file_path }}</code><pre>{{ evidence.existing_code }}</pre></article></details><details v-if="selected.rawReason"><summary>原始原因</summary><p>{{ selected.rawReason }}</p></details><CommentDraftPanel v-if="draft" :finding="draft" :review-id="task.id" :reviewed-head-sha="task.run_manifest?.head_sha || null" />
      <section class="setting-section"><h3>人工处理</h3><p>证据：{{ authoritative?.evidence_status || 'unknown' }} · 动态验证：{{ authoritative?.dynamic_validation_status || 'unknown' }}</p><p v-if="authoritative?.human_confirmation_reason">待人工确认原因：{{ authoritative.human_confirmation_reason }}</p><p>处理状态：{{ authoritative?.disposition?.status || 'open' }} {{ authoritative?.disposition?.reason }}</p><p class="field-hint">人工确认、忽略与已处理单独保存，不改变机器核验结论或 GitHub 发布资格。</p><textarea v-model="dispositionReason" class="input" aria-label="人工处理原因" placeholder="填写判断依据或处理说明" maxlength="2000" /><div class="hstack wrap"><button v-for="item in [{id:'confirmed', label:'人工确认'}, {id:'ignored', label:'忽略'}, {id:'resolved', label:'已处理'}, {id:'open', label:'重新打开'}] as const" :key="item.id" type="button" class="btn secondary sm" :disabled="saving || !dispositionReason.trim() || !authoritative" @click="disposition(item.id)">{{ item.label }}</button></div><p v-if="dispositionError" class="input-error" role="alert">{{ dispositionError }}</p></section>
      <div class="hstack wrap space-top"><button type="button" class="btn secondary sm" @click="copyConclusion">复制结论</button><button type="button" class="btn soft sm" @click="emit('activity')">查看技术记录</button></div><p v-if="copied" class="small space-top" role="status">{{ copied }}</p>
    </aside>
    <section v-else class="card card-pad"><EmptyState icon="context" title="没有选中的发现" description="筛选后选择一条发现，查看证据、影响与建议。" /></section>
  </div>
</template>
