<script setup lang="ts">
defineProps<{
  markdown: string | null | undefined;
}>();
</script>

<template>
  <section class="panel report-panel">
    <div class="panel-head">
      <h2>Markdown 报告</h2>
      <span v-if="markdown">ready</span>
    </div>
    <pre v-if="markdown" class="markdown">{{ markdown }}</pre>
    <div v-else class="empty">报告生成后将在这里显示</div>
  </section>
</template>

