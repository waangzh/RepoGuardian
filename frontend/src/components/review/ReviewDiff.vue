<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import type { ChangedFile, EvidenceAnchor } from "../../types/review";
import { evidenceSide, highlightedLine } from "../../utils/workspacePresentation";
import EmptyState from "../common/EmptyState.vue";
import AppIcon from "../common/AppIcon.vue";
import { getFileContext } from "../../api/client";
import { completeDiff, diffDisplayRows, type DiffGap, type ExpandedContext, type FileContext } from "../../utils/diffContext";
const props = defineProps<{ file?: ChangedFile | null; evidence?: EvidenceAnchor | null; taskId?: string; fileId?: number;
  snapshot?: { head_sha: string; effective_base_sha: string } | null }>();
const source = ref<FileContext | null>(null);
const expanded = ref<ExpandedContext>({});
const loading = ref(false);
const error = ref("");
let controller: AbortController | null = null;
const expandable = computed(() => !!props.taskId && props.fileId != null && !!props.file?.hunks.length);
const rows = computed(() => props.file && expandable.value ? diffDisplayRows(props.file, source.value, expanded.value) : []);
watch([() => props.taskId, () => props.fileId, () => props.file?.file_path,
  () => props.snapshot?.head_sha, () => props.snapshot?.effective_base_sha], () => {
  controller?.abort(); controller = null; source.value = null; expanded.value = {}; error.value = ""; loading.value = false;
});
onBeforeUnmount(() => controller?.abort());
async function expand(gap: DiffGap, direction: "up" | "down") {
  if (!props.file || !props.taskId || props.fileId == null || loading.value) return;
  let target = gap;
  error.value = "";
  if (!gap.lines && !source.value) {
    const request = new AbortController(); controller = request; loading.value = true;
    try {
      const value = await getFileContext(props.taskId, props.fileId, request.signal);
      if (request.signal.aborted || controller !== request) return;
      if (!value || !value.snapshot || value.file_path !== props.file.file_path || value.file_id !== props.fileId || !props.snapshot ||
          value.snapshot.head_sha !== props.snapshot.head_sha || value.snapshot.effective_base_sha !== props.snapshot.effective_base_sha ||
          !Array.isArray(value.base_lines) || !Array.isArray(value.head_lines) ||
          !value.base_lines.every(line => typeof line === "string") || !value.head_lines.every(line => typeof line === "string")) {
        throw new Error("返回的上下文不属于当前任务快照");
      }
      completeDiff(props.file, value);
      source.value = value;
      target = rows.value.find((row): row is DiffGap => row.type === "gap" &&
        row.oldStart <= gap.oldStart && row.newStart <= gap.newStart &&
        row.oldStart + (row.count || 0) > gap.oldStart && row.newStart + (row.count || 0) > gap.newStart) || gap;
      if (!rows.value.some(row => row.type === "gap" && row.key === target.key)) return;
    } catch (cause) {
      if (!request.signal.aborted) error.value = cause instanceof Error ? cause.message : "上下文读取失败，请重试";
      return;
    } finally { if (controller === request) loading.value = false; }
  }
  const current = expanded.value[target.key] || { up: 0, down: 0 };
  expanded.value = { ...expanded.value, [target.key]: { ...current, [direction]: current[direction] + 20 } };
}
const side = computed(() => props.evidence ? evidenceSide(props.evidence) : null);
const snippet = computed(() => {
  const evidence = props.evidence;
  if (!evidence?.existing_code) return [];
  const start = evidence.resolution_status !== "unresolved" ? evidence.resolved_start_line : null;
  return evidence.existing_code.split("\n").map((content, index) => ({ content, line: start && side.value ? start + index : null }));
});
</script>
<template>
  <details :key="file?.file_path || evidence?.file_path" class="card rg-diff" open>
    <summary class="rg-diff-header">
      <AppIcon name="chevron-down" :size="16" class="rg-diff-chevron" />
      <AppIcon name="file-code" :size="16" />
      <span class="rg-diff-path mono break-anywhere">{{ file?.file_path || evidence?.file_path || '代码证据' }}<small v-if="file?.old_file_path">原路径：{{ file.old_file_path }}</small></span>
      <span v-if="file" class="rg-diff-stats" aria-label="变更行数"><span class="rg-diff-added">+{{ file.additions }}</span><span class="rg-diff-deleted">−{{ file.deletions }}</span></span>
      <span v-else class="rg-diff-caption">证据原文 · 不是完整文件</span>
      <span v-if="evidence" class="badge blue">{{ side === 'base' ? 'BASE' : side === 'head' ? 'HEAD' : '侧别未定位' }}</span>
    </summary>
    <div v-if="file?.hunks.length" class="rg-code-scroll" :class="{ 'rg-context-scroll': expandable }" role="region" aria-label="代码差异" tabindex="0">
      <div class="rg-diff-table">
      <template v-if="expandable">
        <div class="rg-hunk mono"><span class="rg-hunk-gutter" aria-hidden="true">···</span><code>代码差异 · Base → Head</code></div>
        <template v-for="(row, index) in rows" :key="row.type === 'gap' ? row.key : `line-${row.line.old_line_no}-${row.line.new_line_no}-${index}`">
          <div v-if="row.type === 'gap'" class="rg-hunk rg-context-gap">
            <span class="rg-context-controls"><button v-if="row.up" type="button" :disabled="loading" class="rg-context-expand" aria-label="向上展开 20 行上下文" title="向上展开 20 行上下文" @click="expand(row, 'up')"><AppIcon name="chevron-down" :size="16" class="rg-context-up" /></button><button v-if="row.down" type="button" :disabled="loading" class="rg-context-expand" aria-label="向下展开 20 行上下文" title="向下展开 20 行上下文" @click="expand(row, 'down')"><AppIcon name="chevron-down" :size="16" /></button></span>
            <span class="rg-context-label">{{ loading ? '正在读取固定快照…' : row.count == null ? '展开后续上下文' : `已折叠 ${row.count} 行未修改代码` }}</span>
          </div>
          <div v-else class="rg-code-line" :data-kind="row.line.kind"><span class="ln" aria-label="旧行号">{{ row.line.old_line_no ?? '' }}</span><span class="ln" aria-label="新行号">{{ row.line.new_line_no ?? '' }}</span><span class="sign">{{ row.line.kind === 'added' ? '+' : row.line.kind === 'deleted' ? '-' : ' ' }}</span><code>{{ row.line.content || ' ' }}</code></div>
        </template>
      </template>
      <template v-for="hunk in expandable ? [] : file.hunks" :key="hunk.hunk_id">
        <div class="rg-hunk mono"><span class="rg-hunk-gutter" aria-hidden="true">···</span><code>@@ -{{ hunk.old_start }},{{ hunk.old_length }} +{{ hunk.new_start }},{{ hunk.new_length }} @@</code></div>
        <div v-for="(line, index) in hunk.lines" :key="`${hunk.hunk_id}-${index}`" class="rg-code-line" :data-kind="line.kind" :class="{ highlighted: highlightedLine(evidence, line.old_line_no, line.new_line_no) }"><span class="ln" aria-label="旧行号">{{ line.old_line_no ?? '' }}</span><span class="ln" aria-label="新行号">{{ line.new_line_no ?? '' }}</span><span class="sign">{{ line.kind === 'added' ? '+' : line.kind === 'deleted' ? '-' : ' ' }}</span><code>{{ line.content || ' ' }}</code></div>
      </template>
      </div>
    </div>
    <div v-else-if="snippet.length" class="rg-code-scroll" role="region" aria-label="证据片段" tabindex="0"><div class="rg-diff-table"><div v-for="(line, index) in snippet" :key="index" class="rg-code-line"><span class="ln" aria-label="旧行号">{{ side === 'base' ? line.line : '' }}</span><span class="ln" aria-label="新行号">{{ side === 'head' ? line.line : '' }}</span><span class="sign" /><code>{{ line.content || ' ' }}</code></div></div></div>
    <EmptyState v-else icon="file-code" :title="file?.is_binary ? '二进制文件没有文本 Diff' : '没有可展示的文本差异'" description="重命名、元数据变更或证据缺失不能被当作代码已检查。" />
    <p v-if="error" class="rg-context-error" role="alert">{{ error }}。可再次点击箭头重试；当前已保存的 Diff 保持可读。</p>
    <p class="rg-diff-footnote">行号与侧别来自任务快照。代码定位与静态核验分别记录，不代表已运行测试。</p>
  </details>
</template>
