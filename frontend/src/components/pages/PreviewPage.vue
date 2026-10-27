<script setup lang="ts">
import type { PreviewJob } from '../../types/previewJob';
import AppIcon from '../common/AppIcon.vue';
import ReviewStepper from '../common/ReviewStepper.vue';
import StatusBadge from '../common/StatusBadge.vue';
defineProps<{ job: PreviewJob | null; tracking: boolean; connectionError: string; prUrl: string }>();
defineEmits<{ resume: []; cancel: []; retry: []; back: [] }>();
</script>
<template>
  <ReviewStepper :current="0" />
  <div class="page-head"><div><h1>{{ tracking ? '正在准备审查方案' : '方案准备状态' }}</h1><p class="title-sub break-anywhere">{{ prUrl }}</p></div><span class="badge blue">零模型调用</span></div>
  <div class="split-70"><section class="card card-pad" :aria-busy="tracking"><div class="card-header"><h2>固定快照准备</h2><StatusBadge v-if="job" :status="job.status" /></div>
    <div v-if="tracking" class="progress rg-indeterminate" role="progressbar" aria-label="正在准备固定快照"><span style="width:35%" /></div>
    <ol class="rg-stage-list"><li v-for="(stage,index) in job?.stages" :key="index"><AppIcon name="check-circle" /><div><strong>{{ stage.message }}</strong><small v-if="stage.percent !== null"> {{ stage.percent }}%</small></div></li></ol>
    <p v-if="!job && !connectionError" class="muted" role="status">正在连接预览服务…</p>
    <p v-if="job?.error" class="notice warn" role="alert">{{ job.error }}</p><p v-if="connectionError" class="notice warn" role="alert">{{ connectionError }}</p>
  </section><aside class="plan-aside"><section class="card card-pad"><div class="card-header"><h2>本次预览</h2><AppIcon name="shield" /></div><p class="muted small">读取 PR 信息、准备 Base/Head 快照并建立静态索引；不调用审查模型，不执行目标代码。</p><p class="field-hint space-top">刷新页面后可以恢复服务端预览进度。</p><div class="plan-cta">
    <button v-if="tracking" class="btn secondary" @click="$emit('cancel')">取消预览</button>
    <button v-if="connectionError" class="btn primary" @click="$emit('resume')">重新连接预览</button>
    <button v-if="!tracking" class="btn secondary" @click="$emit('retry')">重新准备方案</button>
    <button v-if="!tracking" class="btn secondary" @click="$emit('back')">返回修改 PR</button>
  </div></section></aside></div>
</template>
