<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { setFindingDisposition } from '../../api/client';
import type { ReviewTask, ReviewLimitation } from '../../types/review';
import type { FindingDisposition, FindingView } from '../../types/reviewPlan';
import { terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import EmptyState from '../common/EmptyState.vue';
import StatusBadge from '../common/StatusBadge.vue';
import ReviewDiff from '../review/diff/ReviewDiff.vue';
const props = defineProps<{ task: ReviewTask; findings: FindingView[]; limitations: ReviewLimitation[]; focusKey?: string; initialFilter?: string; loading?: boolean }>();
const emit = defineEmits<{ updated: [items: FindingView[]]; file: [path: string] }>();
const filter = ref(props.initialFilter || 'all'), severity = ref('all'), query = ref(''), selection = ref(props.focusKey || '');
const reason = ref(''), status = ref<FindingDisposition['status']>('confirmed'), saving = ref(false), error = ref('');
let generation = 0;
onBeforeUnmount(() => { generation++; });
watch(() => props.task.id, () => { generation++; selection.value = ''; reason.value = ''; error.value = ''; saving.value = false; });
watch(() => props.focusKey, key => { if (key) { selection.value = key; filter.value = 'all'; severity.value = 'all'; query.value = ''; } });
watch(() => props.initialFilter, value => { filter.value = value || 'all'; });
const visible = computed(() => props.findings.filter(item => {
  const state = filter.value === 'all' || (filter.value === 'unresolved'
    ? ['candidate','unresolved','unknown'].includes(item.review_status) : item.review_status === filter.value);
  return state && (severity.value === 'all' || item.severity === severity.value)
    && `${item.title ?? ''} ${item.location?.path ?? ''}`.toLowerCase().includes(query.value.toLowerCase());
}));
const selected = computed(() => visible.value.find(item => item.id === selection.value) || visible.value[0]);
const verification = computed(() => props.task.review_report?.decisions.find(item => item.candidate_id === selected.value?.id));
const openLimitations = computed(() => props.limitations.filter(item => ['pending','unresolved'].includes(item.status)));
const fileIndex = computed(() => props.task.changed_files.findIndex(file => file.file_path === selected.value?.location?.path));
const attribution = { introduced:'本次 PR 引入', exposed:'本次 PR 暴露或扩大影响', pre_existing:'既有问题（非本次新增）', uncertain:'归因尚未确定' };
watch(() => selected.value?.id, () => { reason.value = ''; error.value = ''; });
async function save() {
  const item = selected.value;
  if (!item || !reason.value.trim() || saving.value || !terminal(props.task.status)) return;
  const owner = generation; saving.value = true; error.value = '';
  try {
    const result = await setFindingDisposition(props.task.id,item.id,{status:status.value,reason:reason.value.trim()});
    if (owner === generation) { emit('updated',result.items); if (selected.value?.id === item.id) reason.value = ''; }
  } catch (cause) { if (owner === generation && selected.value?.id === item.id) error.value = cause instanceof Error ? cause.message : '保存注记失败'; }
  finally { if (owner === generation) saving.value = false; }
}
</script>
<template>
  <div class="page-head"><div><h2>问题与证据</h2><p class="title-sub">候选、核验结论与人工处理分别记录；人工注记不改变发布资格。</p></div><span class="badge neutral">{{ findings.length }} 条记录</span></div>
  <section v-if="openLimitations.length" class="card card-pad space-bottom">
    <h3>审查待核实事项</h3><p class="muted small">这些事项独立于代码缺陷计数；必要性复核未完成，不代表已确认存在代码问题。</p>
    <article v-for="item in openLimitations" :key="item.id" class="plan-file">
      <span class="badge neutral">{{ item.status === 'pending' ? '限制声明尚待复核' : '必要证据缺口' }}</span>
      <p>{{ item.text }}</p>
      <p v-if="item.assessment?.dependent_conclusion" class="small">影响的结论：{{ item.assessment.dependent_conclusion }}</p>
      <p v-if="item.assessment?.change_relevance" class="small">与本次变更的关联：{{ item.assessment.change_relevance }}</p>
      <p v-if="item.assessment?.next_check" class="small">需要补查：{{ item.assessment.next_check }}</p>
    </article>
  </section>
  <div class="workspace-toolbar rg-finding-filters"><input v-model="query" class="input" placeholder="搜索问题或文件" aria-label="搜索问题" /><select v-model="filter" class="select" aria-label="核验状态"><option value="all">全部核验状态</option><option value="confirmed">已确认</option><option value="unresolved">待核实</option><option value="dismissed">已排除</option></select><select v-model="severity" class="select" aria-label="风险等级"><option value="all">全部风险等级</option><option value="critical">严重</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></div>
  <p v-if="loading" class="muted space-top" role="status">正在读取问题记录…</p>
  <EmptyState v-else-if="!visible.length" icon="shield" title="没有匹配的问题记录" :description="openLimitations.length ? '尚无匹配的代码缺陷记录；仍需处理上方的审查待核实事项。' : '可以调整筛选条件；未发现问题不能证明源码范围已经完整检查。'" />
  <div v-else class="rg-restored-findings space-top">
    <aside class="card card-pad rg-findings-list"><button v-for="item in visible" :key="item.id" class="rg-finding-row" :class="{ active: selected?.id === item.id }" @click="selection = item.id"><span class="rg-inline-badges"><StatusBadge :status="item.severity || 'unknown'" /><StatusBadge :status="item.review_status" /></span><strong>{{ item.title || '未命名候选' }}</strong><code>{{ item.location?.path || '位置尚未确定' }}</code></button></aside>
    <article v-if="selected" class="section-stack">
      <section class="card card-pad"><div class="card-header"><h2>{{ selected.title || '候选问题' }}</h2><StatusBadge :status="selected.evidence_status" /></div><p v-if="selected.location" class="small"><button class="link" @click="$emit('file',selected.location.path)">{{ selected.location.path }}:{{ selected.location.start_line }}</button> · {{ selected.location.side === 'left' ? 'BASE' : 'HEAD' }}</p>
        <dl class="rg-finding-facts"><div><dt>问题与触发</dt><dd>{{ selected.message || '未记录' }}</dd></div><div><dt>影响</dt><dd>{{ selected.impact || '未记录' }}</dd></div><div><dt>变更归因</dt><dd>{{ attribution[selected.attribution] }}</dd></div><div><dt>建议</dt><dd>{{ selected.recommendation || '未记录' }}</dd></div><div><dt>独立核验</dt><dd>{{ verification?.explanation || '尚无完整核验说明' }}</dd></div></dl>
        <ul v-if="selected.limitations.length" class="rg-limitations"><li v-for="limit in selected.limitations" :key="limit">{{ limit }}</li></ul>
        <details v-if="verification?.boundary_notes?.length" class="space-top"><summary class="link">一般验证边界</summary><ul><li v-for="note in verification.boundary_notes" :key="note">{{ note }}</li></ul><p class="muted small">这些边界不等同于影响当前裁决的必要证据缺口。</p></details>
        <details class="space-top"><summary class="link">证据与反证引用</summary><h3 class="space-top">支持证据</h3><code v-for="id in verification?.evidence_ids || []" :key="id" class="rg-evidence-id">{{ id }}</code><p v-if="!verification?.evidence_ids.length" class="muted small">未记录支持证据。</p><h3 class="space-top">反证</h3><code v-for="id in verification?.counterevidence_ids || []" :key="id" class="rg-evidence-id">{{ id }}</code><p v-if="!verification?.counterevidence_ids.length" class="muted small">未记录独立反证，不能据此推断不存在反证。</p></details>
      </section>
      <ReviewDiff v-if="fileIndex >= 0" :key="`${task.id}-${selected.id}`" :file="task.changed_files[fileIndex]!" :file-id="fileIndex" :task-id="task.id" :snapshot="task.snapshot" :highlight-line="selected.location?.start_line" :highlight-side="selected.location?.side" />
      <section class="card card-pad"><div class="card-header"><h3>人工处理</h3><AppIcon name="shield" /></div><p v-if="selected.disposition" class="notice small">已保存：{{ {open:'重新打开',confirmed:'人工确认',ignored:'忽略',resolved:'已处理'}[selected.disposition.status] }} · {{ selected.disposition.reason }}</p>
        <form class="section-stack" @submit.prevent="save"><label class="field-label">处理状态<select v-model="status" class="select" :disabled="saving"><option value="confirmed">人工确认</option><option value="ignored">忽略</option><option value="resolved">已处理</option><option value="open">重新打开</option></select></label><label class="field-label">处理原因<textarea v-model="reason" class="textarea" rows="3" required maxlength="2000" :disabled="saving" /></label><p v-if="error" class="input-error" role="alert">{{ error }}</p><div><button class="btn primary" :disabled="!reason.trim() || saving || !terminal(task.status)">{{ saving ? '保存中…' : '保存人工注记' }}</button></div></form>
      </section>
    </article>
  </div>
</template>
