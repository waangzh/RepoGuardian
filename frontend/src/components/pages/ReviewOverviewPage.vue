<script setup lang="ts">
import { computed } from "vue";
import type { ReviewTask } from "../../types/review";
import type { PublicationPlan } from "../../types/publication";
import type { RuntimeFindingRow, TaskRuntimeSummary } from "../../utils/taskRuntimePresentation";
import type { ReviewSection } from "../../types/workspace";
import { evidenceLabel } from "../../utils/workspacePresentation";
import ReviewOutcomePanel from "../review/ReviewOutcomePanel.vue";
import PrChangeSummary from "../review/PrChangeSummary.vue";
import AppIcon from "../common/AppIcon.vue";
import EmptyState from "../common/EmptyState.vue";
const props = defineProps<{ task: ReviewTask; report?: string | null; plan: PublicationPlan | null; summary: TaskRuntimeSummary | null; findings: RuntimeFindingRow[]; warnings: string[]; terminal: boolean }>();
const emit = defineEmits<{ navigate: [section: ReviewSection]; finding: [key: string] }>();
const active = computed(() => props.findings.filter(row => row.group !== "dismissed"));
const confirmed = computed(() => props.summary?.findings.confirmed ?? (props.task.engine === "task_review_v1" ? null : props.findings.filter(row => row.group === "confirmed").length));
const unresolved = computed(() => props.summary?.findings.unresolved ?? (props.task.engine === "task_review_v1" ? null : props.findings.filter(row => row.group === "unresolved").length));
</script>
<template>
  <div class="section-stack">
    <ReviewOutcomePanel v-if="plan" :plan="plan" />
    <section v-else class="summary-banner"><div class="eyebrow">REVIEW OUTCOME</div><h2>{{ !terminal ? '审查正在进行中' : '审查结果需要结合范围判断' }}</h2><p>结论尚未取得后端发布计划，请参考下方记录与限制。</p></section>
    <div class="notice"><AppIcon name="info" /><div>本轮结论不等于代码已被证明正确。0 个问题不代表所有业务路径已验证，静态核验不能代替测试。</div></div>
    <div class="kpis"><section class="card kpi"><div class="cap">静态核验已确认的问题</div><div class="number">{{ confirmed ?? '未知' }}</div><small>按服务端发现状态统计</small></section><section class="card kpi"><div class="cap">待确认候选</div><div class="number">{{ unresolved ?? '未知' }}</div><small>可能需要业务背景或补充依据</small></section><section class="card kpi"><div class="cap">证据不足的补充调查</div><div class="number">{{ summary?.investigations.insufficient_evidence ?? '未知' }}</div><small>调查状态，不等同于缺陷数量</small></section><section class="card kpi"><div class="cap">约定目标已检查</div><div class="number">{{ summary?.scope.examined ?? '—' }}<small> / {{ summary?.scope.total ?? '—' }}</small></div><small>目标完成度，不是测试覆盖率</small></section></div>
    <div class="grid twoone"><div class="section-stack"><section class="card card-pad"><div class="card-header"><div><h2>需要你关注</h2><p>优先查看结论、证据与触发条件</p></div><button type="button" class="link" @click="emit('navigate', 'findings')">查看全部 →</button></div><button v-for="row in active.slice(0, 5)" :key="row.key" type="button" class="recent-row" @click="emit('finding', row.key)"><AppIcon :name="row.group === 'confirmed' ? 'alert' : 'help'" /><span class="recent-identity"><strong>{{ row.title }}</strong><small>{{ evidenceLabel(row.status, row.issue) }} · {{ row.filePath || '尚无代码定位' }}</small></span><AppIcon name="arrow-right" :size="15" /></button><EmptyState v-if="!active.length" icon="context" :title="terminal ? '本轮没有可展示的待处理发现' : '正在检查本次变更'" description="请同时查看检查完整性、未决调查与未知状态。" /></section><PrChangeSummary :task="task" :report="report" :digest="plan?.digest" /></div>
      <aside class="section-stack"><section class="card card-pad"><div class="card-header"><div><h2>本次审查的证据边界</h2><p>完成情况与运行限制分别说明</p></div></div><div class="rg-boundary-row"><AppIcon name="shield" /><div><strong>只读静态检查</strong><p>目标完成 {{ summary ? `${summary.scope.examined} / ${summary.scope.total}` : '未知' }}；不表示代码正确。</p></div></div><div class="rg-boundary-row"><AppIcon name="info" /><div><strong>{{ task.validation.length ? '历史动态验证记录需单独查看' : '未执行目标仓库测试或构建' }}</strong><p>动态结果与静态结论独立。</p></div></div><ul v-if="warnings.length" class="rg-limitations"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul><button type="button" class="btn secondary space-top" @click="emit('navigate', 'scope')">查看覆盖范围与局限 →</button></section><section class="card card-pad"><div class="card-header"><h2>下一步建议</h2></div><p class="small muted">优先处理已确认的问题，待确认候选结合业务背景复核。发布前检查当前 PR Head 与行定位。</p><div class="hstack wrap space-top"><button type="button" class="btn secondary" @click="emit('navigate', 'report')">查看报告</button><button type="button" class="btn primary" @click="emit('navigate', 'publish')">发布到 GitHub</button></div></section></aside></div>
  </div>
</template>
