<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Finding, Report, Run } from '../../types/review';
import type { ReviewSection } from '../../types/workspace';
import { phaseLabel, terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import InlineCodeText from '../common/InlineCodeText.vue';

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
const additions = computed(() => props.task.changed_files.reduce((sum, file) => sum + file.additions, 0));
const deletions = computed(() => props.task.changed_files.reduce((sum, file) => sum + file.deletions, 0));
const authorText = computed(() => props.task.pr.body?.trim() || props.task.pr.title);
const authorOpen = ref(false);
const omitted = computed(() => Object.entries(props.report?.overview.omitted || {}).filter(([, count]) => count > 0));
const omissionSectionLabel: Record<string, string> = {
  changes: '变更片段', sources: '源码片段', findings: '已确认问题', decision_boundaries: '候选判定背景',
};
const omissionLabel = {
  material_capacity: '材料容量不足',
  source_unavailable: '源码不可用',
  diff_source_mismatch: 'Diff 与源码不一致',
};
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
          <div class="ov-overview-text">
            <p v-for="(item, index) in report.overview.output.change_overview" :key="item.text" class="rg-preserve-lines" :class="{ 'is-lead': index === 0 }"><InlineCodeText :text="item.text" /></p>
          </div>
        </template>
        <p v-else class="ov-note" role="status">{{ overviewLabel }}</p>
        <button v-if="report?.overview.status === 'failed' && terminal(task.status)" class="ov-link" @click="$emit('retry-overview')">使用剩余额度重试总览</button>
        <p v-if="omitted.length" class="ov-note">
          <AppIcon name="info" />总览输入有省略：
          <span v-for="[key, count] in omitted" :key="key">{{ omissionSectionLabel[key] }} {{ count }} 项；</span>
          引用只能证明已提供的来源，不能保证语义穷尽。
        </p>
        <details v-if="report?.overview.omitted_materials.length" class="ov-note">
          <summary>查看总览省略范围</summary>
          <ul>
            <li v-for="(item, index) in report.overview.omitted_materials" :key="index">
              <code>{{ item.path }}</code>
              <span v-if="item.side"> {{ item.side.toUpperCase() }} {{ item.start_line }}–{{ item.end_line }}</span>
              {{ omissionLabel[item.reason] }}
            </li>
          </ul>
        </details>
        <dl class="ov-change__rows">
          <div>
            <dt>变更范围</dt>
            <dd class="ov-change__scope">
              <span class="ov-change__filecount"><b>{{ task.changed_files.length }}</b> 个文件</span>
              <span class="ov-diff-stat is-add">+{{ additions }}</span>
              <span class="ov-diff-stat is-del">−{{ deletions }}</span>
            </dd>
          </div>
          <div>
            <dt>作者声明</dt>
            <dd>
              <div class="ov-author" :class="{ 'is-open': authorOpen }">
                <p class="ov-author__body rg-preserve-lines">{{ authorText }}</p>
                <button v-if="authorText.length > 240" type="button" class="ov-author__toggle" @click="authorOpen = !authorOpen">
                  {{ authorOpen ? '收起作者原文' : '展开作者原文' }}
                </button>
              </div>
              <p class="ov-change__limitation">作者描述是不可信背景，不表示目标已经源码核实。</p>
            </dd>
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
