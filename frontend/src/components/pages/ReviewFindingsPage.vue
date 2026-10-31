<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { updateFindingDisposition } from '../../api/client';
import type { Candidate, Finding, FindingDisposition, FindingDispositions, Run } from '../../types/review';
import { terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import EmptyState from '../common/EmptyState.vue';
import StatusBadge from '../common/StatusBadge.vue';

const props = defineProps<{
  task: Run;
  findings: Finding[];
  candidates: Candidate[];
  dispositions: FindingDispositions;
  loading?: boolean;
}>();
const emit = defineEmits<{ 'update:dispositions': [value: FindingDispositions] }>();

const query = ref('');
const selected = ref('');
const status = ref<FindingDisposition['status']>('open');
const note = ref('');
const saving = ref(false);
const error = ref('');
let generation = 0;

const visible = computed(() => props.findings.filter(item => (
  item.claim + ' ' + (item.location?.path || '')
).toLowerCase().includes(query.value.toLowerCase())));
const current = computed(() => visible.value.find(item => item.id === selected.value) || visible.value[0]);
const currentDisposition = computed(() => current.value ? props.dispositions[current.value.id] : undefined);
const canSave = computed(() => Boolean(current.value) && terminal(props.task.status) && !saving.value);

const attribution = {
  introduced: '本次 PR 引入',
  exposed: '本次 PR 暴露或扩大影响',
  pre_existing: '既有问题（非本次新增）',
  uncertain: '归因尚未确定',
};

function resetDispositionForm() {
  const disposition = currentDisposition.value;
  status.value = disposition?.status ?? 'open';
  note.value = disposition?.note ?? '';
  error.value = '';
}

watch(() => props.task.id, () => {
  generation += 1;
  selected.value = '';
  saving.value = false;
  resetDispositionForm();
});
watch(() => current.value?.id, resetDispositionForm);
onBeforeUnmount(() => { generation += 1; });

async function saveDisposition() {
  const finding = current.value;
  if (!finding || !canSave.value) return;

  const owner = generation;
  saving.value = true;
  error.value = '';
  try {
    const result = await updateFindingDisposition(props.task.id, finding.id, {
      status: status.value,
      note: note.value.trim() || null,
    });
    if (owner === generation && current.value?.id === finding.id) {
      emit('update:dispositions', result.dispositions);
    }
  } catch (cause) {
    if (owner === generation && current.value?.id === finding.id) {
      error.value = cause instanceof Error ? cause.message : '保存人工注记失败';
    }
  } finally {
    if (owner === generation) saving.value = false;
  }
}
</script>

<template>
  <div class="page-head">
    <div>
      <h2>问题与证据</h2>
      <p class="title-sub">仅展示通过准入的 Finding；候选和证据不足保持独立可见。</p>
    </div>
    <span class="badge neutral">{{ findings.length }} 条 Finding</span>
  </div>

  <div class="workspace-toolbar rg-finding-filters">
    <input v-model="query" class="input" placeholder="搜索问题或文件" aria-label="搜索问题" />
    <span class="muted small">候选 {{ candidates.length }} 条</span>
  </div>

  <p v-if="loading" class="muted space-top" role="status">正在读取问题记录…</p>
  <section v-else-if="!visible.length" class="card card-pad space-top">
    <EmptyState
      icon="shield"
      title="没有匹配的已准入问题"
      :description="candidates.length ? '仍有候选或证据不足项，不能据此推断不存在缺陷。' : '未发现问题不能证明审查范围完整。'"
    />
  </section>

  <div v-else class="rg-restored-findings space-top">
    <aside class="card card-pad rg-findings-list">
      <button v-for="item in visible" :key="item.id" class="rg-finding-row" :class="{ active: current?.id === item.id }" @click="selected = item.id">
        <span class="rg-inline-badges"><StatusBadge :status="item.severity" /></span>
        <strong>{{ item.claim }}</strong>
        <code>{{ item.location?.path || '位置尚未确定' }}</code>
      </button>
    </aside>

    <article v-if="current" class="section-stack">
      <section class="card card-pad">
        <div class="card-header">
          <h2>{{ current.claim }}</h2>
          <StatusBadge :status="current.severity" />
        </div>
        <p v-if="current.location" class="small">
          <code>{{ current.location.path }}:{{ current.location.line }}</code>
          · {{ current.location.side === 'base' ? 'BASE' : 'HEAD' }}
        </p>
        <dl class="rg-finding-facts">
          <div><dt>触发条件</dt><dd>{{ current.trigger }}</dd></div>
          <div><dt>影响</dt><dd>{{ current.impact }}</dd></div>
          <div><dt>审查意见</dt><dd class="rg-preserve-lines">{{ current.author_comment }}</dd></div>
        </dl>
        <p class="small">变更归因：{{ attribution[current.attribution] }}</p>
        <details class="space-top">
          <summary class="link">来源证据</summary>
          <code v-for="source in current.sources" :key="source.id" class="rg-evidence-id">{{ source.side.toUpperCase() }} · {{ source.path }}:{{ source.start_line }}–{{ source.end_line }} · {{ source.id }}</code>
        </details>
        <p v-if="!current.publishable" class="notice small">该 Finding 不具备发布资格，发布面板会按服务端计划进一步校验。</p>
      </section>

      <section class="card card-pad">
        <div class="card-header">
          <h3>人工注记</h3>
          <AppIcon name="shield" />
        </div>
        <p class="muted small">人工状态与机器 Finding、证据判定及发布准入分离保存，不会隐藏或改写机器结论。</p>
        <p v-if="currentDisposition" class="notice small">
          当前状态：{{ { open: '打开', resolved: '已处理', dismissed: '已忽略' }[currentDisposition.status] }}
          <template v-if="currentDisposition.note"> · {{ currentDisposition.note }}</template>
        </p>
        <form class="section-stack" @submit.prevent="saveDisposition">
          <label class="field-label">
            人工状态
            <select v-model="status" class="select" :disabled="!terminal(task.status) || saving">
              <option value="open">打开</option>
              <option value="resolved">已处理</option>
              <option value="dismissed">已忽略</option>
            </select>
          </label>
          <label class="field-label">
            处理注记
            <textarea v-model="note" class="textarea" rows="3" maxlength="2000" :disabled="!terminal(task.status) || saving" />
          </label>
          <p v-if="!terminal(task.status)" class="muted small">仅终态审查允许保存人工注记。</p>
          <p v-if="error" class="input-error" role="alert">{{ error }}</p>
          <div><button class="btn primary" :disabled="!canSave">{{ saving ? '保存中…' : '保存人工注记' }}</button></div>
        </form>
      </section>
    </article>
  </div>
</template>
