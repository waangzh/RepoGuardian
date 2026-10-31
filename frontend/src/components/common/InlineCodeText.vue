<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{ text: string }>();
const parts = computed(() => {
  const result: { text: string; code: boolean }[] = [];
  const pattern = /(?<!`)`([^`\r\n]+)`(?!`)/g;
  let start = 0;
  for (const match of props.text.matchAll(pattern)) {
    result.push({ text: props.text.slice(start, match.index), code: false });
    result.push({ text: match[1]!, code: true });
    start = match.index! + match[0].length;
  }
  result.push({ text: props.text.slice(start), code: false });
  return result;
});
</script>

<template>
  <template v-for="(part, index) in parts" :key="index"><code v-if="part.code">{{ part.text }}</code><template v-else>{{ part.text }}</template></template>
</template>
