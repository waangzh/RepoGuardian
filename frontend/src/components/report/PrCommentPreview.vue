<script setup lang="ts">
import { computed } from 'vue';
import { parseCommentPreview } from '../../utils/commentPreview';
import AppIcon from '../common/AppIcon.vue';
import ReportMarkdown from './ReportMarkdown.vue';

const props = defineProps<{ content: string; findingCount?: number | null }>();
const document = computed(() => parseCommentPreview(props.content));
</script>

<template>
  <article v-if="document" class="rd-pr-comment" aria-label="PR 评论正文">
    <h2>{{ document.title }}</h2>
    <div class="rd-comment-conclusion" :class="document.positive ? 'positive' : 'attention'">
      <AppIcon :name="document.positive ? 'check-circle' : 'alert'" :size="19" />
      <strong>{{ document.conclusion }}</strong>
    </div>
    <section v-for="(section, index) in document.sections" :key="index" class="rd-comment-section">
      <header>
        <h3>{{ section.title }}</h3>
        <span v-if="section.findings && (section.findingCount ?? findingCount) != null" class="rd-comment-count" :class="{ 'has-findings': (section.findingCount ?? findingCount ?? 0) > 0 }">{{ section.findingCount ?? findingCount }} 项</span>
      </header>
      <ReportMarkdown class="rd-comment-content" :content="section.markdown" />
      <div v-if="index === document.sections.length - 1 && document.snapshot" class="rd-comment-snapshot">
        <ReportMarkdown :content="document.snapshot" />
      </div>
    </section>
  </article>
  <ReportMarkdown v-else :content="content" />
</template>
