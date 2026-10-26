<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import type { PreviewJob } from '../../../types/previewJob';
import AppIcon from '../../common/AppIcon.vue';
import StatusBadge from '../../common/StatusBadge.vue';

type PreviewStage = PreviewJob['stages'][number];

const props = defineProps<{ job: PreviewJob | null; tracking: boolean; connectionError: string; prUrl?: string }>();
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
onBeforeUnmount(() => { if (timer) clearInterval(timer); });

const active = computed(() => props.job?.status === 'queued' || props.job?.status === 'running');
const currentStage = computed(() => {
  const stages = props.job?.stages;
  return stages && stages.length ? stages[stages.length - 1] : null;
});
const percent = computed(() => {
  const value = currentStage.value?.percent;
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
});
const titles: Record<string, string> = {
  completed: '预览已完成但未返回方案',
  failed: '预览失败',
  cancelled: '预览已取消',
  expired: '方案已过期',
  consumed: '方案已启动审查',
};
const title = computed(() => {
  if (!props.job) return props.connectionError ? '无法读取预览状态' : '正在连接预览服务';
  if (active.value) return currentStage.value?.message || '正在准备预览';
  return titles[props.job.status] || '预览状态未知';
});

function elapsed(start?: string | null, end?: string | null) {
  if (!start) return '—';
  const from = Date.parse(start);
  if (!Number.isFinite(from)) return '—';
  const to = end ? Date.parse(end) : now.value;
  if (!Number.isFinite(to)) return '—';
  const seconds = Math.max(0, Math.floor((to - from) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
function stageTime(stage: PreviewStage) {
  return elapsed(stage.started_at, stage.finished_at || (active.value ? null : props.job?.completed_at));
}
function stageStatus(stage: PreviewStage, index: number): string {
  if (stage.finished_at) return 'completed';
  if (props.job?.status === 'failed') return 'failed';
  if (props.job?.status === 'cancelled') return 'cancelled';
  if (active.value && index === (props.job?.stages.length ?? 0) - 1) return 'running';
  return 'pending';
}
function stageText(stage: PreviewStage) {
  // Git 进度消息本身已包含百分比时不再重复拼接。
  return stage.percent !== null && !stage.message.includes('%') ? `${stage.message} · ${stage.percent}%` : stage.message;
}
</script>
<template>
  <div class="section-stack">
    <section class="card card-pad" :aria-busy="active" aria-label="PR 预览进度">
      <div class="card-header">
        <div><h2 role="status" aria-live="polite">{{ title }}</h2><p v-if="prUrl || job" class="break-anywhere">{{ prUrl || job?.request.pr_url }}</p></div>
        <span v-if="job" class="muted small">总耗时 {{ elapsed(job.created_at, job.completed_at) }}</span>
      </div>
      <template v-if="job">
        <strong class="plan-number">{{ percent == null ? '处理中…' : `${percent}%` }}</strong>
        <p class="muted small">当前步骤进度，不是整轮审查或覆盖完成率。</p>
        <div class="progress space-top" :class="{ 'rg-indeterminate': percent == null }" role="progressbar" aria-label="PR 预览准备进度" :aria-valuenow="percent ?? undefined" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: percent == null ? '35%' : `${percent}%` }" /></div>
        <p class="muted small space-top">预览在服务端继续执行，刷新后会恢复进度。大型仓库首次准备可能需要数分钟。</p>
        <div v-if="job.cache_hit" class="notice good"><AppIcon name="check-circle" :size="16" /><div>已复用相同提交的仓库输入与索引。</div></div>
        <div v-if="job.error" class="notice danger" role="alert">{{ job.error }}</div>
        <div class="notice"><AppIcon name="shield" :size="16" /><div>预览阶段只读取 PR 与仓库快照，不调用模型，也不执行目标仓库代码。</div></div>
      </template>
      <p v-else class="muted small" role="status">正在读取预览状态…</p>
      <div v-if="connectionError" class="notice danger" role="alert">连接中断：{{ connectionError }}。已保留预览标识，可继续查询。</div>
    </section>
    <section v-if="job?.stages.length" class="card card-pad">
      <div class="card-header"><div><h2>准备步骤</h2><p>来自服务端保存的真实进度</p></div><span class="muted small">{{ job.stages.length }} 个阶段</span></div>
      <div class="timeline">
        <div v-for="(stage, index) in job.stages" :key="`${stage.phase}-${index}`" class="timeline-row" :class="{ current: stageStatus(stage, index) === 'running', interrupted: stageStatus(stage, index) === 'failed', cancelled: stageStatus(stage, index) === 'cancelled' }">
          <span class="timeline-dot"><AppIcon v-if="stage.finished_at" name="check" :size="13" :stroke-width="2.4" /><template v-else>{{ index + 1 }}</template></span>
          <div class="timeline-body"><strong>{{ stageText(stage) }}</strong><span class="time">{{ stageTime(stage) }}</span></div>
          <StatusBadge :status="stageStatus(stage, index)" />
        </div>
      </div>
    </section>
  </div>
</template>
<style scoped>
.timeline-row.interrupted .timeline-dot { background: var(--dangerWash); color: var(--danger); }
.timeline-row.cancelled .timeline-dot { background: var(--surface3); color: var(--muted); }
</style>
