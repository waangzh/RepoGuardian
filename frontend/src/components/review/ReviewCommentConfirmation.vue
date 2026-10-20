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
    <p>当前内容仅适用于审查时的快照。{{ pending.status === "stale" ? "请点击“重新审查此 PR”检查新提交；历史记录不能作为当前版本的审查结论。" : "无法判断评论是否过期；请点击“重试检查”，确认远端版本后再交付。" }}</p>
    <p role="status">{{ feedback }}</p>
    <p>审查 Head：<code>{{ pending.reviewed_head_sha || "未记录 Head" }}</code></p>
    <p>继续{{ action === "copy" ? "复制" : "导出" }}会保留服务端返回的{{ pending.status === "stale" ? "旧快照" : "未知状态" }}说明。</p>
    <footer>
      <button ref="cancelButton" type="button" class="button button--ghost button--compact" autofocus @click="emit('cancel')">取消</button>
      <button type="button" class="button button--secondary button--compact" @click="emit('acknowledge')">仍{{ action === "copy" ? "复制" : "导出" }}带{{ pending.status === "stale" ? "旧快照" : "未知状态" }}说明的历史记录</button>
      <button v-if="pending.status === 'stale'" type="button" class="button button--primary button--compact" @click="emit('rereview')">重新审查此 PR</button>
      <button v-else type="button" class="button button--primary button--compact" @click="emit('retry')">重试检查</button>
    </footer>
  </section>
</template>

<style scoped>
.comment-confirmation h2 { font-size: 18px; margin: 0 0 14px; }
.comment-confirmation p { line-height: 1.6; }
.comment-confirmation code { overflow-wrap: anywhere; }
.comment-confirmation footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px; margin-top: 20px; }
</style>
