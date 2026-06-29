<script setup lang="ts">
import type { ContextSnippet } from "../types/review";

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
  <section class="panel">
    <h2>审查上下文 ({{ snippets.length }})</h2>
    <div v-if="snippets.length === 0" class="muted">暂无上下文</div>
    <details v-for="(s, i) in snippets" :key="i" class="snippet">
      <summary>
        <span class="relevance-tag" :data-kind="s.relevance">
          {{ relevanceLabel[s.relevance] || s.relevance }}
        </span>
        <code>{{ s.file }}</code>
        <span v-if="s.symbol" class="symbol-name">{{ s.symbol }}</span>
        <small>L{{ s.start_line }}–L{{ s.end_line }}</small>
      </summary>
      <pre><code>{{ s.content }}</code></pre>
    </details>
  </section>
</template>

<style scoped>
.muted { color: #888; }
.snippet { margin-bottom: 0.5rem; }
.snippet summary { cursor: pointer; display: flex; align-items: center; gap: 0.5rem; }
.relevance-tag { font-size: 0.75rem; padding: 0 4px; border-radius: 2px; }
.relevance-tag[data-kind="direct"] { background: #d4edda; color: #155724; }
.relevance-tag[data-kind="caller"] { background: #d1ecf1; color: #0c5460; }
.relevance-tag[data-kind="test"] { background: #fff3cd; color: #856404; }
.relevance-tag[data-kind="adjacent"] { background: #e2e3e5; color: #383d41; }
.symbol-name { color: #6f42c1; font-size: 0.85rem; }
pre { background: #f5f5f5; padding: 0.5rem; border-radius: 4px; overflow-x: auto; max-height: 300px; }
pre code { font-size: 0.8rem; }
</style>
