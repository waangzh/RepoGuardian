<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import type { ReviewTask } from '../../types/review';
import type { FindingView } from '../../types/reviewPlan';
import type { PublicationPlan } from '../../types/publication';
import { getFindings, getPublicationPlan, getReport, getPrDiscussions, getFileContext, setFindingDisposition } from '../../api/client';
import { observedUsage, phaseLabel, reviewerLabel, safeGitHubLink, terminal } from '../../utils/nativeReview';
import ReportMarkdown from '../report/ReportMarkdown.vue';
import GitHubPublicationPanel from './publication/GitHubPublicationPanel.vue';
import StatusBadge from '../common/StatusBadge.vue';

const props = defineProps<{ task: ReviewTask }>();
const emit = defineEmits<{ cancel: []; resume: []; rereview: []; }>();
const tab = ref('overview');
const tabs = [{id:'overview',label:'概览'},{id:'findings',label:'问题与证据'},{id:'scope',label:'审查范围'},{id:'files',label:'变更文件'},{id:'activity',label:'Agent 活动'},{id:'discussions',label:'PR 讨论'},{id:'report',label:'报告与交付'}];
const plan = ref<PublicationPlan | null>(null);
const findings = ref<FindingView[]>([]);
const report = ref('');
const error = ref('');
const loading = ref(false);
const selectedFile = ref(0);
const context = ref<{base_lines:string[];head_lines:string[]} | null>(null);
const contextLoading = ref(false);
const contextError = ref('');
const discussions = ref<Awaited<ReturnType<typeof getPrDiscussions>> | null>(null);
const dispositionTarget = ref<FindingView | null>(null);
const disposition = ref<'open'|'confirmed'|'ignored'|'resolved'>('open');
const reason = ref('');
let controller: AbortController | null = null;
let contextController: AbortController | null = null;
let discussionController: AbortController | null = null;
let generation = 0;
const summary = computed(() => props.task.review_report);
const usage = computed(() => observedUsage(summary.value));
const file = computed(() => props.task.changed_files[selectedFile.value]);
const activeAgents = computed(() => terminal(props.task.status) ? [] : props.task.steps.filter(step => step.status === 'running'));
const decisions = computed(() => new Map(summary.value?.decisions.map(item => [item.candidate_id,item]) ?? []));
const headline = computed(() => plan.value?.digest.merge_decision?.label ?? (props.task.status === 'failed' ? '审查中断，尚无完整结论' : props.task.status === 'cancelled' ? '审查已停止' : terminal(props.task.status) ? '审查结果加载中' : '正在独立探索变更风险'));
async function load() {
  const id = props.task.id, owner = ++generation;
  controller?.abort(); controller = new AbortController();
  loading.value = true; error.value = '';
  try {
    const [nextPlan,nextFindings,nextReport] = await Promise.all([getPublicationPlan(id,controller.signal),getFindings(id,controller.signal),getReport(id,controller.signal).catch(() => '')]);
    if (owner !== generation) return;
    plan.value = nextPlan; findings.value = nextFindings.items; report.value = nextReport || nextPlan.user_report_markdown;
  } catch (e) { if (owner === generation && !controller.signal.aborted) error.value = e instanceof Error ? e.message : '结果读取失败'; }
  finally { if (owner === generation) loading.value = false; }
}
watch(() => props.task.id, () => { generation++; controller?.abort(); contextController?.abort(); discussionController?.abort(); plan.value = null; findings.value = []; report.value = ''; discussions.value = null; context.value = null; error.value = ''; selectedFile.value = 0; tab.value = 'overview'; dispositionTarget.value = null; });
watch(() => [props.task.id, props.task.status, props.task.updated_at], () => { if (terminal(props.task.status)) void load(); }, {immediate:true});
watch(selectedFile, () => { contextController?.abort(); context.value = null; contextError.value = ''; });
async function readContext() {
  contextController?.abort(); const owner = new AbortController(); contextController = owner;
  contextLoading.value = true; contextError.value = '';
  try { const result = await getFileContext(props.task.id,selectedFile.value,owner.signal); if (!owner.signal.aborted) context.value = result; }
  catch(e) { if (!owner.signal.aborted) contextError.value = e instanceof Error ? e.message : '固定源码读取失败'; }
  finally { if (contextController === owner) contextLoading.value = false; }
}
async function readDiscussions() {
  discussionController?.abort(); const owner = new AbortController(); discussionController = owner;
  try { const result = await getPrDiscussions(props.task.id,owner.signal); if (!owner.signal.aborted) discussions.value = result; }
  catch(e) { if (!owner.signal.aborted) error.value = e instanceof Error ? e.message : '讨论读取失败'; }
}
watch(tab, value => { if (value === 'discussions' && !discussions.value) void readDiscussions(); });
async function saveDisposition() {
  if (!dispositionTarget.value || !reason.value.trim()) return;
  const id = props.task.id, owner = generation;
  try { const result = await setFindingDisposition(id,dispositionTarget.value.id,{status:disposition.value,reason:reason.value}); if (owner !== generation) return; findings.value = result.items; dispositionTarget.value = null; reason.value = ''; await load(); }
  catch(e) { if (owner === generation) error.value = e instanceof Error ? e.message : '保存人工注记失败'; }
}
function exportSnapshotReport() {
  const content = report.value || plan.value?.user_report_markdown;
  if (!content) return;
  const url = URL.createObjectURL(new Blob([content], {type:'text/markdown;charset=utf-8'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = `RepoGuardian-${props.task.id}-${props.task.snapshot.head_sha.slice(0,8)}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
onBeforeUnmount(() => { generation++; controller?.abort(); contextController?.abort(); discussionController?.abort(); });
</script>
<template>
  <section class="native-review">
    <div class="native-title"><div><p class="native-eyebrow">{{ task.pr.owner }}/{{ task.pr.repo }} · PR #{{ task.pr.number }}</p><h1>{{ task.pr.title }}</h1><p><StatusBadge :status="task.status" /> <span class="native-muted">{{ phaseLabel(task.phase) }}</span></p></div><div class="native-actions"><button v-if="!terminal(task.status)" class="button button--secondary" @click="emit('cancel')">停止审查</button><button v-if="task.status === 'failed'" class="button button--secondary" @click="emit('resume')">从检查点继续</button><button class="button button--secondary" @click="emit('rereview')">重新审查此 PR</button></div></div>
    <nav class="native-tabs" aria-label="审查内容"><button v-for="item in tabs" :key="item.id" :aria-current="tab === item.id ? 'page' : undefined" @click="tab=item.id">{{ item.label }}</button></nav>
    <p v-if="error" class="native-error" role="alert">{{ error }} <button @click="load">重试读取</button></p>
    <p v-if="task.error" class="native-error">执行原因：{{ task.error }}；未知与缺失不代表审查通过。</p>
    <template v-if="tab === 'overview'">
      <section class="native-panel native-verdict"><p class="native-eyebrow">本次静态审查</p><h2>{{ headline }}</h2><p>{{ plan?.digest.merge_decision?.advice || '各审查视角独立读取固定源码，候选经过独立核验后才可能进入发布草稿。' }}</p><div class="native-metrics"><div><strong>{{ summary?.findings.length ?? '—' }}</strong><span>已确认问题</span></div><div><strong>{{ summary?.candidates.length ?? '—' }}</strong><span>发现候选</span></div><div><strong>{{ summary ? `${summary.goals.filter(g=>g.status==='examined').length}/${summary.goals.length}` : '—' }}</strong><span>源码范围已检查</span></div><div><strong>{{ usage.tokens ?? '未知' }}</strong><span>观测 Token 用量</span></div></div><p class="native-muted">代码覆盖、缺陷核验和发布状态分别记录；未发现问题不等于证明代码正确。未执行目标仓库的测试、构建或脚本。</p></section>
      <section v-if="activeAgents.length" class="native-panel"><h2>当前工作</h2><p v-for="step in activeAgents" :key="step.name"><span class="native-pulse" />{{ phaseLabel(step.message) }} · {{ step.name.slice(0,10) }}</p></section>
      <section v-if="plan" class="native-panel"><h2>变更与限制</h2><ul><li v-for="change in plan.digest.actual_changes" :key="change">{{ change }}</li><li v-for="limit in plan.digest.merge_decision?.limits" :key="limit">{{ limit }}</li></ul><button class="button button--primary" @click="tab='report'">查看报告与交付</button></section>
      <details class="native-panel"><summary>固定快照与执行边界</summary><dl class="native-facts"><div><dt>HEAD</dt><dd><code>{{ task.snapshot.head_sha }}</code></dd></div><div><dt>有效 BASE</dt><dd><code>{{ task.snapshot.effective_base_sha }}</code></dd></div><div><dt>模型</dt><dd>{{ task.configuration.provider }} / {{ task.configuration.model }}</dd></div><div><dt>观测模型调用</dt><dd>{{ usage.calls ?? '未知' }}</dd></div></dl></details>
    </template>
    <section v-else-if="tab === 'findings'" class="native-panel"><h2>问题与核验</h2><p v-if="!findings.length">{{ loading ? '正在加载…' : '没有已保存的问题记录；这不代表检查范围完整。' }}</p><article v-for="finding in findings" :key="finding.id" class="native-finding"><div class="native-actions"><StatusBadge :status="finding.review_status" /><StatusBadge :status="finding.severity || 'unknown'" /></div><h3>{{ finding.title }}</h3><p><code>{{ finding.location?.path || summary?.candidates.find(c=>c.id===finding.id)?.affected_path }}</code><span v-if="finding.location">:{{ finding.location.start_line }}</span></p><p>{{ finding.message }}</p><p><b>变更归因：</b>{{ ({introduced:"本次引入",exposed:"本次暴露或扩大影响",pre_existing:"既有问题（非本次新增）",uncertain:"尚未确定"})[finding.attribution] }}</p><p><b>影响：</b>{{ finding.impact }}</p><p><b>建议：</b>{{ finding.recommendation }}</p><p v-for="limit in finding.limitations" :key="limit" class="native-muted">{{ limit }}</p><p v-if="finding.disposition">人工注记：{{ finding.disposition.status }} · {{ finding.disposition.reason }}（不改变机器核验与发布资格）</p><button class="button button--ghost" @click="dispositionTarget=finding;reason=''">添加人工注记</button><details><summary>来源与可信证据</summary><code v-for="id in decisions.get(finding.id)?.evidence_ids || []" :key="id" class="native-code-line">{{ id }}</code></details></article>
      <form v-if="dispositionTarget" @submit.prevent="saveDisposition" class="native-panel native-form"><h3>人工处理：{{ dispositionTarget.title }}</h3><label>状态<select v-model="disposition"><option value="open">重新打开</option><option value="confirmed">人工确认</option><option value="ignored">忽略</option><option value="resolved">已处理</option></select></label><label>原因<textarea v-model="reason" required maxlength="2000" /></label><div class="native-actions"><button class="button button--primary">保存注记</button><button type="button" class="button button--secondary" @click="dispositionTarget=null">取消</button></div></form>
    </section>
    <section v-else-if="tab === 'scope'" class="native-panel"><h2>范围与覆盖</h2><p>同一源码范围不会因多个 Reviewer 重复计入分母。完成读取不自动证明缺陷已核验。</p><div class="native-table-wrap"><table><thead><tr><th>文件 / 侧别</th><th>区间</th><th>状态</th></tr></thead><tbody><tr v-for="goal in summary?.goals" :key="goal.id"><td><code>{{ goal.path }}</code> · {{ goal.side }}</td><td>{{ goal.start_line }}–{{ goal.end_line }}</td><td>{{ goal.status === 'examined' ? '已检查' : goal.status === 'blocked' ? '受阻' : '未检查' }}<small v-if="goal.reason"> · {{ goal.reason }}</small></td></tr></tbody></table></div><p v-if="!summary">审查尚未形成最终覆盖记录。</p><p v-for="excluded in plan?.digest.excluded_files" :key="excluded.path">未纳入：{{ excluded.path }} · {{ excluded.reason }}</p></section>
    <section v-else-if="tab === 'files'" class="native-panel"><h2>固定快照变更</h2><label>文件<select v-model="selectedFile"><option v-for="(entry,index) in task.changed_files" :key="entry.file_path" :value="index">{{ entry.file_path }} +{{ entry.additions }} / -{{ entry.deletions }}</option></select></label><template v-if="file"><p v-if="file.is_binary">二进制内容不展示</p><div v-for="hunk in file.hunks" :key="hunk.hunk_id" class="native-diff"><div v-for="(line,index) in hunk.lines" :key="index" :data-kind="line.kind"><span>{{ line.old_line_no }}</span><span>{{ line.new_line_no }}</span><pre>{{ line.kind === 'added' ? '+' : line.kind === 'deleted' ? '-' : ' ' }}{{ line.content }}</pre></div></div><button class="button button--secondary" :disabled="contextLoading || file.is_binary" @click="readContext">{{ contextLoading ? '读取中…' : '读取固定 BASE / HEAD 完整上下文' }}</button><p v-if="contextError" role="alert">{{ contextError }}</p><div v-if="context" class="native-source-pair"><details open><summary>BASE</summary><pre>{{ context.base_lines.join('\n') }}</pre></details><details open><summary>HEAD</summary><pre>{{ context.head_lines.join('\n') }}</pre></details></div></template></section>
    <section v-else-if="tab === 'activity'" class="native-panel"><h2>Agent 活动</h2><p>消息历史相互隔离；父图仅汇聚结构化结果。</p><ol class="native-timeline"><li v-for="step in task.steps" :key="step.name"><StatusBadge :status="step.status" /> <b>{{ phaseLabel(step.message) }}</b><small>{{ new Date(step.updated_at).toLocaleTimeString() }}</small><details><summary>结构化进度</summary><pre>{{ JSON.stringify(step.data,null,2) }}</pre></details></li></ol><h3 v-if="summary">发现任务</h3><p v-for="assignment in summary?.assignments" :key="assignment.id">{{ reviewerLabel(assignment.reviewer) }} · {{ assignment.focus_paths.join('、') }} · {{ summary?.discovery_results.find(r=>r.id===assignment.id)?.status || '未知' }}</p></section>
    <section v-else-if="tab === 'discussions'" class="native-panel"><h2>冻结的 PR 讨论</h2><p>讨论是未经验证的历史上下文；平台 resolved / outdated 不证明代码问题已修复。</p><p v-if="!task.configuration.options.include_pr_discussions">本次方案未纳入历史讨论。</p><template v-else><p v-if="discussions">保存 {{ discussions.total }} 条线程；当前展示首批，原始采集限制保持可见。</p><p v-for="limit in discussions?.limitations" :key="limit">{{ limit }}</p><article v-for="thread in discussions?.items" :key="thread.id" class="native-finding"><b>{{ thread.source }} · {{ thread.path || 'PR 讨论' }}</b><small>resolved: {{ thread.resolved ?? '未知' }} / outdated: {{ thread.outdated ?? '未知' }}</small><p v-for="comment in thread.comments" :key="comment.id" class="native-discussion">{{ comment.body }} <a v-if="safeGitHubLink(comment.url)" :href="safeGitHubLink(comment.url)" target="_blank" rel="noreferrer">查看原文</a></p></article></template></section>
    <section v-else-if="tab === 'report'" class="native-panel"><h2>报告与交付</h2><p v-if="!terminal(task.status)">审查尚未结束；结束后才可生成发布草稿。</p><p v-else-if="loading">正在加载报告…</p><template v-if="plan"><button class="button button--secondary" @click="exportSnapshotReport">导出此快照报告</button><p class="native-muted">导出包含固定提交与审查限制，不表示已核对最新 PR。</p><GitHubPublicationPanel :review-id="task.id" :plan="plan" /><ReportMarkdown :content="report || plan.user_report_markdown" /></template></section>
  </section>
</template>
