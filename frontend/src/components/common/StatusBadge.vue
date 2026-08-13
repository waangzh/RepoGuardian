<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  status: string;
  label?: string;
}>();

const labels: Record<string, string> = {
  pending: "等待中",
  queued: "等待中",
  planning: "规划中",
  running: "进行中",
  reviewing: "审查中",
  resolving_evidence: "解析证据",
  verifying_issues: "验证问题",
  generating_patches: "生成补丁",
  validating: "验证中",
  waiting_for_human: "等待人工",
  needs_human: "等待人工",
  completed: "已完成",
  completed_with_warnings: "存在警告",
  failed: "失败",
  timed_out: "已超时",
  cancelled: "已取消",
  skipped: "已跳过",
  planned: "已规划",
  warning: "警告",
  candidate: "候选",
  evidence_resolved: "证据已定位",
  confirmed: "已确认",
  dismissed: "已过滤",
  published: "已发布",
  suggested: "建议稿",
  unverified: "未验证",
  validation_pending: "待验证",
  verified: "已验证",
  validation_failed: "验证失败",
  validation_inconclusive: "结果不确定",
  stale: "已过期",
  abandoned: "已放弃",
  superseded: "已替代",
  passed: "已通过",
  inconclusive: "不确定",
  unsupported: "不支持",
  infrastructure_error: "环境错误",
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

const tone = computed(() => {
  if (["completed", "confirmed", "verified", "passed", "low"].includes(props.status)) return "success";
  if (["failed", "validation_failed", "critical", "high"].includes(props.status)) return "danger";
  if (["timed_out", "infrastructure_error", "medium", "warning", "completed_with_warnings"].includes(props.status)) return "warning";
  if (["inconclusive", "validation_inconclusive"].includes(props.status)) return "purple";
  if (["running", "reviewing", "planning", "validating", "resolving_evidence", "verifying_issues", "generating_patches", "evidence_resolved"].includes(props.status)) return "info";
  return "neutral";
});

const text = computed(() => props.label || labels[props.status] || props.status);
</script>

<template>
  <span class="status-badge" :data-tone="tone" :title="status">{{ text }}</span>
</template>
