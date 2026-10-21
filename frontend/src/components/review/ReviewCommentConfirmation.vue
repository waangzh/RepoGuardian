<script setup lang="ts">
import { onMounted, ref, useId } from "vue";
import type { ReviewCommentCheck } from "../../types/publication";

withDefaults(defineProps<{ pending: ReviewCommentCheck; action?: "copy" | "download"; feedback: string }>(), { action: "copy" });
const emit = defineEmits<{ cancel: []; acknowledge: []; rereview: []; retry: [] }>();
const titleId = useId();
const cancelButton = ref<HTMLButtonElement | null>(null);
onMounted(() => cancelButton.value?.focus());
</script>

<template>
  <section class="comment-confirmation" :aria-labelledby="titleId">
    <h2 :id="titleId">{{ pending.status === "stale" ? "PR 已更新，本轮结论不适用于最新提交" : "暂时无法确认 PR 是否有新提交" }}</h2>
    <p>{{ pending.status === "stale" ? "PR 已有新提交。建议重新审查此 PR，获取新版本的结论。" : "请先重新检查 PR 版本，再决定是否交付这份记录。" }}</p>
    <p role="status">{{ feedback }}</p>
    <p>本次审查使用的 Commit：<code>{{ pending.reviewed_head_sha || "未记录" }}</code></p>
    <p>{{ action === "copy" ? "复制" : "导出" }}内容将{{ pending.reviewed_head_sha ? "注明本次审查使用的 Commit" : "注明未记录审查 Commit" }}，{{ pending.status === "unknown" ? "保留 PR 版本尚未确认的说明，" : "" }}不能作为最新提交的审查结论。</p>
    <footer>
      <button ref="cancelButton" type="button" class="button button--ghost button--compact" autofocus @click="emit('cancel')">取消</button>
      <button type="button" class="button button--secondary button--compact" @click="emit('acknowledge')">仍{{ action === "copy" ? "复制" : "导出" }}旧版审查记录</button>
      <button v-if="pending.status === 'stale'" type="button" class="button button--primary button--compact" @click="emit('rereview')">重新审查此 PR</button>
      <button v-else type="button" class="button button--primary button--compact" @click="emit('retry')">重新检查 PR 版本</button>
    </footer>
  </section>
</template>

<style scoped>
.comment-confirmation h2 { font-size: 18px; margin: 0 0 14px; }
.comment-confirmation p { line-height: 1.6; }
.comment-confirmation code { overflow-wrap: anywhere; }
.comment-confirmation footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px; margin-top: 20px; }
</style>
