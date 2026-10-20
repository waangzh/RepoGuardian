<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import type { ReviewTask, TaskRuntimeReport } from "../../types/review";
import type { ReviewSummary } from "../../types/publication";
import { asRecord, runtimeLabel, runtimeWarnings } from "../../utils/taskRuntimePresentation";
import StatusBadge from "../common/StatusBadge.vue";
const props = defineProps<{ task: ReviewTask; runtime: TaskRuntimeReport | null; duration: string; summary?: ReviewSummary | null; initialFocus?: "limits" | "validation" | "error" }>();
const limitsPanel = ref<HTMLElement | null>(null), validationPanel = ref<HTMLElement | null>(null), errorPanel = ref<HTMLElement | null>(null);
const limits = computed(() => {
  const recorded = props.summary?.digest.merge_decision?.limits;
  return recorded?.length ? recorded : props.runtime ? runtimeWarnings(props.runtime) : [];
});
const validations = computed(() => props.summary?.digest.validations || []);
onMounted(() => {
  const target = props.initialFocus === "validation" ? validationPanel.value : props.initialFocus === "limits" ? limitsPanel.value : props.initialFocus === "error" ? errorPanel.value : null;
  target?.focus(); target?.scrollIntoView({ block: "start" });
});
const labels: Record<string, string> = { queued: '等待执行', intake: '读取 PR', repo_prepare: '准备仓库快照', diff_parse: '解析变更', repo_index: '建立静态索引', project_detection: '识别项目', review_plan: '规划审查目标', task_review_runtime: '检查代码与核验证据', review_units: '静态检查', resolve_evidence: '定位证据', issue_policy: '检查问题策略', issue_verifier: '独立核验问题', report: '整理报告', complete: '保存审查结果' };
</script>
<template>
  <div class="page-head"><div><h1>技术记录</h1><p class="title-sub">查看真实执行步骤、固定快照与计量完整性。原始技术数据按需展开。</p></div></div>
  <div class="section-stack"><div class="kpis"><section class="card kpi"><div class="cap">总耗时</div><div class="number rg-number-small">{{ duration || '尚未结束' }}</div><small>以任务创建和更新时间计算</small></section><section class="card kpi"><div class="cap">已发送模型调用</div><div class="number">{{ runtime?.sent_calls ?? '未知' }}</div><small>与缺陷数量无关</small></section><section class="card kpi"><div class="cap">已结算 Tokens</div><div class="number">{{ runtime?.settled_tokens.toLocaleString() ?? '未知' }}</div><small>{{ runtime?.usage_integrity === 'complete' ? '计量记录完整' : '不代表整轮完整消耗' }}</small></section><section class="card kpi"><div class="cap">未知用量请求</div><div class="number">{{ runtime?.unknown_attempts ?? '未知' }}</div><small>缺失用量不记为零</small></section></div>
    <section v-if="initialFocus === 'error' || task.status === 'failed'" ref="errorPanel" class="card card-pad" tabindex="-1" aria-label="执行错误"><h2>执行错误</h2><p>{{ runtime?.failure_summary || '未保存可确认的错误摘要；请核对下方执行历程和终止原因，不推测故障原因。' }}</p></section>
    <section v-if="limits.length || initialFocus === 'limits'" ref="limitsPanel" class="card card-pad" tabindex="-1" aria-label="运行限制"><h2>运行限制</h2><p>以下记录限制合并判断，不等于已确认代码缺陷；请分别核对实际检查范围、执行和计量记录。</p><ul v-if="limits.length"><li v-for="item in limits" :key="item">{{ item }}</li></ul><p v-else>未保存明确的限制条目，请核对执行历程与计量完整性；缺失记录保持未知。</p></section>
    <div class="grid twoone"><section class="card card-pad"><div class="card-header"><div><h2>审查执行历程</h2><p>来自服务端保存的步骤状态</p></div><StatusBadge :status="task.status" /></div><div class="timeline"><div v-for="(step, index) in task.steps" :key="`${step.name}-${index}`" class="timeline-row" :class="{ current: step.status === 'running' }"><span class="timeline-dot">{{ step.status === 'completed' ? '✓' : index + 1 }}</span><div class="timeline-body"><strong>{{ labels[step.name] || step.name }}</strong><p>{{ step.message || '此步骤未提供说明。' }}</p><small v-if="step.progress">{{ step.progress.detail }} <template v-if="step.progress.current != null">{{ step.progress.current }} / {{ step.progress.total ?? '未知' }}</template></small></div><StatusBadge :status="step.status" /></div></div><p v-if="!task.steps.length" class="muted small">尚无执行步骤记录。</p></section>
      <section class="card card-pad"><div class="card-header"><h2>运行信息</h2></div><dl class="rg-facts"><div><dt>审查引擎</dt><dd>{{ task.engine || 'legacy_units（历史）' }}</dd></div><div><dt>终止原因</dt><dd>{{ runtime ? runtimeLabel(runtime.termination_reason) : '未记录' }}</dd></div><div><dt>执行模型</dt><dd>{{ task.model || asRecord(task.task_runtime_configuration).model || '未记录 / 服务端默认' }}</dd></div><div><dt>Head SHA</dt><dd><code>{{ task.pr?.head.sha || '未记录' }}</code></dd></div><div><dt>有效 Base SHA</dt><dd><code>{{ task.run_manifest?.effective_base_sha || task.run_manifest?.base_sha || '未记录' }}</code></dd></div><div><dt>计量完整性</dt><dd>{{ runtime ? runtimeLabel(runtime.usage_integrity) : '未知' }}</dd></div><div><dt>费用</dt><dd>未知（未提供完整价格与结算信息）</dd></div></dl></section></div>
    <section class="card card-pad"><h2>冻结的资源限制</h2><pre class="rg-raw">{{ JSON.stringify(asRecord(task.task_runtime_configuration).limits || { status: 'not_recorded', note: '此记录未保存有效限额，不使用初始预览推断。' }, null, 2) }}</pre></section>
    <section ref="validationPanel" class="card card-pad" tabindex="-1" aria-label="验证记录"><h2>验证记录</h2><p>通过仅指该记录覆盖的检查，不代表整个项目 CI 全部通过；请核对来源、快照绑定与失败原因。</p><ul v-if="validations.length"><li v-for="item in validations" :key="item.id">{{ item.source }} / {{ item.stage }}：<StatusBadge :status="item.status" /> {{ item.reason }} · {{ item.trusted && item.snapshot_matches ? '已绑定本次快照' : '可信绑定尚未确认' }}</li></ul><p v-else-if="!task.validation.length" class="muted small space-top">尚无可核对的动态验证结果。静态检查不等于测试通过。</p><div v-for="result in task.validation" :key="result.id" class="aside-stat"><span>{{ result.backend }}</span><StatusBadge :status="result.status" /></div></section>
    <details class="card card-pad"><summary class="rg-disclosure-title">高级审计数据</summary><details class="space-top"><summary>完整任务级检查记录</summary><pre class="rg-raw">{{ JSON.stringify(runtime, null, 2) }}</pre></details><details class="space-top"><summary>模型用量记录</summary><pre class="rg-raw">{{ JSON.stringify(task.model_usage_summary, null, 2) }}</pre></details><details class="space-top"><summary>原始事件与步骤</summary><pre class="rg-raw">{{ JSON.stringify({ steps: task.steps, events: task.agent_events }, null, 2) }}</pre></details></details>
  </div>
</template>
