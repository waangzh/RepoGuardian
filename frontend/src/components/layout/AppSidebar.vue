<script setup lang="ts">
import type { AppPage } from "../../types/operations";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{
  modelValue: AppPage;
  hasReview?: boolean;
}>();

const emit = defineEmits<{
  "update:modelValue": [page: AppPage];
}>();

const items = () => [
  { id: "dashboard", icon: props.hasReview ? "dashboard" : "play", label: props.hasReview ? "当前审查" : "新审查" },
  { id: "history", icon: "history", label: "审查历史" },
  { id: "validation", icon: "server", label: "验证后端" },
  { id: "settings", icon: "settings", label: "系统" },
] satisfies Array<{ id: AppPage; icon: string; label: string }>;
</script>

<template>
  <nav class="app-sidebar" aria-label="主导航">
    <button
      v-for="item in items()"
      :key="item.label"
      type="button"
      class="sidebar-item"
      :class="{ 'is-active': modelValue === item.id }"
      :aria-current="modelValue === item.id ? 'page' : undefined"
      :title="item.label"
      @click="emit('update:modelValue', item.id)"
    >
      <span><AppIcon :name="item.icon" :size="19" /></span>
      <small>{{ item.label }}</small>
    </button>
    <div class="sidebar-spacer" />
    <a class="sidebar-item sidebar-help" href="https://github.com" target="_blank" rel="noreferrer">
      <span><AppIcon name="help" :size="19" /></span><small>帮助</small>
    </a>
  </nav>
</template>
