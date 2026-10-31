<script setup lang="ts">
import { computed } from 'vue';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
const props = defineProps<{ content: string; allowImages?: boolean }>();
// SSR（测试）没有 DOM：DOMPurify 退化为工厂函数，此时转义整段输出；浏览器端始终走 DOMPurify。
const escapeHtml = (value: string) => value.replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char] as string));
const rendered = computed(() => {
  const html = marked.parse(props.content, { async: false, gfm: true, breaks: false }) as string;
  if (typeof DOMPurify.sanitize !== 'function') return escapeHtml(html);
  return DOMPurify.sanitize(html,
    { USE_PROFILES: { html: true }, FORBID_TAGS: props.allowImages === false ? ['style', 'img'] : ['style'] });
});
</script>
<template><article class="rd-markdown markdown-body" v-html="rendered" /></template>
<style scoped>
.rd-markdown { min-width: 0; overflow-wrap: anywhere; }
.rd-markdown :deep(pre) { max-width: 100%; overflow: auto; white-space: pre; }
.rd-markdown :deep(table) { display: block; max-width: 100%; overflow-x: auto; }
.rd-markdown :deep(img) { max-width: 100%; height: auto; }
</style>
