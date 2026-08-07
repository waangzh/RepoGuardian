<script setup lang="ts">
import { computed, ref } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import AppIcon from "./common/AppIcon.vue";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";

const props = defineProps<{
  markdown: string | null | undefined;
}>();

const copied = ref(false);
const reportDialog = ref<HTMLDialogElement | null>(null);

const renderedMarkdown = computed(() => {
  const html = marked.parse(props.markdown || "", {
    async: false,
    gfm: true,
    breaks: false,
  }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style"],
  });
});

async function copyMarkdown() {
  if (!props.markdown) return;
  await navigator.clipboard.writeText(props.markdown);
  copied.value = true;
  window.setTimeout(() => { copied.value = false; }, 1400);
}
</script>

<template>
  <section id="review-report" class="panel report-panel">
    <PanelHeader icon="report" title="报告预览" subtitle="Markdown Report">
      <div class="report-actions">
        <button type="button" class="button button--ghost button--compact" :disabled="!markdown" @click="reportDialog?.showModal()">查看完整报告</button>
        <button type="button" class="button button--secondary button--compact" :disabled="!markdown" @click="copyMarkdown">{{ copied ? "已复制" : "复制 Markdown" }}</button>
      </div>
    </PanelHeader>
    <EmptyState v-if="!markdown" icon="report" title="报告正在生成" description="审查流程完成后，结构化 Markdown 报告会显示在这里。" />
    <article v-else class="report-document markdown-body" v-html="renderedMarkdown" />
    <dialog ref="reportDialog" class="report-dialog" @click.self="reportDialog?.close()">
      <header><strong>完整 Markdown 报告</strong><button type="button" class="icon-button" aria-label="关闭报告" @click="reportDialog?.close()"><AppIcon name="x" :size="16" /></button></header>
      <article class="report-document report-document--full markdown-body" v-html="renderedMarkdown" />
    </dialog>
  </section>
</template>
