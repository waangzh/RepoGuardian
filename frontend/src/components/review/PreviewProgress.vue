<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import type { PreviewJob } from '../../types/previewJob';
const props = defineProps<{ job: PreviewJob | null; tracking: boolean; connectionError: string }>();
defineEmits<{ resume: []; cancel: [] }>();
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
onBeforeUnmount(() => { if (timer) clearInterval(timer); });
const active = computed(() => props.job?.status === 'queued' || props.job?.status === 'running');
function elapsed(start: string, end?: string | null) {
  const seconds = Math.max(0, Math.floor(((end ? Date.parse(end) : now.value) - Date.parse(start)) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
const labels = { queued: '等待准备', running: '正在准备预览', completed: '方案已生成', failed: '预览失败', cancelled: '预览已取消', expired: '方案已过期', consumed: '方案已启动审查' };
</script>
<template>
  <section v-if="job || tracking || connectionError" class="card card-pad space-top" aria-live="polite" aria-label="PR 预览进度">
    <div class="card-header"><h2>{{ job ? labels[job.status] : '正在连接预览服务' }}</h2><span v-if="job" class="muted small">总耗时 {{ elapsed(job.created_at, job.completed_at) }}</span></div>
    <p v-if="active || tracking" class="muted small">预览在服务端继续执行，刷新后会恢复进度。大型仓库首次准备可能需要数分钟。</p>
    <p v-if="job?.cache_hit" class="notice">已复用相同提交的仓库输入与索引。</p>
    <ol v-if="job?.stages.length" class="preview-stages">
      <li v-for="(stage, index) in job.stages" :key="index"><span>{{ stage.message }}<template v-if="stage.percent !== null"> · {{ stage.percent }}%</template></span><small class="muted">{{ elapsed(stage.started_at, stage.finished_at || (!active ? job.completed_at : null)) }}</small></li>
    </ol>
    <p v-if="job?.error" class="notice danger" role="alert">{{ job.error }}</p>
    <p v-if="connectionError" class="notice danger" role="alert">连接中断：{{ connectionError }}。已保留预览标识，可继续查询。</p>
    <div class="preview-actions"><button v-if="connectionError && !tracking" type="button" class="btn" @click="$emit('resume')">继续查询预览</button><button v-if="active" type="button" class="btn" @click="$emit('cancel')">取消预览</button></div>
  </section>
</template>
<style scoped>
.preview-stages { list-style: none; padding: 0; margin: 12px 0; }
.preview-stages li { display: flex; justify-content: space-between; gap: 16px; padding: 7px 0; }
.preview-stages small { white-space: nowrap; }
.preview-actions { display: flex; gap: 12px; }
</style>
