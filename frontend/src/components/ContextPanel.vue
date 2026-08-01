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
    <PanelHeader icon="⌘" title="审查上下文" subtitle="Context Snippets"><span class="panel-count">{{ snippets.length }} 条</span></PanelHeader>
    <EmptyState v-if="snippets.length === 0" icon="⌘" title="暂无上下文" description="审查过程中引用的代码上下文片段会显示在这里。" />
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
      <pre><code>{{ s.content }}</code></pre>
    </details>
  </section>
</template>
