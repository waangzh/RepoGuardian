<script setup lang="ts">
import { computed } from "vue";
import type { ChangedFile, EvidenceAnchor } from "../../types/review";
import { evidenceSide, highlightedLine } from "../../utils/workspacePresentation";
import EmptyState from "../common/EmptyState.vue";
const props = defineProps<{ file?: ChangedFile | null; evidence?: EvidenceAnchor | null }>();
const side = computed(() => props.evidence ? evidenceSide(props.evidence) : null);
const snippet = computed(() => {
  const evidence = props.evidence;
  if (!evidence?.existing_code) return [];
  const start = evidence.resolution_status !== "unresolved" ? evidence.resolved_start_line : null;
  return evidence.existing_code.split("\n").map((content, index) => ({ content, line: start && side.value ? start + index : null }));
});
</script>
<template>
  <article class="card rg-diff">
    <header class="rg-diff-header"><div><strong class="mono break-anywhere">{{ file?.file_path || evidence?.file_path || '代码证据' }}</strong><small>{{ file ? `变更 +${file.additions} / −${file.deletions}` : '证据原文 · 不是完整文件' }}</small></div><span v-if="evidence" class="badge blue">{{ side === 'base' ? 'BASE' : side === 'head' ? 'HEAD' : '侧别未定位' }}</span></header>
    <div v-if="file?.hunks.length" class="rg-code-scroll" role="region" aria-label="代码差异" tabindex="0">
      <template v-for="hunk in file.hunks" :key="hunk.hunk_id">
        <div class="rg-hunk mono">@@ −{{ hunk.old_start }},{{ hunk.old_length }} +{{ hunk.new_start }},{{ hunk.new_length }} @@</div>
        <div v-for="(line, index) in hunk.lines" :key="`${hunk.hunk_id}-${index}`" class="rg-code-line" :data-kind="line.kind" :class="{ highlighted: highlightedLine(evidence, line.old_line_no, line.new_line_no) }"><span class="ln">{{ line.old_line_no ?? '' }}</span><span class="ln">{{ line.new_line_no ?? '' }}</span><span class="sign">{{ line.kind === 'added' ? '+' : line.kind === 'deleted' ? '−' : ' ' }}</span><code>{{ line.content || ' ' }}</code></div>
      </template>
    </div>
    <div v-else-if="snippet.length" class="rg-code-scroll" role="region" aria-label="证据片段" tabindex="0"><div v-for="(line, index) in snippet" :key="index" class="rg-code-line"><span class="ln">{{ side === 'base' ? line.line : '' }}</span><span class="ln">{{ side === 'head' ? line.line : '' }}</span><span /><code>{{ line.content || ' ' }}</code></div></div>
    <EmptyState v-else icon="file-code" :title="file?.is_binary ? '二进制文件没有文本 Diff' : '没有可展示的文本差异'" description="重命名、元数据变更或证据缺失不能被当作代码已检查。" />
    <p class="rg-diff-footnote">行号与侧别来自任务快照。代码定位与静态核验分别记录，不代表已运行测试。</p>
  </article>
</template>
