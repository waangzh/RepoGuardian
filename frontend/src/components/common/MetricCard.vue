<script setup lang="ts">
defineProps<{
  icon: string;
  label: string;
  value: string | number;
  detail: string;
  tone?: "primary" | "info" | "warning" | "purple";
  progress?: number;
}>();
</script>

<template>
  <article class="metric-card" :data-tone="tone || 'primary'">
    <div class="metric-card__icon" aria-hidden="true">{{ icon }}</div>
    <div class="metric-card__content">
      <span>{{ label }}</span>
      <strong>{{ value }}</strong>
      <p>{{ detail }}</p>
      <div v-if="progress !== undefined" class="progress" aria-label="完成进度">
        <span :style="{ width: `${Math.max(0, Math.min(progress, 100))}%` }" />
      </div>
    </div>
  </article>
</template>
