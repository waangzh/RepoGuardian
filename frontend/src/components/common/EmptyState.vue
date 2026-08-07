<script setup lang="ts">
import AppIcon from "./AppIcon.vue";

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
    <div class="empty-state__icon"><AppIcon :name="icon || 'info'" :size="20" /></div>
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
