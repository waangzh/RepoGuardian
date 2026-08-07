<script setup lang="ts">
import type {
  ProjectProfile,
  ValidationDelta,
  ValidationResult,
  ValidationSnapshot,
} from "../types/review";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";
import StatusBadge from "./common/StatusBadge.vue";

defineProps<{
  profile?: ProjectProfile | null;
  snapshots: ValidationSnapshot[];
  deltas: ValidationDelta[];
  results: ValidationResult[];
}>();

const stageLabel: Record<ValidationSnapshot["stage"], string> = {
  base: "Base",
  head: "Head",
  patched: "Patched",
};

function commandSummary(snapshot: ValidationSnapshot): string {
  return snapshot.command_results.map((result) => result.command).join(" · ") || "未运行命令";
}

function validationStatusText(status: ValidationResult["status"]): string {
  const labels: Record<string, string> = {
    unsupported: "验证环境不支持",
    passed: "已通过所选验证后端",
    failed: "测试失败",
    infrastructure_error: "验证基础设施失败",
    timed_out: "验证超时",
    inconclusive: "验证结果不确定",
    cancelled: "验证已取消",
  };
  return labels[status] ?? `未知验证状态：${status}`;
}

function short(value?: string | null): string {
  return value?.slice(0, 8) || "—";
}

function time(value?: string | null): string {
  return value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value)) : "—";
}
</script>

<template>
  <section class="panel validation-panel">
    <PanelHeader icon="flask" title="验证账本" subtitle="Validation Ledger"><span class="panel-count">{{ results.length }} 条记录</span></PanelHeader>

    <p v-if="profile" class="validation-profile">
      {{ profile.adapter_id }} / {{ profile.language }}
      <span v-if="profile.detected_files.length">· {{ profile.detected_files.join("、") }}</span>
    </p>
    <EmptyState v-if="!results.length" icon="flask" title="尚无验证结果" description="候选补丁进入所选验证后端后，可信结果会记录在这里。" />
    <div v-else class="validation-table-wrap">
      <table class="validation-table">
        <thead><tr><th>Backend / Patch</th><th>状态</th><th>可信度</th><th>SHA</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="result in results" :key="result.id">
            <td><strong>{{ result.backend }}</strong><small>{{ result.profile || "默认 Profile" }} · {{ result.patch_id ? `Patch ${short(result.patch_id)}` : "无 Patch" }}</small></td>
            <td><StatusBadge :status="result.status" /><small>{{ validationStatusText(result.status) }}</small></td>
            <td><strong>{{ result.trusted ? "可信结果" : "尚未确认" }}</strong><small>{{ result.trust_source || "无信任来源" }}</small></td>
            <td><code>H {{ short(result.head_sha) }}</code><code>P {{ short(result.patch_sha) }}</code></td>
            <td><small>{{ time(result.started_at) }}</small><small>{{ time(result.completed_at) }}</small></td>
          </tr>
        </tbody>
      </table>
      <p v-for="result in results.filter((item) => item.validation_request_id)" :key="`${result.id}:request`" class="request-id">Request {{ result.validation_request_id }}</p>
    </div>

    <details v-if="snapshots.length || deltas.length" class="validation-history">
      <summary>查看历史快照和结果变化（{{ snapshots.length + deltas.length }}）</summary>
      <article v-for="snapshot in snapshots" :key="`${snapshot.stage}:${snapshot.patch_id || snapshot.sha}`" class="validation-history__row">
        <div><strong>{{ stageLabel[snapshot.stage] }}</strong><StatusBadge :status="snapshot.passed ? 'passed' : 'failed'" /></div>
        <p>{{ commandSummary(snapshot) }}</p><small>SHA {{ short(snapshot.sha) }}<template v-if="snapshot.failure_kind"> · {{ snapshot.failure_kind }}</template></small>
      </article>
      <article v-for="delta in deltas" :key="`${delta.from_stage}:${delta.to_stage}:${delta.patch_id || ''}`" class="validation-history__row">
        <div><strong>{{ stageLabel[delta.from_stage] }} → {{ stageLabel[delta.to_stage] }}</strong><StatusBadge :status="delta.introduced_failure ? 'failed' : delta.resolved_failure ? 'passed' : 'pending'" /></div>
        <p>{{ delta.introduced_failure ? "新增失败" : delta.resolved_failure ? "已解决失败" : "无状态变化" }}</p>
      </article>
    </details>
  </section>
</template>
