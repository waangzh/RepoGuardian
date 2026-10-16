<script setup lang="ts">
import AppIcon from "../common/AppIcon.vue";
import { usePreferences } from "../../composables/usePreferences";
defineProps<{ title: string; statusText: string; menuOpen: boolean }>();
const emit = defineEmits<{ menu: [] }>();
const { theme, toggleTheme } = usePreferences();
</script>

<template>
  <header class="topbar">
    <div class="breadcrumbs">
      <button type="button" class="icon-btn menu-btn" aria-label="切换导航" aria-controls="workspace-sidebar" :aria-expanded="menuOpen" @click="emit('menu')"><AppIcon name="menu" /></button>
      <span class="crumb-parent">RepoGuardian</span><span class="sep">/</span><strong>{{ title }}</strong>
    </div>
    <div class="top-actions">
      <span class="environment">● 只读静态审查</span><span class="top-status" role="status">{{ statusText }}</span>
      <button type="button" class="icon-btn" :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'" @click="toggleTheme"><AppIcon :name="theme === 'light' ? 'moon' : 'sun'" /></button>
      <span class="avatar" aria-label="本地工作区">RG</span>
    </div>
  </header>
</template>
