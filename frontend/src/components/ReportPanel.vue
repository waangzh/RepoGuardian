<script setup lang="ts">
import { computed, ref } from "vue";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";

const props = defineProps<{
  markdown: string | null | undefined;
}>();

type ReportBlock = { type: "h1" | "h2" | "h3" | "p" | "li" | "code"; text: string };
const copied = ref(false);
const reportDialog = ref<HTMLDialogElement | null>(null);

const blocks = computed<ReportBlock[]>(() => {
  const lines = (props.markdown || "").split("\n");
  const result: ReportBlock[] = [];
  let inCode = false;
  let code = "";
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) { result.push({ type: "code", text: code.trimEnd() }); code = ""; }
      inCode = !inCode;
    } else if (inCode) code += `${line}\n`;
    else if (line.startsWith("### ")) result.push({ type: "h3", text: line.slice(4) });
    else if (line.startsWith("## ")) result.push({ type: "h2", text: line.slice(3) });
    else if (line.startsWith("# ")) result.push({ type: "h1", text: line.slice(2) });
    else if (/^[-*] /.test(line)) result.push({ type: "li", text: line.slice(2) });
    else if (line.trim()) result.push({ type: "p", text: line });
  }
  if (code) result.push({ type: "code", text: code.trimEnd() });
  return result;
});

async function copyMarkdown() {
  if (!props.markdown) return;
  await navigator.clipboard.writeText(props.markdown);
  copied.value = true;
  window.setTimeout(() => { copied.value = false; }, 1400);
}
</script>

<template>
  <section class="panel report-panel">
    <PanelHeader icon="▤" title="报告预览" subtitle="Markdown Report">
      <div class="report-actions">
        <button type="button" class="button button--ghost button--compact" :disabled="!markdown" @click="reportDialog?.showModal()">查看完整报告</button>
        <button type="button" class="button button--secondary button--compact" :disabled="!markdown" @click="copyMarkdown">{{ copied ? "已复制" : "复制 Markdown" }}</button>
      </div>
    </PanelHeader>
    <EmptyState v-if="!markdown" icon="▤" title="暂无报告" description="审查完成后，Markdown 报告将在这里预览。" />
    <article v-else class="report-document">
      <template v-for="(block, index) in blocks" :key="index">
        <h1 v-if="block.type === 'h1'">{{ block.text }}</h1><h2 v-else-if="block.type === 'h2'">{{ block.text }}</h2><h3 v-else-if="block.type === 'h3'">{{ block.text }}</h3>
        <li v-else-if="block.type === 'li'">{{ block.text }}</li><pre v-else-if="block.type === 'code'"><code>{{ block.text }}</code></pre><p v-else>{{ block.text }}</p>
      </template>
    </article>
    <dialog ref="reportDialog" class="report-dialog" @click.self="reportDialog?.close()">
      <header><strong>完整 Markdown 报告</strong><button type="button" class="icon-button" aria-label="关闭报告" @click="reportDialog?.close()">×</button></header>
      <article class="report-document report-document--full">
        <template v-for="(block, index) in blocks" :key="index"><h1 v-if="block.type === 'h1'">{{ block.text }}</h1><h2 v-else-if="block.type === 'h2'">{{ block.text }}</h2><h3 v-else-if="block.type === 'h3'">{{ block.text }}</h3><li v-else-if="block.type === 'li'">{{ block.text }}</li><pre v-else-if="block.type === 'code'"><code>{{ block.text }}</code></pre><p v-else>{{ block.text }}</p></template>
      </article>
    </dialog>
  </section>
</template>
