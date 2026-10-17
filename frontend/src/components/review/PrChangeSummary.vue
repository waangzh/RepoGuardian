<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ReviewTask } from "../../types/review";
import { prChangeSummary } from "../../utils/prChangeSummary";

const props = defineProps<{ task: ReviewTask; report?: string | null }>();
const overview = computed(() => prChangeSummary(props.task, props.report));
const authorOpen = ref(false);
const authorMarkdown = computed(() => props.task.pr?.body?.trim() || "");
const authorHtml = computed(() => DOMPurify.sanitize(marked.parse(authorMarkdown.value, { async: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ["style", "img"] }));
watch(() => props.task.id, () => { authorOpen.value = false; });
</script>

<template>
  <section class="card card-pad rg-change-summary" aria-label="PR 变更摘要">
    <div class="card-header"><div><h2>本次 PR 做了什么</h2><p>{{ overview.subtitle }}</p></div></div>
    <p class="small rg-pr-summary">{{ overview.text }}</p>
    <div class="facts">
      <span class="fact">{{ task.changed_files.length }} 个变更文件</span>
      <span class="fact">{{ task.review_units.length }} 个审查分组</span>
      <span v-if="overview.languages.length" class="fact">{{ overview.languages.join(' / ') }}</span>
    </div>
    <details v-if="authorMarkdown" class="rg-pr-original" :open="authorOpen" @toggle="authorOpen = ($event.target as HTMLDetailsElement).open">
      <summary>查看作者原始说明</summary>
      <div v-if="authorOpen" class="markdown-body rg-pr-original__body" v-html="authorHtml" />
    </details>
  </section>
</template>
