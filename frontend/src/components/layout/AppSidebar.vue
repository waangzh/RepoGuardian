<script setup lang="ts">
const items = [
  { icon: "⌘", label: "控制台", active: true, disabled: false },
  { icon: "◷", label: "审查历史", active: false, disabled: true },
  { icon: "✓", label: "验证后端", active: false, disabled: true },
  { icon: "⚙", label: "设置", active: false, disabled: true },
];
</script>

<template>
  <nav class="app-sidebar" aria-label="主导航">
    <button
      v-for="item in items"
      :key="item.label"
      type="button"
      class="sidebar-item"
      :class="{ 'is-active': item.active }"
      :disabled="item.disabled"
      :aria-current="item.active ? 'page' : undefined"
      :title="item.disabled ? `${item.label}暂未提供` : item.label"
    >
      <span aria-hidden="true">{{ item.icon }}</span>
      <small>{{ item.label }}</small>
    </button>
    <div class="sidebar-spacer" />
    <a class="sidebar-item sidebar-help" href="https://github.com" target="_blank" rel="noreferrer">
      <span aria-hidden="true">?</span><small>帮助</small>
    </a>
  </nav>
</template>
