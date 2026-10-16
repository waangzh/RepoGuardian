<script setup lang="ts">
import { computed, ref, watch } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import AppIcon from "./common/AppIcon.vue";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";

const props = defineProps<{
  markdown: string | null | undefined;
  userMarkdown?: string | null;
  commentsMarkdown?: string;
  filenamePrefix?: string;
}>();

const copied = ref(false);
const feedback = ref("");
const reportView = ref<"user" | "audit">("user");
const currentMarkdown = computed(() => props.userMarkdown && reportView.value === "user" ? props.userMarkdown : props.markdown);
const currentLabel = computed(() => props.userMarkdown && reportView.value === "user" ? "用户审查报告" : "原始审计报告");
watch(currentMarkdown, () => { copied.value = false; feedback.value = ""; });
const reportDialog = ref<HTMLDialogElement | null>(null);

const renderedMarkdown = computed(() => {
  const html = marked.parse(currentMarkdown.value || "", {
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
  if (!currentMarkdown.value) return;
  try {
    await navigator.clipboard.writeText(currentMarkdown.value);
    copied.value = true;
    feedback.value = "当前报告已复制。";
  } catch { copied.value = false; feedback.value = "复制失败，请查看完整报告或导出 Markdown 文件。"; }
}

function exportMarkdown(comments = false) {
  const content = comments ? props.commentsMarkdown : currentMarkdown.value;
  if (!content) return;
  const prefix = (props.filenamePrefix || "repoguardian").replace(/[^a-z0-9_-]/gi, "-").slice(0, 80);
  const kind = comments ? "comments" : props.userMarkdown && reportView.value === "user" ? "report" : "audit";
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${prefix}-${kind}.md`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <section id="review-report" class="panel report-panel">
    <PanelHeader icon="report" :title="currentLabel" subtitle="预览、复制与本地导出">
      <div class="report-actions">
        <button type="button" class="button button--ghost button--compact" :disabled="!currentMarkdown" @click="reportDialog?.showModal()">查看完整报告</button>
        <button type="button" class="button button--secondary button--compact" :disabled="!currentMarkdown" @click="copyMarkdown">{{ copied ? "已复制" : "复制当前报告" }}</button>
        <button type="button" class="button button--primary button--compact" :disabled="!currentMarkdown" @click="exportMarkdown()">导出当前报告</button>
      </div>
    </PanelHeader>
    <div v-if="userMarkdown" class="report-mode-switch" aria-label="报告内容选择"><button type="button" :aria-pressed="reportView === 'user'" @click="reportView = 'user'">用户报告</button><button type="button" :aria-pressed="reportView === 'audit'" @click="reportView = 'audit'">完整审计</button><span>{{ reportView === 'user' ? '结论、位置、影响与建议' : '保留运行、关系与追踪信息；历史报告保持原样' }}</span></div>
    <p v-if="feedback" class="report-feedback" role="status">{{ feedback }}</p>
    <EmptyState v-if="!currentMarkdown" icon="report" title="报告尚未生成" description="当前没有可用的报告记录；请结合任务状态判断。" />
    <article v-else class="report-document markdown-body" v-html="renderedMarkdown" />
    <details v-if="commentsMarkdown" class="report-comment-export"><summary>已确认问题的评论草稿</summary><p>只包含具备完整问题、有效证据和建议的已确认发现，未发布到 GitHub。</p><button type="button" class="button button--secondary button--compact" @click="exportMarkdown(true)">导出评论草稿</button><textarea readonly aria-label="全部评论草稿 Markdown" :value="commentsMarkdown" /></details>
    <dialog ref="reportDialog" class="report-dialog" @click.self="reportDialog?.close()">
      <header><strong>完整 Markdown 报告</strong><button type="button" class="icon-button" aria-label="关闭报告" @click="reportDialog?.close()"><AppIcon name="x" :size="16" /></button></header>
      <article class="report-document report-document--full markdown-body" v-html="renderedMarkdown" />
    </dialog>
  </section>
</template>
