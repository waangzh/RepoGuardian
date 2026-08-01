<script setup lang="ts">
import type { ReviewIssue } from "../types/review";

defineProps<{
  issues: ReviewIssue[];
}>();

const severityLabel: Record<string, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low"
};

const placementLabel: Record<string, string> = {
  inline: "已定位",
  summary: "仅摘要",
  needs_human: "需要人工定位",
  suppressed: "证据无效"
};
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <h2>审查问题</h2>
      <span>{{ issues.length }}</span>
    </div>
    <div v-if="issues.length === 0" class="empty">未发现明确问题</div>
    <article
      v-for="issue in issues"
      :key="issue.id"
      class="issue"
      :data-severity="issue.severity"
    >
      <header>
        <span class="badge">{{ severityLabel[issue.severity] }}</span>
        <h3>{{ issue.title }}</h3>
      </header>
      <p class="location">
        {{ issue.primary_evidence.file_path }}<template v-if="issue.primary_evidence.resolved_start_line">:{{ issue.primary_evidence.resolved_start_line }}</template>
        · {{ placementLabel[issue.placement] }}
      </p>
      <p>{{ issue.failure_scenario }}</p>
      <p class="suggestion">{{ issue.recommendation }}</p>
      <p v-if="issue.unresolved_reason" class="location">{{ issue.unresolved_reason }}</p>
      <footer>
        {{ issue.category }} / confidence {{ issue.confidence.toFixed(2) }} /
        {{ issue.auto_fix_eligible ? "可自动修复" : "需人工判断" }} / {{ issue.id.slice(0, 8) }}
      </footer>
    </article>
  </section>
</template>
