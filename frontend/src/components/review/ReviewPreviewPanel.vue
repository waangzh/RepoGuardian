<script setup lang="ts">
import type { ReviewPreviewResponse, ReviewUnit } from "../../types/review";
import AppIcon from "../common/AppIcon.vue";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";

defineProps<{ preview: ReviewPreviewResponse | null; previewing: boolean }>();

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

function unitTitle(unit: ReviewUnit): string {
  return unit.changed_symbols[0] || basename(unit.primary_files[0] || unit.id).replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ");
}
</script>

<template>
  <section class="preview-workspace panel" :aria-busy="previewing">
    <header class="preview-workspace__header">
      <div><span>DETERMINISTIC PLAN</span><h2>Preview · 确定性审查计划</h2><p>Planner 将变更组织为独立变更组，并在模型调用前暴露范围、风险和预算。</p></div>
      <StatusBadge status="info" label="零模型调用" />
    </header>

    <div v-if="previewing" class="preview-loading"><span /><strong>正在拉取 PR 并规划审查范围…</strong><small>这一步不会调用模型，也不会运行目标仓库代码。</small></div>
    <template v-else-if="preview">
      <div class="preview-metrics-grid">
        <article><p>变更文件</p><strong>{{ preview.changed_file_count }}</strong><small>{{ preview.included_file_count }} included · {{ preview.excluded_files.length }} excluded</small></article>
        <article><p>变更组</p><strong>{{ preview.review_units.length }}</strong><small>{{ preview.review_units.filter(unit => unit.complexity === 'small').length }} small · {{ preview.review_units.filter(unit => unit.complexity === 'medium').length }} medium · {{ preview.review_units.filter(unit => unit.complexity === 'large').length }} large</small></article>
        <article><p>Unit 预计调用</p><strong>{{ preview.estimated_model_calls }}</strong><small>planning {{ preview.planning_model_calls }} · Unit max {{ preview.max_model_calls }}</small></article>
        <article><p>Unit 输入规模估算</p><strong>{{ preview.estimated_tokens.toLocaleString() }}</strong><small>Review Unit 合计，不含 verifier、dedup 与报告</small></article>
      </div>
      <div v-if="preview.risk_tags.length" class="preview-risk-tags"><strong>风险标签</strong><span v-for="risk in preview.risk_tags" :key="risk">{{ risk }}</span></div>
      <section class="preview-groups">
        <header><h3>计划中的变更组</h3><span>主要文件</span><span>复杂度</span><span>Tokens</span><span>风险</span></header>
        <article v-for="unit in preview.review_units" :key="unit.id">
          <span><strong>{{ unitTitle(unit) }}</strong><small>{{ unit.grouping_reason }}</small></span>
          <code>{{ unit.primary_files.length }} + {{ unit.related_files.length }} related</code>
          <StatusBadge :status="unit.complexity" :label="unit.complexity" />
          <b>{{ unit.estimated_tokens.toLocaleString() }}</b>
          <StatusBadge :status="unit.risk_tags.includes('high') ? 'high' : unit.risk_tags.length ? 'medium' : 'low'" :label="unit.risk_tags[0] || 'low'" />
        </article>
      </section>
      <section v-if="preview.excluded_files.length" class="preview-excluded"><h3>已排除</h3><p v-for="file in preview.excluded_files" :key="file.file_path"><code>{{ file.file_path }}</code><span>{{ file.reason }}</span></p></section>
      <p v-for="warning in preview.warnings" :key="warning" class="preview-warning"><AppIcon name="alert" :size="15" />{{ warning }}</p>
    </template>
    <EmptyState v-else icon="context" title="等待 Preview" description="输入一个 GitHub Pull Request URL，先确认文件范围、变更组和模型预算，再决定是否启动审查。" />
  </section>
</template>
