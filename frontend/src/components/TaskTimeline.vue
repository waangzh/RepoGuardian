<script setup lang="ts">
import type { TaskStep } from "../types/review";

defineProps<{
  steps: TaskStep[];
}>();
</script>

<template>
  <ol class="timeline">
    <li v-for="step in steps" :key="step.name" class="timeline-item" :data-status="step.status">
      <span class="timeline-dot" />
      <div>
        <div class="timeline-title">{{ step.name }}</div>
        <div class="timeline-message">{{ step.message || step.status }}</div>
      </div>
    </li>
  </ol>
</template>

