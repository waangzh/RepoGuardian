<script setup lang="ts">
import { computed } from 'vue';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import type { ReviewSection } from '../../types/workspace';
import { observedUsage, phaseLabel, terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
const props = defineProps<{ task: ReviewTask; plan: PublicationPlan | null; loading: boolean }>();
defineEmits<{ navigate: [section: ReviewSection]; filter: [status: string] }>();
const report = computed(() => props.task.review_report);
const usage = computed(() => observedUsage(report.value));
const outcome = computed(() => props.plan?.outcome);
const presentation = computed(() => props.plan?.presentation);
const tone = computed(() => props.task.status === 'failed' ? 'danger' : props.task.status === 'cancelled' ? 'info' : outcome.value?.kind === 'clean' ? 'success' : outcome.value?.kind === 'findings' ? 'danger' : 'info');
const headline = computed(() => props.task.status === 'failed' ? '审查中断，尚无完整结论'
  : props.task.status === 'cancelled' ? '审查已停止，保留已有结果'
  : props.plan?.digest.merge_decision?.label || (terminal(props.task.status) ? props.loading ? '正在读取审查结论' : '审查结论暂不可用' : '不同视角正在独立检查本次变更'));
const limits = computed(() => [...new Set([...(props.task.warnings ?? []), ...(report.value?.coverage_limitations ?? []), ...(props.plan?.digest.merge_decision?.limits ?? [])])]);
const running = computed(() => terminal(props.task.status) ? [] : props.task.steps.filter(step => step.status === 'running'));
</script>
<template>
  <div class="rg-overview">
    <section class="ov-panel ov-verdict" :data-tone="tone"><div class="ov-verdict__inner">
      <div class="ov-verdict__kicker"><AppIcon name="shield" />本轮结论 · {{ terminal(task.status) ? '固定快照审查' : '执行中' }}</div>
      <h2>{{ headline }}</h2><p class="ov-verdict__description">{{ plan?.digest.merge_decision?.advice || '行为、契约与风险视角分别读取源码，候选经过独立核验后才可能进入交付草稿。' }}</p>
      <div class="ov-verdict__bottom"><div class="ov-verdict__meta"><span><AppIcon name="activity" />{{ phaseLabel(task.phase) }}</span><span><AppIcon name="units" />{{ report ? `${report.goals.filter(goal => goal.status === 'examined').length} / ${report.goals.length} 个源码范围已检查` : '覆盖范围尚未结算' }}</span></div><button class="ov-link" @click="$emit('navigate','report')">查看审查意见 <AppIcon name="arrow-right" /></button></div>
    </div></section>
    <section v-if="running.length" class="ov-panel ov-understanding"><header class="ov-section-head"><h2>当前工作</h2><button class="ov-link" @click="$emit('navigate','activity')">查看 Agent 活动</button></header><p v-for="step in running" :key="step.name" class="rg-running-agent"><span class="rg-live-dot" />{{ phaseLabel(step.message) }}<code>{{ step.name.slice(0,12) }}</code></p></section>
    <div class="ov-main">
      <section class="ov-panel ov-results"><div class="ov-metrics">
        <div class="ov-metric"><button class="ov-metric__head" @click="$emit('filter','confirmed')"><span class="ov-metric__title"><AppIcon name="shield" />已确认问题</span><strong :class="{ 'ov-count-danger': !!report?.findings.length }">{{ report?.findings.length ?? '—' }}</strong><small>独立核验后的机器结论</small></button></div>
        <div class="ov-metric"><button class="ov-metric__head" @click="$emit('filter','unresolved')"><span class="ov-metric__title"><AppIcon name="help" />待核实候选</span><strong>{{ outcome?.unresolved_count ?? '—' }}</strong><small>未决不等于问题已排除</small></button></div>
        <div class="ov-metric"><button class="ov-metric__head" @click="$emit('navigate','scope')"><span class="ov-metric__title"><AppIcon name="units" />源码范围已检查</span><strong>{{ report?.goals.filter(goal => goal.status === 'examined').length ?? '—' }}<small> / {{ report?.goals.length ?? '—' }}</small></strong><small>独立于问题数量与发布状态</small></button></div>
      </div></section>
      <section class="ov-panel ov-understanding"><header class="ov-section-head"><h2>PR 目标与代码变化</h2><button class="ov-link" @click="$emit('navigate','files')">查看 Diff <AppIcon name="external" :size="14" /></button></header>
        <dl class="ov-change__rows"><div><dt>作者声明</dt><dd class="rg-preserve-lines">{{ task.pr.body || task.pr.title }}<p class="ov-change__limitation">作者描述属于不可信背景，不表示目标已由源码验证。</p></dd></div>
          <div v-if="presentation && ['model','observations'].includes(presentation.summary_source)"><dt>实际变更</dt><dd class="rg-preserve-lines">{{ presentation.change_summary }}</dd></div>
          <div v-else><dt>变更范围</dt><dd>{{ task.changed_files.length }} 个文件，新增 {{ task.changed_files.reduce((sum,file)=>sum+file.additions,0) }} 行、删除 {{ task.changed_files.reduce((sum,file)=>sum+file.deletions,0) }} 行。<p class="muted small">以上来自固定 Diff 元数据；尚缺少有证据的行为观察。</p></dd></div>
          <div v-if="presentation?.review_summary"><dt>主要关注</dt><dd class="rg-preserve-lines">{{ presentation.review_summary }}</dd></div>
          <div v-if="presentation?.action_summary"><dt>处理重点</dt><dd class="rg-preserve-lines">{{ presentation.action_summary }}</dd></div>
        </dl>
        <p v-if="presentation && presentation.synthesis_status !== 'ready'" class="muted small">变更总览未生成，当前显示已保存观察或变更范围；问题与检查限制仍以完整记录为准。</p>
        <p v-if="presentation?.synthesis && Object.values(presentation.synthesis.omitted).some(Boolean)" class="muted small">总览材料存在省略，请结合完整问题与证据阅读。</p>
      </section>
    </div>
    <section class="ov-panel ov-boundary"><header class="ov-section-head"><h2>检查边界与运行记录</h2></header>
      <button class="ov-boundary__row" @click="$emit('navigate','scope')"><AppIcon name="units" /><span class="ov-boundary__copy"><strong>覆盖与限制</strong><small>{{ report?.complete ? '约定源码范围已完成检查，仍需结合证据判断。' : '尚未确认范围完整，未检查或受阻项保持可见。' }}</small></span><AppIcon name="arrow-right" /></button>
      <button class="ov-boundary__row" @click="$emit('navigate','activity')"><AppIcon name="activity" /><span class="ov-boundary__copy"><strong>模型与观测用量</strong><small>{{ task.configuration.model }} · {{ usage.calls ?? '未知' }} 次模型调用 · {{ usage.tokens ?? '未知' }} Tokens</small></span><AppIcon name="arrow-right" /></button>
      <p class="ov-note"><AppIcon name="info" />只读静态审查。未执行目标仓库的测试、构建或脚本；零问题不等于代码已证明正确。</p><ul v-if="limits.length" class="ov-limitations"><li v-for="limit in limits" :key="limit">{{ limit }}</li></ul>
    </section>
  </div>
</template>
