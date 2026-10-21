<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ReviewTask } from "../../types/review";
import type { ChangeUnderstanding } from "../../types/publication";
import { prChangeSummary } from "../../utils/prChangeSummary";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{
  task: ReviewTask;
  report?: string | null;
  understanding?: ChangeUnderstanding | null;
  authorLimitation?: string | null;
  compact?: boolean;
}>();
const emit = defineEmits<{ file: [path: string]; evidence: [] }>();
const overview = computed(() => prChangeSummary(props.task, props.report));
const authorOpen = ref(false);
const authorMarkdown = computed(() => props.task.pr?.body?.trim() || "");
const authorHtml = computed(() => DOMPurify.sanitize(marked.parse(authorMarkdown.value, { async: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ["style", "img"] }));
watch(() => props.task.id, () => { authorOpen.value = false; });
</script>

<template>
  <section class="card card-pad rg-change-summary" :class="{ 'ov-change': compact }" aria-label="PR 变更摘要">
    <template v-if="compact">
      <dl class="ov-change__rows">
        <div><dt><AppIcon name="file" :size="16" />作者想解决什么</dt><dd>{{ understanding?.author_goal || task.pr?.title || "未提供作者目标。" }} <span class="ov-tag">作者声明 · 未独立验证</span></dd></div>
        <div><dt><AppIcon name="code" :size="16" />代码实际改了什么</dt><dd>{{ understanding?.actual_changes || "未提供结构化代码观察，请结合 Diff 与详细报告核对。" }} <span v-if="understanding?.actual_changes" class="ov-tag">来自静态检查</span></dd></div>
        <div><dt><AppIcon name="help" :size="16" />吻合与未确认</dt><dd>{{ understanding?.alignment || "未提供结构化契约对照，无法据此确认作者目标与全部代码行为吻合。" }}</dd></div>
      </dl>
      <p v-if="authorLimitation" class="ov-change__limitation">{{ authorLimitation }}</p>
      <div class="ov-change__footer">
        <div class="ov-file-tags">
          <button v-for="file in task.changed_files.slice(0, 3)" :key="file.file_path" type="button" :title="file.file_path" @click="emit('file', file.file_path)">{{ file.file_path }}</button>
          <details v-if="task.changed_files.length > 3"><summary>其余 {{ task.changed_files.length - 3 }} 个文件</summary><button v-for="file in task.changed_files.slice(3)" :key="file.file_path" type="button" @click="emit('file', file.file_path)">{{ file.file_path }}</button></details>
          <span v-if="!task.changed_files.length">暂无变更文件记录</span>
        </div>
        <button type="button" class="ov-link" @click="emit('evidence')">查看证据依据 <AppIcon name="arrow-right" :size="14" /></button>
      </div>
    </template>
    <div v-if="!compact" class="card-header"><div><h2>本次 PR 做了什么</h2><p>{{ understanding ? '作者声明、代码行为与未确认事项分别陈述' : overview.subtitle }}</p></div></div>
    <template v-if="!compact && understanding">
      <dl class="rg-understanding">
        <div><dt>作者想解决什么</dt><dd>{{ understanding.author_goal }}</dd></div>
        <div><dt>代码实际改变了什么</dt><dd>{{ understanding.actual_changes }}</dd></div>
        <div><dt>吻合与未确认</dt><dd>{{ understanding.alignment }}</dd></div>
      </dl>
      <p v-if="authorLimitation" class="notice warn space-top" role="status">{{ authorLimitation }}</p>
      <p class="small muted space-top">以上归纳来自已绑定证据的检查记录；未覆盖路径仍需结合详细报告核对。</p>
    </template>
    <p v-else-if="!compact" class="small rg-pr-summary">{{ overview.text }}</p>
    <div v-if="!compact" class="facts">
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
