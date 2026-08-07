<script setup lang="ts">
import { computed } from "vue";
import type { ReviewTask } from "../../types/review";
import MetricCard from "../common/MetricCard.vue";

const props = defineProps<{ task: ReviewTask | null }>();

const completedUnits = computed(() => props.task?.review_unit_results.filter((item) => item.status === "completed").length || 0);
const totalUnits = computed(() => props.task?.review_units.length || 0);
const unitProgress = computed(() => totalUnits.value ? Math.round((completedUnits.value / totalUnits.value) * 100) : 0);
const issueSummary = computed(() => {
  const issues = props.task?.issues || [];
  return {
    total: issues.length,
    high: issues.filter((item) => item.severity === "high" || item.severity === "critical").length,
    medium: issues.filter((item) => item.severity === "medium").length,
    low: issues.filter((item) => item.severity === "low").length,
  };
});
const patchSummary = computed(() => {
  const patches = props.task?.patches || [];
  return {
    total: patches.length,
    pending: patches.filter((item) => ["suggested", "unverified", "validation_pending"].includes(item.status)).length,
    verified: patches.filter((item) => item.status === "verified").length,
  };
});
const validation = computed(() => {
  const latest = props.task?.validation.at(-1);
  if (latest) return { value: latest.status, detail: latest.trusted ? "可信验证结果" : "结果尚未确认" };
  if (props.task?.patches.some((item) => item.status === "validation_pending")) return { value: "pending", detail: "等待验证结果" };
  return { value: "—", detail: "无验证记录" };
});
</script>

<template>
  <section class="review-metrics" aria-label="审查概览">
    <MetricCard icon="units" label="审查单元" :value="`${completedUnits} / ${totalUnits}`" :detail="`已完成 ${unitProgress}%`" :progress="unitProgress" />
    <MetricCard icon="alert" label="发现问题" :value="issueSummary.total" :detail="`高 ${issueSummary.high} / 中 ${issueSummary.medium} / 低 ${issueSummary.low}`" tone="warning" />
    <MetricCard icon="patch" label="候选补丁" :value="patchSummary.total" :detail="`待验证 ${patchSummary.pending} · 已验证 ${patchSummary.verified}`" tone="info" />
    <MetricCard icon="flask" label="验证状态" :value="validation.value" :detail="validation.detail" tone="purple" />
  </section>
</template>
