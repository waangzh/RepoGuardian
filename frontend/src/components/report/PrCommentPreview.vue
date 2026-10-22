<script setup lang="ts">
import { computed } from 'vue';
import { parseCommentPreview } from '../../utils/commentPreview';
import AppIcon from '../common/AppIcon.vue';
import ReportMarkdown from './ReportMarkdown.vue';
import type { ReviewPresentation } from '../../types/publication';

const props = defineProps<{ content: string; findingCount?: number | null; presentation?: ReviewPresentation | null }>();
const document = computed(() => props.presentation ? null : parseCommentPreview(props.content));
</script>

<template>
  <article v-if="presentation" class="rd-pr-comment" aria-label="PR 评论正文">
    <p class="rd-comment-intro">
      {{ presentation.synthesis_status === 'ready' ? '摘要来源：已冻结的模型编辑结果；审查判定仍来自核验事实。' : '摘要来源：已保存事实的确定性回退。' }}
      本轮已确认 {{ presentation.delivery.confirmed }} 项，当前正文包含 {{ findingCount ?? presentation.delivery.selected }} 项。
    </p>
    <ReportMarkdown :content="content" />
  </article>
  <article v-else-if="document" class="rd-pr-comment" aria-label="PR 评论正文">
    <h2>{{ document.title }}</h2>
    <div class="rd-comment-conclusion" :class="document.positive ? 'positive' : 'attention'">
      <AppIcon :name="document.positive ? 'check-circle' : 'alert'" :size="19" />
      <strong>{{ document.conclusion }}</strong>
    </div>
    <ReportMarkdown v-if="document.preamble" class="rd-comment-intro" :content="document.preamble" />
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
