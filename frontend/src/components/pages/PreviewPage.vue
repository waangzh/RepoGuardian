<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import type { PreviewJob } from '../../types/previewJob';
import AppIcon from '../common/AppIcon.vue';
import ReviewStepper from '../common/ReviewStepper.vue';
import StatusBadge from '../common/StatusBadge.vue';
import PreviewProgress from '../review/PreviewProgress.vue';

const props = defineProps<{
  job: PreviewJob | null;
  tracking: boolean;
  connectionError: string;
  prUrl: string;
  model: string;
  defaultModel: string;
}>();
const emit = defineEmits<{ resume: []; cancel: []; retry: []; back: [] }>();

const active = computed(() => props.job?.status === 'queued' || props.job?.status === 'running');
const incomplete = computed(() => Boolean(props.job && (
  ['failed', 'cancelled', 'expired'].includes(props.job.status)
  || (props.job.status === 'completed' && !props.job.result)
)));
const retryable = computed(() => incomplete.value);
const statusLabel = computed(() => {
  if (!props.job) return props.connectionError ? '读取失败' : '连接中';
  return ({ failed: '预览失败', cancelled: '已取消', expired: '已过期', completed: '未返回方案', consumed: '已启动审查' } as Record<string, string>)[props.job.status];
});
const title = computed(() => {
  if (!props.job) return props.connectionError ? '无法读取预览状态' : '正在连接预览服务';
  if (active.value) return '正在分析 PR';
  return 'PR 分析未完成';
});
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
onBeforeUnmount(() => { if (timer) clearInterval(timer); });
const elapsed = computed(() => {
  if (!props.job) return '—';
  const from = Date.parse(props.job.created_at);
  const to = props.job.completed_at ? Date.parse(props.job.completed_at) : now.value;
  if (!Number.isFinite(from) || !Number.isFinite(to)) return '—';
  const seconds = Math.max(0, Math.floor((to - from) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
});
</script>
<template>
  <ReviewStepper :current="0" />
  <div class="page-head">
    <div>
      <div class="eyebrow">STEP 01 / IMPORT PULL REQUEST</div>
      <h1 tabindex="-1">{{ title }}</h1>
      <p class="title-sub break-anywhere">{{ prUrl }}</p>
      <p class="title-sub">读取 PR 信息、准备只读快照并生成审查方案；此阶段不调用模型，也不执行目标仓库代码。</p>
    </div>
    <span v-if="active || (!job && !connectionError)" class="badge blue">零模型调用</span>
    <StatusBadge v-else-if="job" :status="job.status" :label="statusLabel" />
    <StatusBadge v-else status="failed" label="连接中断" />
  </div>
  <div class="split-70">
    <PreviewProgress :job="job" :tracking="tracking" :connection-error="connectionError" :pr-url="prUrl" />
    <aside class="plan-aside">
      <section class="card card-pad" :aria-busy="tracking">
        <div class="card-header"><div><h2>本次预览</h2><p>不会调用模型，也不会运行仓库代码</p></div><AppIcon name="shield" :size="18" /></div>
        <div class="aside-stat"><span class="muted">Pull Request</span><strong class="break-anywhere">{{ prUrl }}</strong></div>
        <div class="aside-stat"><span class="muted">执行模型</span><strong class="break-anywhere">{{ model || defaultModel || '服务端默认' }}</strong></div>
        <div class="aside-stat"><span class="muted">缓存</span><strong>{{ job ? (job.cache_hit ? '已命中' : job.status === 'completed' ? '未命中' : '等待完成') : '未知' }}</strong></div>
        <div class="aside-stat"><span class="muted">已用时间</span><strong>{{ elapsed }}</strong></div>
        <div class="divider" />
        <p class="muted small">预览在服务端继续执行。离开或刷新不会中止准备；再次打开时会恢复进度。</p>
        <div class="plan-cta">
          <button v-if="connectionError && !tracking" type="button" class="btn secondary" @click="emit('resume')">继续查询预览</button>
          <button v-if="active" type="button" class="btn secondary" @click="emit('cancel')">取消预览</button>
          <template v-if="retryable">
            <button type="button" class="btn primary" @click="emit('retry')"><AppIcon name="play" :size="16" />重新分析 PR</button>
            <button type="button" class="btn secondary" @click="emit('back')">返回修改 PR 链接</button>
          </template>
          <button v-else-if="!active && !connectionError" type="button" class="btn secondary" @click="emit('back')">返回修改 PR 链接</button>
        </div>
        <p v-if="incomplete" class="field-hint">重新分析会创建新的预览；已产生的尝试不会计入正式审查。</p>
      </section>
    </aside>
  </div>
</template>
