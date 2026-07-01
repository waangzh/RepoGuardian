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
        {{ issue.file_path }}<template v-if="issue.line_no">:{{ issue.line_no }}</template>
      </p>
      <p>{{ issue.description }}</p>
      <p class="suggestion">{{ issue.suggestion }}</p>
      <footer>
        {{ issue.category }} / confidence {{ issue.confidence.toFixed(2) }} /
        {{ issue.auto_fixable ? "可自动修复" : "需人工判断" }} / {{ issue.id.slice(0, 8) }}
      </footer>
    </article>
  </section>
</template>
