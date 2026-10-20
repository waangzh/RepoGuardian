<script setup lang="ts">
import type { DiscussionItem } from "../../../types/prDiscussion";
import AppIcon from "../../common/AppIcon.vue";
defineProps<{ item: DiscussionItem; canOpenFile?: boolean }>();
const emit = defineEmits<{ file: [path: string] }>();
</script>
<template>
  <h3 class="pd-section-heading"><AppIcon name="code" :size="17" />关联代码依据</h3>
  <p class="pd-caption">仅展示此次审查快照已经保存的代码片段。存在可定位片段不代表主张已经被验证。</p>
  <article v-for="(evidence, index) in item.evidence" :key="index" class="pd-evidence">
    <header><div class="pd-mono">{{ evidence.file_path }} · {{ evidence.side === 'base' ? 'Base' : evidence.side === 'head' ? 'Head' : '侧别未知' }} L{{ evidence.start_line }}–{{ evidence.end_line }}</div><span class="pd-pill blue">静态代码片段</span></header>
    <pre><code>{{ evidence.content }}</code></pre>
    <button v-if="canOpenFile" type="button" class="pd-btn" @click="emit('file', evidence.file_path)">在变更文件中查看 <AppIcon name="arrow-right" :size="14" /></button>
  </article>
  <p v-if="!item.evidence.length" class="pd-notice amber"><AppIcon name="alert" :size="17" />当前记录没有可定位的代码片段，不能据此判定问题成立或已修复。</p>
  <details v-if="item.recheck.evidence_ids.length" class="pd-limits"><summary>证据索引（{{ item.recheck.evidence_ids.length }}）</summary><ul><li v-for="id in item.recheck.evidence_ids" :key="id" class="pd-mono">{{ id }}<span v-if="!item.evidence.some(e => (e.id || e.evidence_id) === id)"> · 当前响应未提供可匹配片段</span></li></ul></details>
  <h3 class="pd-section-heading"><AppIcon name="alert" :size="17" />核验限制</h3>
  <ul v-if="item.recheck.limitations.length" class="pd-limit-list"><li v-for="(value, index) in item.recheck.limitations" :key="index">{{ value }}</li></ul>
  <p v-else class="pd-caption">未记录额外核验限制；不代表所有路径均已检查。</p>
  <p v-if="!item.complete" class="pd-notice amber">此线程原文采集不完整。</p>
  <p class="pd-aside">核验状态和证据索引来自本轮保存记录；GitHub resolved/outdated 是平台线程元数据。</p>
</template>
