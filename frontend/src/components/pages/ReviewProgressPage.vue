<script setup lang="ts">
import { computed, ref } from "vue";
import type { ReviewTask, TaskRuntimeReport } from "../../types/review";
import ReviewStepper from "../common/ReviewStepper.vue";
import AppIcon from "../common/AppIcon.vue";
import StatusBadge from "../common/StatusBadge.vue";
const props = defineProps<{ task: ReviewTask; runtime: TaskRuntimeReport | null; cancelling: boolean }>();
const emit = defineEmits<{ cancel: []; activity: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const current = computed(() => [...props.task.steps].reverse().find(step => step.status === 'running'));
const percent = computed(() => {
  const value = current.value?.progress?.percent;
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
});
</script>
<template>
  <ReviewStepper :current="2" /><div class="page-head"><div><div class="eyebrow">STEP 03 / REVIEW IN PROGRESS</div><h1>正在审查本次变更</h1><p class="title-sub">基于服务端真实进度更新。你可以查看技术记录或停止本次审查。</p></div></div>
  <div class="grid twoone"><div class="section-stack"><section class="card card-pad"><div class="card-header"><div><h2>{{ current?.message || '等待服务端处理审查任务' }}</h2><p>{{ task.pr?.title || task.pr_url }}</p></div><StatusBadge :status="task.status" /></div><strong class="plan-number">{{ percent == null ? '处理中…' : `${percent}%` }}</strong><p class="muted small">{{ percent == null ? '服务端尚未提供可计算的百分比。' : '当前步骤进度，不是整轮审查或覆盖完成率。' }}</p><div class="progress space-top" :class="{ 'rg-indeterminate': percent == null }" role="progressbar" aria-label="当前步骤进度" :aria-valuenow="percent ?? undefined" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: percent == null ? '35%' : `${percent}%` }" /></div><div class="notice space-top"><AppIcon name="info" /><div>服务端不会执行目标仓库的测试、构建或脚本。审查停止后会保留已经取得的结果与覆盖缺口。</div></div></section><section class="card card-pad"><div class="card-header"><h2>审查进展</h2><button type="button" class="link" @click="emit('activity')">技术记录 →</button></div><div class="timeline"><div v-for="(step, index) in task.steps" :key="`${step.name}-${index}`" class="timeline-row" :class="{ current: step.status === 'running' }"><span class="timeline-dot">{{ step.status === 'completed' ? '✓' : index + 1 }}</span><div class="timeline-body"><strong>{{ step.message || step.name }}</strong><p v-if="step.progress?.detail">{{ step.progress.detail }}</p></div><StatusBadge :status="step.status" /></div></div></section></div><aside class="section-stack"><section class="card card-pad"><div class="card-header"><h2>当前资源记录</h2></div><div class="aside-stat"><span class="muted">模型调用</span><strong>{{ runtime?.sent_calls ?? '尚未记录' }}</strong></div><div class="aside-stat"><span class="muted">已结算 Tokens</span><strong>{{ runtime?.settled_tokens.toLocaleString() ?? '尚未记录' }}</strong></div><div class="aside-stat"><span class="muted">未知用量</span><strong>{{ runtime?.unknown_attempts ?? '未知' }}</strong></div><p class="field-hint">缺失的用量不视作 0。完整计量在结果中单独呈现。</p><button type="button" class="btn secondary space-top" :disabled="cancelling" @click="dialog?.showModal()">{{ cancelling ? '取消中…' : '取消审查' }}</button></section></aside></div>
  <dialog ref="dialog" class="rg-dialog" @click.self="dialog?.close()"><h2>停止本次审查？</h2><p>将向服务端提交取消请求。已取得的结果仍会保留，未检查范围会明确展示。</p><div class="hstack wrap space-top"><button type="button" class="btn secondary" @click="dialog?.close()">继续审查</button><button type="button" class="btn danger" :disabled="cancelling" @click="dialog?.close(); emit('cancel')">停止审查</button></div></dialog>
</template>
