<script setup lang="ts">
import { computed } from "vue";
import type { ReviewIssue, TaskStatus } from "../types/review";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";
import StatusBadge from "./common/StatusBadge.vue";

const props = defineProps<{
  issues: ReviewIssue[];
  taskStatus?: TaskStatus;
}>();

const successful = computed(() => ["completed", "completed_with_warnings"].includes(props.taskStatus || ""));
const ended = computed(() => ["failed", "cancelled"].includes(props.taskStatus || ""));
const emptyTitle = computed(() => successful.value ? "未发现需报告的问题" : ended.value ? "审查未完整结束" : "正在汇总审查问题");
const emptyDescription = computed(() => successful.value
  ? "本次自动审查没有发现达到报告门槛的问题，仍建议由工程师复核关键业务逻辑。"
  : ended.value
    ? "任务在形成完整问题结论前停止，请结合任务状态和执行记录处理。"
    : "候选问题完成证据定位、验证和去重后会显示在这里。");

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

const resolutionLabel: Record<string, string> = {
  diff_exact: "Diff 精确匹配",
  diff_normalized: "Diff 归一化匹配",
  file_exact: "文件精确匹配",
  symbol_assisted: "符号辅助定位",
  unresolved: "尚未定位",
};
</script>

<template>
  <section class="panel issue-panel">
    <PanelHeader icon="alert" title="审查问题" subtitle="Review Issues"><span class="panel-count">{{ issues.length }} 个发现</span></PanelHeader>
    <EmptyState
      v-if="issues.length === 0"
      :icon="successful ? 'check-circle' : ended ? 'alert' : 'clock'"
      :title="emptyTitle"
      :description="emptyDescription"
    />
    <details
      v-for="issue in issues"
      :key="issue.id"
      class="issue"
      :data-severity="issue.severity"
    >
      <summary>
        <div class="issue__badges"><StatusBadge :status="issue.severity" :label="severityLabel[issue.severity]" /><StatusBadge :status="issue.status" /><span>{{ issue.category }}</span></div>
        <strong>{{ issue.title }}</strong>
        <code>{{ issue.primary_evidence.file_path }}<template v-if="issue.primary_evidence.resolved_start_line">:{{ issue.primary_evidence.resolved_start_line }}</template></code>
        <small>{{ placementLabel[issue.placement] }} · 置信度 {{ Math.round(issue.confidence * 100) }}% · {{ issue.auto_fix_eligible ? "可生成补丁" : "需人工判断" }}</small>
      </summary>
      <div class="issue__details">
        <dl>
          <div><dt>受影响行为</dt><dd>{{ issue.affected_behavior }}</dd></div>
          <div><dt>失败场景</dt><dd>{{ issue.failure_scenario }}</dd></div>
          <div><dt>建议</dt><dd>{{ issue.recommendation }}</dd></div>
          <div><dt>证据定位</dt><dd>{{ resolutionLabel[issue.primary_evidence.resolution_method] }} · {{ issue.primary_evidence.expected_side }}</dd></div>
          <div v-if="issue.supporting_evidence.length"><dt>辅助证据</dt><dd>{{ issue.supporting_evidence.map((item) => item.file_path).join("、") }}</dd></div>
          <div v-if="issue.unresolved_reason"><dt>未解决原因</dt><dd>{{ issue.unresolved_reason }}</dd></div>
          <div><dt>Review Unit</dt><dd><code>{{ issue.review_unit_id }}</code></dd></div>
        </dl>
      </div>
    </details>
  </section>
</template>
