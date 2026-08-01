<script setup lang="ts">
defineProps<{
  icon?: string;
  title: string;
  description: string;
  actionLabel?: string;
  disabled?: boolean;
}>();

defineEmits<{
  action: [];
}>();
</script>

<template>
  <div class="empty-state">
    <div class="empty-state__icon" aria-hidden="true">{{ icon || "◇" }}</div>
    <strong>{{ title }}</strong>
    <p>{{ description }}</p>
    <button
      v-if="actionLabel"
      type="button"
      class="button button--secondary button--compact"
      :disabled="disabled"
      @click="$emit('action')"
    >
      {{ actionLabel }}
    </button>
  </div>
</template>
