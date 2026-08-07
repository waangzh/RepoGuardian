<script setup lang="ts">
import AppIcon from "../common/AppIcon.vue";

defineProps<{
  statusText: string;
  statusEnglish: string;
  status: string;
}>();
</script>

<template>
  <header class="app-header">
    <div class="app-brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" role="img"><path d="M12 2.5 20 5v6.1c0 5.2-3.3 8.5-8 10.4-4.7-1.9-8-5.2-8-10.4V5l8-2.5Zm0 3L7 7v4.1c0 3.5 1.9 5.8 5 7.3V5.5Z" /></svg>
      </div>
      <strong>RepoGuardian</strong>
      <span class="app-brand__divider" />
      <span class="app-brand__product">PR Review Control Desk</span>
    </div>
    <div class="app-header__actions">
      <div class="header-status" :data-status="status" role="status">
        <span class="header-status__dot" />
        <span><strong>{{ statusText }}</strong><small>{{ statusEnglish }}</small></span>
      </div>
      <a class="icon-button" href="https://github.com" target="_blank" rel="noreferrer" aria-label="打开 GitHub 帮助"><AppIcon name="help" :size="17" /></a>
      <button class="user-button" type="button" disabled aria-label="用户设置暂不可用">RG</button>
    </div>
  </header>
</template>
