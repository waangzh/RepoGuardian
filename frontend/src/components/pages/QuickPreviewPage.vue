<script setup lang="ts">
import type { QuickPreview } from '../../types/quickPreview';
import ReviewStepper from '../common/ReviewStepper.vue';
import AppIcon from '../common/AppIcon.vue';

defineProps<{ overview: QuickPreview | null; loading: boolean; error: string; prUrl: string }>();
const emit = defineEmits<{ prepare: []; retry: []; back: [] }>();
const statuses: Record<string, string> = { added: '新增', removed: '删除', modified: '修改', renamed: '重命名', copied: '复制' };
</script>
<template>
  <ReviewStepper :current="0" />
  <div class="page-head"><div>
    <h1 tabindex="-1">{{ loading ? '正在读取 PR 概览' : overview ? overview.title : 'PR 概览暂不可用' }}</h1>
    <p class="title-sub break-anywhere">{{ overview ? `${overview.repository} · #${overview.number}` : prUrl }}</p>
  </div><span class="badge blue">零模型调用</span></div>
  <section class="card card-pad" :aria-busy="loading">
    <p v-if="loading" role="status">正在从 GitHub 读取变更文件，无需等待仓库准备。</p>
    <div v-else-if="error" class="notice" role="alert"><AppIcon name="alert" /><p>{{ error }}</p></div>
    <template v-if="overview">
      <div class="card-header"><h2>变更文件{{ overview.files_complete ? `（${overview.files.length}）` : `（前 ${overview.files.length} 项）` }}</h2>
        <span class="muted small">Head {{ overview.head_sha.slice(0, 12) }}</span></div>
      <div v-if="!overview.files_complete" class="notice" role="status"><AppIcon name="info" /><p>当前文件列表可能不完整，完整范围以之后生成的审查方案为准。</p></div>
      <p v-if="!overview.files.length" class="muted">GitHub 当前未返回变更文件；请准备完整方案进一步核对。</p>
      <div v-for="file in overview.files" :key="file.path" class="quick-file">
        <span class="quick-file-path break-anywhere"><strong>{{ file.path }}</strong><small v-if="file.previous_path" class="muted">原路径：{{ file.previous_path }}</small></span>
        <span class="muted small">{{ statuses[file.status] || file.status }}</span>
        <span class="quick-file-count">+{{ file.additions }} / −{{ file.deletions }}</span>
      </div>
    </template>
    <div class="divider" />
    <p class="muted small">这里只展示 GitHub 变更概览，尚未进行审查。准备完整方案后可选择范围、深度和模型，再确认开始；完整方案会重新校验 PR 快照。</p>
    <div class="quick-actions">
      <button type="button" class="btn primary" :disabled="loading" @click="emit('prepare')">准备完整审查方案</button>
      <button v-if="error" type="button" class="btn secondary" @click="emit('retry')">重试概览</button>
      <button type="button" class="btn secondary" @click="emit('back')">{{ loading ? '取消并返回' : '返回修改 PR 链接' }}</button>
    </div>
  </section>
</template>
<style scoped>
.quick-file { display: flex; align-items: center; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.quick-file-path { flex: 1; min-width: 0; display: grid; gap: 4px; }
.quick-file-count { white-space: nowrap; font-variant-numeric: tabular-nums; }
.quick-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }
@media (max-width: 600px) { .quick-file { flex-wrap: wrap; gap: 8px; } .quick-file-path { flex-basis: 100%; } }
</style>
