<script setup lang="ts">
defineProps<{
  icon?: string;
  title: string;
  subtitle?: string;
}>();
</script>

<template>
  <header class="panel-header">
    <div class="panel-header__title">
      <span v-if="icon" class="panel-header__icon" aria-hidden="true">{{ icon }}</span>
      <div>
        <h2>{{ title }}</h2>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
    </div>
    <div class="panel-header__meta"><slot /></div>
  </header>
</template>
