<script setup lang="ts">
import AppIcon from "./AppIcon.vue";

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
    <div class="metric-card__icon"><AppIcon :name="icon" :size="22" /></div>
    <div class="metric-card__content">
      <span>{{ label }}</span>
      <strong>{{ value }}</strong>
      <p>{{ detail }}</p>
      <div v-if="progress !== undefined" class="progress" role="progressbar" aria-label="完成进度" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="Math.max(0, Math.min(progress, 100))">
        <span :style="{ width: `${Math.max(0, Math.min(progress, 100))}%` }" />
      </div>
    </div>
  </article>
</template>
