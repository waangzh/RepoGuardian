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
  waiting_for_human: "历史审查已中止",
  needs_human: "未确认",
  unresolved: "未确认",
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

/**
 * 颜色按三条互不混用的语义轴划分，同一个状态只表达一类含义：
 * - 执行状态：任务/步骤/验证是否跑完，成功色只表示“正向完成”。
 * - 证据状态：发现是否已核验；已核验只是事实，不代表结果是好消息，因此用信息色。
 * - 风险等级：问题本身的严重度，用风险色；低等级也不使用成功色。
 * 旧调用方会直接把 tone（如 neutral/warning）当作 status 传入，这里一并兼容。
 */
const EXECUTION_TONES: Record<string, string> = {
  pending: "neutral", queued: "neutral", planned: "neutral", skipped: "neutral",
  cancelled: "neutral", abandoned: "neutral", superseded: "neutral", stale: "neutral",
  running: "info", reviewing: "info", planning: "info", validating: "info",
  resolving_evidence: "info", verifying_issues: "info", generating_patches: "info",
  completed: "success", passed: "success",
  failed: "danger", validation_failed: "danger", infrastructure_error: "danger",
  timed_out: "warning", completed_with_warnings: "warning", warning: "warning",
  validation_pending: "warning", waiting_for_human: "warning",
};

const EVIDENCE_TONES: Record<string, string> = {
  // 静态核验已确认的是“有证据的结论”，不是审查成功；避免与绿色成功标签叠加。
  confirmed: "info", published: "info", verified: "info", evidence_resolved: "info",
  // 尚未定论或仍需人工确认，保持中性提示色，不冒充通过。
  unresolved: "purple", needs_human: "purple", candidate: "purple",
  inconclusive: "purple", validation_inconclusive: "purple", unverified: "neutral",
  dismissed: "neutral", unsupported: "neutral",
};

const SEVERITY_TONES: Record<string, string> = {
  critical: "danger", high: "danger", medium: "warning", low: "neutral",
};

const PASSTHROUGH_TONES = new Set(["neutral", "info", "success", "warning", "danger", "purple"]);

const tone = computed(() => EXECUTION_TONES[props.status] ?? EVIDENCE_TONES[props.status]
  ?? SEVERITY_TONES[props.status] ?? (PASSTHROUGH_TONES.has(props.status) ? props.status : "neutral"));

const text = computed(() => props.label || labels[props.status] || props.status);
</script>

<template>
  <span class="status-badge" :data-tone="tone" :title="status">{{ text }}</span>
</template>
