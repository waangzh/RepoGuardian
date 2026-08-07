<script setup lang="ts">
import AppIcon from "./AppIcon.vue";

defineProps<{
  icon?: string;
  title: string;
  subtitle?: string;
}>();
</script>

<template>
  <header class="panel-header">
    <div class="panel-header__title">
      <span v-if="icon" class="panel-header__icon"><AppIcon :name="icon" :size="15" /></span>
      <div>
        <h2>{{ title }}</h2>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
    </div>
    <div class="panel-header__meta"><slot /></div>
  </header>
</template>
