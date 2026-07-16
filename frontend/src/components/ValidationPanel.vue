<script setup lang="ts">
import type { ProjectProfile, ValidationDelta, ValidationSnapshot } from "../types/review";

defineProps<{
  profile?: ProjectProfile | null;
  snapshots: ValidationSnapshot[];
  deltas: ValidationDelta[];
}>();

const stageLabel: Record<ValidationSnapshot["stage"], string> = {
  base: "Base",
  head: "Head",
  patched: "Patched",
};

function commandSummary(snapshot: ValidationSnapshot): string {
  return snapshot.command_results.map((result) => result.command).join(" · ") || "未运行命令";
}
</script>

<template>
  <section class="panel validation-panel">
    <div class="panel-head">
      <div>
        <p class="eyebrow">Verification ledger</p>
        <h2>三阶段验证</h2>
      </div>
      <span>{{ snapshots.length }} 条</span>
    </div>

    <p v-if="profile" class="validation-profile">
      {{ profile.adapter_id }} / {{ profile.language }}
      <span v-if="profile.detected_files.length">· {{ profile.detected_files.join("、") }}</span>
    </p>
    <div v-if="snapshots.length === 0" class="empty">尚无验证快照</div>

    <article
      v-for="snapshot in snapshots"
      :key="`${snapshot.stage}:${snapshot.patch_id || snapshot.sha}:${snapshot.command_results.length}`"
      class="validation-snapshot"
      :data-pass="snapshot.passed"
    >
      <div class="validation-snapshot-head">
        <strong>{{ stageLabel[snapshot.stage] }}</strong>
        <span :data-pass="snapshot.passed">{{ snapshot.passed ? "通过" : "失败" }}</span>
      </div>
      <p>{{ commandSummary(snapshot) }}</p>
      <small>
        SHA {{ snapshot.sha.slice(0, 8) }}
        <template v-if="snapshot.patch_id"> · Patch {{ snapshot.patch_id.slice(0, 8) }}</template>
        <template v-if="snapshot.failure_kind"> · {{ snapshot.failure_kind }}</template>
      </small>
      <p v-if="snapshot.failure_detail" class="validation-detail">{{ snapshot.failure_detail }}</p>
    </article>

    <div v-if="deltas.length" class="validation-deltas">
      <h3>结果变化</h3>
      <div v-for="delta in deltas" :key="`${delta.from_stage}:${delta.to_stage}:${delta.patch_id || ''}`" class="delta-row">
        <strong>{{ stageLabel[delta.from_stage] }} → {{ stageLabel[delta.to_stage] }}</strong>
        <span v-if="delta.introduced_failure" class="delta-negative">新增失败</span>
        <span v-else-if="delta.resolved_failure" class="delta-positive">已解决</span>
        <span v-else>无状态变化</span>
        <small v-if="delta.patch_id">Patch {{ delta.patch_id.slice(0, 8) }}</small>
      </div>
    </div>
  </section>
</template>
