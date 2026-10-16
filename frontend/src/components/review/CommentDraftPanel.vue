<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { runtimeCommentDraft, type RuntimeFindingRow } from "../../utils/taskRuntimePresentation";
const props = defineProps<{ finding: RuntimeFindingRow; headSha?: string }>();
const markdown = computed(() => runtimeCommentDraft(props.finding, props.headSha));
const copied = ref(false);
const copyError = ref(false);
watch(markdown, () => { copied.value = false; copyError.value = false; });
async function copyDraft() {
  if (!markdown.value) return;
  try { await navigator.clipboard.writeText(markdown.value); copied.value = true; copyError.value = false; }
  catch { copyError.value = true; copied.value = false; }
}
</script>
<template>
  <section v-if="markdown" class="comment-draft" aria-label="PR 评论草稿">
    <header><div><h3>PR 评论草稿</h3><p>已确认问题 · 尚未发布到 GitHub</p></div><button type="button" class="button button--secondary button--compact" @click="copyDraft">{{ copied ? '已复制' : '复制评论' }}</button></header>
    <div class="comment-draft__preview"><strong>{{ finding.title }}</strong><code>{{ finding.filePath }}<template v-if="finding.line">:{{ finding.line }}</template></code><p v-if="finding.affectedBehavior">{{ finding.affectedBehavior }}</p><p>{{ finding.failureScenario }}</p><p>建议：{{ finding.recommendation }}</p></div>
    <p class="runtime-caption" role="status">{{ copyError ? '复制失败，可展开 Markdown 原文手动复制。' : copied ? '评论已复制，请在原 PR 核对后发布。' : '复制只写入剪贴板；不会自动提交或发布评论。' }}</p>
    <details class="runtime-technical" :open="copyError"><summary>Markdown 原文</summary><textarea readonly aria-label="评论草稿 Markdown" :value="markdown" /></details>
  </section>
</template>
