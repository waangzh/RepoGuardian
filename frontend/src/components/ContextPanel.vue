<script setup lang="ts">
import type { ContextSnippet } from "../types/review";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";

defineProps<{
  snippets: ContextSnippet[];
}>();

const relevanceLabel: Record<string, string> = {
  direct: "直接变更",
  caller: "调用方",
  test: "测试关联",
  adjacent: "相邻代码",
};
</script>

<template>
  <section class="panel context-panel">
    <PanelHeader icon="context" title="审查上下文" subtitle="Context Snippets"><span class="panel-count">{{ snippets.length }} 条</span></PanelHeader>
    <EmptyState v-if="snippets.length === 0" icon="context" title="未引用额外上下文" description="本次审查没有产生需要单独展示的代码上下文片段。" />
    <details v-for="(s, i) in snippets" v-else :key="i" class="snippet">
      <summary>
        <span class="relevance-tag" :data-kind="s.relevance">
          {{ relevanceLabel[s.relevance] || s.relevance }}
        </span>
        <code>{{ s.file }}</code>
        <span v-if="s.symbol" class="symbol-name">{{ s.symbol }}</span>
        <small>L{{ s.start_line }}–L{{ s.end_line }}<template v-if="s.review_unit_id"> · Unit {{ s.review_unit_id.slice(0, 8) }}</template></small>
      </summary>
      <p>{{ s.relevance }}</p>
      <p v-if="s.why_retrieved">
        {{ s.why_retrieved }}
        <template v-if="s.confidence != null"> · 置信度 {{ Math.round(s.confidence * 100) }}%</template>
        <template v-if="s.distance != null"> · 距离 {{ s.distance }}</template>
      </p>
      <pre><code>{{ s.content }}</code></pre>
    </details>
  </section>
</template>
