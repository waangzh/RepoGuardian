<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
import { getFileContext } from '../../../api/client';
import type { ChangedFile, FileContext, ReviewTask } from '../../../types/review';
import { isFileContextFor } from '../../../utils/nativeReview';
import AppIcon from '../../common/AppIcon.vue';
import EmptyState from '../../common/EmptyState.vue';
const props = defineProps<{ file: ChangedFile; fileId: number; taskId: string; snapshot: ReviewTask['snapshot']; highlightLine?: number; highlightSide?: 'left' | 'right' }>();
const source = ref<FileContext | null>(null), loading = ref(false), error = ref('');
let controller: AbortController | null = null;
watch(() => [props.taskId,props.fileId,props.file.file_path,props.snapshot.head_sha,props.snapshot.effective_base_sha,props.snapshot.declared_base_sha], () => {
  controller?.abort(); source.value = null; error.value = ''; loading.value = false;
});
onBeforeUnmount(() => controller?.abort());
async function expand() {
  if (loading.value) return;
  const request = new AbortController(); controller?.abort(); controller = request;
  loading.value = true; error.value = '';
  try {
    const value = await getFileContext(props.taskId,props.fileId,request.signal);
    if (request.signal.aborted || controller !== request) return;
    if (!isFileContextFor(value,props.fileId,props.file.file_path,props.snapshot)) {
      throw new Error('返回的上下文不属于当前文件和固定快照');
    }
    source.value = value;
  } catch (cause) { if (!request.signal.aborted) error.value = cause instanceof Error ? cause.message : '上下文读取失败'; }
  finally { if (controller === request) loading.value = false; }
}
</script>
<template>
  <section class="card rg-diff"><header class="rg-diff-header"><AppIcon name="file-code" /><span class="rg-diff-path mono break-anywhere">{{ file.file_path }}<small v-if="file.old_file_path">原路径：{{ file.old_file_path }}</small></span><span class="rg-diff-stats"><span class="rg-diff-added">+{{ file.additions }}</span><span class="rg-diff-deleted">−{{ file.deletions }}</span></span></header>
    <div v-if="file.hunks.length" class="rg-code-scroll" role="region" aria-label="代码差异" tabindex="0"><div class="rg-diff-table"><template v-for="hunk in file.hunks" :key="hunk.hunk_id">
      <div class="rg-hunk mono"><span class="rg-hunk-gutter">···</span><code>@@ -{{ hunk.old_start }},{{ hunk.old_length }} +{{ hunk.new_start }},{{ hunk.new_length }} @@</code></div>
      <div v-for="(line,index) in hunk.lines" :key="index" class="rg-code-line" :data-kind="line.kind" :class="{ highlighted: highlightLine != null && (highlightSide === 'left' ? line.old_line_no : line.new_line_no) === highlightLine }"><span class="ln">{{ line.old_line_no ?? '' }}</span><span class="ln">{{ line.new_line_no ?? '' }}</span><span class="sign">{{ line.kind === 'added' ? '+' : line.kind === 'deleted' ? '−' : ' ' }}</span><code>{{ line.content || ' ' }}</code></div>
    </template></div></div>
    <EmptyState v-else icon="file-code" :title="file.is_binary ? '二进制文件没有文本 Diff' : '没有可展示的文本差异'" description="文件元数据与实际代码检查分别记录。" />
    <div class="rg-diff-footnote"><p>行号与侧别来自固定 Base/Head；静态核验不代表已运行测试。</p><button class="btn secondary sm" :disabled="loading || file.is_binary" @click="expand">{{ loading ? '读取中…' : '读取固定 BASE / HEAD 上下文' }}</button><p v-if="error" class="input-error" role="alert">{{ error }}。原有 Diff 保持可读，可重试。</p></div>
    <div v-if="source" class="rg-source-pair"><details v-for="side in (['base','head'] as const)" :key="side" open><summary>{{ side === 'base' ? 'BASE' : 'HEAD' }} · {{ source[`${side}_lines`].length }} 行</summary><div class="rg-code-scroll" tabindex="0" :aria-label="`${side} 固定源码`" role="region"><div class="rg-source-line" v-for="(line,index) in source[`${side}_lines`]" :key="index"><span class="ln">{{ index + 1 }}</span><code>{{ line || ' ' }}</code></div><p v-if="!source[`${side}_lines`].length" class="muted small">该侧文件为空或不存在。</p></div></details></div>
  </section>
</template>
