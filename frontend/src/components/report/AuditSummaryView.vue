<script setup lang="ts">
import { computed, ref } from 'vue';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import { coverageLabel, reportValidationLabel } from '../../utils/reportPresentation';
import { runtimeLabel } from '../../utils/taskRuntimePresentation';
import ReportMarkdown from './ReportMarkdown.vue';
import AppIcon from '../common/AppIcon.vue';
const props = defineProps<{ task?: ReviewTask; plan: PublicationPlan; markdown?: string | null; stale?: boolean }>();
const emit = defineEmits<{ file: [path: string] }>();
const digest = computed(() => props.plan.digest);
const rawOpen = ref(false);
const markdownOpen = ref(false);
const evidenceIndex = computed(() => [...digest.value.examined_goals, ...digest.value.incomplete_goals]);
const raw = computed(() => JSON.stringify({ snapshot: props.plan.snapshot, outcome: props.plan.outcome,
  digest: props.plan.digest, task_runtime_report: props.task?.task_runtime_report ?? null }, null, 2));
const stepLabels: Record<string, string> = { intake: '读取 PR', repo_prepare: '固定仓库快照', diff_parse: '解析 Diff', repo_index: '建立静态索引', task_review_runtime: '静态检查与证据核验', report: '生成报告', reporting: '生成报告' };
const stepStatus: Record<string, string> = { completed: '已完成', running: '进行中', failed: '失败', pending: '未执行' };
</script>
<template>
  <div class="rd-doc">
    <div class="rd-notice"><AppIcon name="info" :size="17" /><div><strong>技术审计记录与面向开发者的交付报告分离。</strong> 此处呈现机器执行、证据索引和覆盖说明，未验证线索仍保留未决状态；运行步骤与计量请查看主导航“技术记录”。</div></div>
    <div v-if="stale" class="rd-notice warn rd-space"><AppIcon name="history" :size="17" /><div><strong>历史快照记录。</strong>以下执行记录与证据索引来自原 Head <code>{{ plan.snapshot.head_sha?.slice(0, 12) || '未记录' }}</code>；PR 已有新提交，不能用于判断最新版本。</div></div>
    <div class="rd-audit-grid"><div><strong>{{ task?.changed_files.length ?? '未知' }}</strong><small>Diff 文件</small></div><div><strong>{{ task ? `+${task.changed_files.reduce((sum, file) => sum + file.additions, 0)}` : '未知' }}</strong><small>新增行数</small></div><div><strong>{{ plan.outcome.examined_goals ?? '?' }}/{{ plan.outcome.total_goals ?? '?' }}</strong><small>约定目标检查</small></div><div><strong>{{ plan.outcome.publishable_count ?? '未知' }}</strong><small>可发布问题</small></div></div>
    <p v-if="stale" class="rd-history-note">以上数量来自旧快照，不适用于最新 Head。</p>
    <section class="rd-doc-section"><h3>审查执行摘要<span v-if="stale" class="badge warn rd-history-badge">历史快照结论</span></h3><ol v-if="task?.steps.length" class="rd-timeline"><li v-for="(step, index) in task.steps" :key="`${step.name}:${index}`"><span class="rd-step" :class="step.status"><AppIcon :name="step.status === 'completed' ? 'check' : step.status === 'failed' ? 'alert' : 'clock'" :size="14" /></span><div><strong>{{ stepLabels[step.name] || runtimeLabel(step.name) }} <span class="badge neutral">{{ stepStatus[step.status] || step.status }}</span></strong><p>{{ step.message || '未记录阶段说明' }}</p></div></li></ol><p v-else>未记录可用的执行阶段，不推断为全部完成。</p><div class="rd-two-col"><div class="rd-info-panel"><h4>审查完整性</h4><p>{{ coverageLabel(plan.outcome.review_completeness) }} · {{ plan.outcome.examined_goals ?? '?' }}/{{ plan.outcome.total_goals ?? '?' }} 个约定目标</p></div><div class="rd-info-panel"><h4>动态验证</h4><p>{{ reportValidationLabel(plan.outcome.dynamic_validation) }}；静态证据与动态验证分别记录。</p></div></div></section>
    <section class="rd-doc-section"><h3>证据追溯与作者声明</h3><div class="rd-two-col"><div class="rd-info-panel"><h4>作者目标追溯</h4><p>{{ digest.author_context_limitation || '作者目标的逐项符合性以已绑定的证据观察为准。' }}</p></div><div class="rd-info-panel"><h4>局部证据记录</h4><p>{{ digest.examined_goals.length }} 项已检查记录 · {{ digest.incomplete_goals.length }} 项未完成记录。记录数量不代表动态执行通过。</p></div></div>
      <details class="rd-accordion"><summary>展开证据与目标索引（{{ evidenceIndex.length }} 项）</summary><div class="rd-accordion-content rd-table-wrap"><table><thead><tr><th>文件 / 目标</th><th>来源</th><th>状态与记录含义</th></tr></thead><tbody><tr v-for="(goal, index) in evidenceIndex" :key="`${goal.goal_id}:${index}`"><td><button v-if="goal.path" type="button" class="rd-path" @click="emit('file', goal.path)">{{ goal.path }}{{ goal.start_line ? `:${goal.start_line}${goal.end_line && goal.end_line !== goal.start_line ? `–${goal.end_line}` : ''}` : '' }}</button><span v-else>{{ goal.goal_id || '未提供路径' }}</span></td><td>{{ goal.side === 'base' ? 'Base 静态记录' : goal.side === 'head' ? 'Head 静态记录' : '未记录侧别' }}</td><td>{{ runtimeLabel(goal.status) }}<small>{{ goal.reason }}</small></td></tr></tbody></table><p v-if="!evidenceIndex.length">没有可用的逐项目标记录。</p></div></details>
      <details v-if="digest.investigations?.length" class="rd-accordion"><summary>调查与未决记录（{{ digest.investigations.length }} 项）</summary><div class="rd-accordion-content"><article v-for="(item, index) in digest.investigations" :key="index" class="rd-evidence"><strong>{{ item.proposition }}</strong><p>{{ ({ supported: '证据支持', refuted: '证据否定', unresolved: '尚未解决' })[item.answer] }}：{{ item.reason || '未提供说明' }}</p><ul><li v-for="(limit, i) in item.limitations" :key="i">{{ limit }}</li></ul></article></div></details>
    </section>
    <section class="rd-doc-section"><h3>审查快照与机器记录</h3><details class="rd-accordion"><summary>审查身份、Head 与 Effective Base</summary><dl class="rd-snapshot"><dt>审查 ID</dt><dd>{{ plan.review_id }}</dd><dt>Head SHA</dt><dd><code>{{ plan.snapshot.head_sha || '未记录' }}</code></dd><dt>Effective Base</dt><dd><code>{{ plan.snapshot.effective_base_sha || '未记录' }}</code></dd><dt>引擎 / 模式</dt><dd>{{ task?.engine || '历史引擎未知' }} / {{ task?.mode || '未记录' }}</dd></dl></details><details class="rd-accordion" @toggle="rawOpen = ($event.target as HTMLDetailsElement).open"><summary>查看原始结构化记录</summary><div class="rd-accordion-content"><p>当前报告计划及可用的任务运行记录，不等于所有原始事件。</p><pre v-if="rawOpen">{{ raw }}</pre></div></details><details v-if="markdown" class="rd-accordion" @toggle="markdownOpen = ($event.target as HTMLDetailsElement).open"><summary>查看后端原始技术记录正文（导出内容）</summary><ReportMarkdown v-if="markdownOpen" :content="markdown" /></details></section>
  </div>
</template>
