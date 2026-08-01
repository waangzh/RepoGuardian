<script setup lang="ts">
import { computed } from "vue";
import type { PatchResult, ValidationResult } from "../types/review";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";
import StatusBadge from "./common/StatusBadge.vue";

const props = defineProps<{ patches: PatchResult[]; validations: ValidationResult[] }>();

const trustedByPatch = computed(() => new Map(props.validations.filter((item) => item.patch_id).map((item) => [item.patch_id as string, item.trusted])));

function displayStatus(patch: PatchResult): string {
  if (patch.stale) return "stale";
  return patch.status;
}

function changedLines(diff: string): number {
  return diff.split("\n").filter((line) => (line.startsWith("+") && !line.startsWith("+++")) || (line.startsWith("-") && !line.startsWith("---"))).length;
}
</script>

<template>
  <section class="panel patch-panel">
    <PanelHeader icon="⌁" title="候选补丁" subtitle="Candidate Patches"><span class="panel-count">{{ patches.length }} 总计</span></PanelHeader>
    <EmptyState v-if="!patches.length" icon="⌁" title="暂无候选补丁" description="发现问题且允许生成补丁后，修复建议会显示在这里。" />
    <div v-else class="patch-list">
      <article v-for="patch in patches" :key="patch.id" class="patch-item">
        <header><div><strong>{{ patch.title }}</strong><small>{{ patch.id.slice(0, 8) }}</small></div><StatusBadge :status="displayStatus(patch)" /></header>
        <p>{{ patch.rationale }}</p>
        <div class="patch-meta">
          <span>风险 <b>{{ patch.risk }}</b></span>
          <span>文件 <b>{{ patch.touched_files.length }}</b></span>
          <span>变更行 <b>约 {{ changedLines(patch.unified_diff) }}</b></span>
          <span>Issues <b>{{ patch.issue_ids.length }}</b></span>
        </div>
        <dl>
          <div><dt>Apply-check</dt><dd>{{ patch.apply_check.status === "passed" ? "补丁可应用" : patch.apply_check.status }}</dd></div>
          <div><dt>Validation</dt><dd>{{ patch.validation_backend || "未选择后端" }}</dd></div>
          <div><dt>Trusted</dt><dd>{{ trustedByPatch.has(patch.id) ? (trustedByPatch.get(patch.id) ? "可信结果" : "尚未确认") : "无验证记录" }}</dd></div>
          <div><dt>Head / Patch SHA</dt><dd><code>{{ patch.head_sha.slice(0, 8) }}</code> / <code>{{ patch.patch_sha?.slice(0, 8) || "—" }}</code></dd></div>
        </dl>
        <p v-if="patch.stale" class="inline-warning">该补丁基于旧 Head，需要重新生成或检查。</p>
        <p v-else-if="patch.status === 'unverified' || patch.status === 'suggested'" class="inline-note">候选补丁尚未经过所选验证后端。</p>
        <p v-else-if="patch.status === 'validation_inconclusive'" class="inline-note">验证结果不确定，不能视为通过或失败。</p>
        <details class="diff-details"><summary>查看补丁 Diff</summary><pre><code>{{ patch.unified_diff }}</code></pre></details>
      </article>
    </div>
  </section>
</template>
