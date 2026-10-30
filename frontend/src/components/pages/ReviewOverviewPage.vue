<script setup lang="ts">
import { computed } from 'vue';
import type { Finding, Report, Run } from '../../types/review';
import type { ReviewSection } from '../../types/workspace';
import { phaseLabel, terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';

const props = defineProps<{
  task: Run;
  report: Report | null;
  findings: Finding[];
  loading: boolean;
}>();
defineEmits<{ navigate: [section: ReviewSection]; 'retry-overview': [] }>();

const unresolved = computed(() => props.report?.candidates.filter(
  item => !item.verification || item.verification.verdict === 'insufficient',
).length ?? 0);
const tone = computed(() => {
  if (props.task.status === 'failed' || props.findings.length) return 'danger';
  if (props.task.status === 'partial' || unresolved.value || props.report?.reasons.includes('scope_empty')) return 'warning';
  return props.task.status === 'completed' ? 'success' : 'info';
});
const title = computed(() => {
  if (props.task.status === 'failed') return '审查中断，尚无完整结论';
  if (props.task.status === 'cancelled') return '审查已停止，保留已有结果';
  if (props.task.status === 'partial') return props.findings.length ? '发现问题，审查结论仍不完整' : '审查结论不完整';
  if (props.findings.length) return '发现需要处理的已准入问题';
  if (unresolved.value) return '存在尚未解决的候选问题';
  return terminal(props.task.status) ? '在本轮已处理范围内未记录已确认问题' : '审查正在进行';
});
const overviewLabel = computed(() => ({
  pending: '等待生成变更总览',
  running: '正在生成 LLM 变更总览，审查事实已保留',
  ready: 'LLM 变更总览',
  failed: 'LLM 总览未能生成，审查事实已保留',
  skipped_deadline: '已到硬截止时间，未生成 LLM 总览',
  skipped_cancelled: '审查已取消，未生成 LLM 总览',
  skipped_empty: '没有可审查范围，未调用模型生成总览',
}[props.report?.overview.status ?? 'pending']));
const excluded = computed(() => Object.entries(props.report?.excluded || {}));
const omitted = computed(() => Object.entries(props.report?.overview.omitted || {}).filter(([, count]) => count > 0));
</script>

<template>
  <div class="rg-overview">
    <section class="ov-panel ov-verdict" :data-tone="tone">
      <div class="ov-verdict__inner">
        <div class="ov-verdict__kicker">
          <AppIcon name="shield" />本轮结论 · {{ terminal(task.status) ? '固定快照审查' : '执行中' }}
        </div>
        <h2>{{ title }}</h2>
        <p class="ov-verdict__description">候选经过源码证据准入与独立核验；零问题不代表代码没有缺陷。</p>
        <p v-if="unresolved" class="notice warn small">存在 {{ unresolved }} 个未决问题，请结合候选的缺失前提判断。</p>
        <div class="ov-verdict__bottom">
          <div class="ov-verdict__meta">
            <span><AppIcon name="activity" />{{ phaseLabel(task.phase) }}</span>
            <span><AppIcon name="units" />{{ task.plan.units.length }} 个审查单元</span>
          </div>
          <button class="ov-link" @click="$emit('navigate', 'findings')">查看问题 <AppIcon name="arrow-right" /></button>
        </div>
      </div>
    </section>

    <div class="ov-main">
      <section class="ov-panel ov-understanding">
        <header class="ov-section-head">
          <h2>变更总览</h2>
          <button class="ov-link" @click="$emit('navigate', 'changes')">查看 Diff <AppIcon name="external" :size="14" /></button>
        </header>
        <p v-if="loading" class="ov-note" role="status">正在读取报告…</p>
        <template v-if="report?.overview.status === 'ready' && report.overview.output">
          <p v-for="item in report.overview.output.change_overview" :key="item.text" class="rg-preserve-lines">{{ item.text }}</p>
        </template>
        <p v-else class="ov-note" role="status">{{ overviewLabel }}</p>
        <button v-if="report?.overview.status === 'failed' && terminal(task.status)" class="ov-link" @click="$emit('retry-overview')">使用剩余额度重试总览</button>
        <p v-if="omitted.length" class="ov-note">
          <AppIcon name="info" />总览输入有省略：
          <span v-for="[key, count] in omitted" :key="key">{{ key }} {{ count }} 项；</span>
          引用只能证明已提供的来源，不能保证语义穷尽。
        </p>
        <dl class="ov-change__rows">
          <div>
            <dt>变更范围</dt>
            <dd>{{ task.changed_files.length }} 个文件，新增 {{ task.changed_files.reduce((sum, file) => sum + file.additions, 0) }} 行、删除 {{ task.changed_files.reduce((sum, file) => sum + file.deletions, 0) }} 行。</dd>
          </div>
          <div>
            <dt>作者声明</dt>
            <dd class="rg-preserve-lines">{{ task.pr.body || task.pr.title }}<p class="ov-change__limitation">作者描述是不可信背景，不表示目标已经源码核实。</p></dd>
          </div>
        </dl>
      </section>

      <section class="ov-panel ov-results">
        <div class="ov-metrics">
          <div class="ov-metric">
            <button class="ov-metric__head" @click="$emit('navigate', 'findings')">
              <span class="ov-metric__title"><AppIcon name="shield" />已准入问题</span>
              <strong :class="{ 'ov-count-danger': !!findings.length }">{{ findings.length }}</strong>
              <small>已保存来源与独立核验</small>
            </button>
          </div>
          <div class="ov-metric">
            <button class="ov-metric__head" @click="$emit('navigate', 'findings')">
              <span class="ov-metric__title"><AppIcon name="help" />未决候选</span>
              <strong>{{ unresolved }}</strong>
              <small>缺失前提单独保留</small>
            </button>
          </div>
          <div class="ov-metric">
            <button class="ov-metric__head" @click="$emit('navigate', 'activity')">
              <span class="ov-metric__title"><AppIcon name="units" />工作项</span>
              <strong>{{ report?.units.length ?? '—' }}</strong>
              <small>材料交付与执行分别记录</small>
            </button>
          </div>
        </div>
      </section>
    </div>

    <section class="ov-panel ov-boundary">
      <header class="ov-section-head"><h2>范围与历史</h2></header>
      <p class="ov-note"><AppIcon name="info" />只读静态审查。未执行目标仓库的测试、构建或脚本。</p>
      <p v-if="task.plan.incremental_from" class="ov-note">
        本轮复用了此前完成且源码字节一致的范围，来源审查 <code>{{ task.plan.incremental_from }}</code>；
        复用项在活动页单独标记。历史问题仍须本轮独立核验。
      </p>
      <p v-else-if="task.plan.incremental_reason" class="muted small">本轮按全量范围执行：{{ task.plan.incremental_reason }}</p>
      <p v-if="task.plan.history.length" class="ov-note">包含 {{ task.plan.history.length }} 条历史意见作为调查线索；历史结论不代表当前 Head 仍成立。</p>
      <ul v-if="report?.reasons.length" class="ov-limitations">
        <li v-for="reason in report.reasons" :key="reason">{{ reason }}</li>
      </ul>
      <ul v-if="excluded.length" class="ov-limitations">
        <li v-for="[path, reason] in excluded" :key="path"><code>{{ path }}</code> · {{ reason }}</li>
      </ul>
    </section>
  </div>
</template>
