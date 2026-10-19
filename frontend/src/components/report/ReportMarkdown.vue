<script setup lang="ts">
import { computed } from 'vue';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
const props = defineProps<{ content: string }>();
const rendered = computed(() => DOMPurify.sanitize(marked.parse(props.content, { async: false, gfm: true, breaks: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ['style'] }));
</script>
<template><article class="rd-markdown markdown-body" v-html="rendered" /></template>
