<script setup lang="ts">
import { useId } from 'vue';
import AppIcon from '../common/AppIcon.vue';
export type ReportMode = 'comment' | 'user' | 'audit';
defineProps<{ modelValue: ReportMode; panelId: string }>();
const emit = defineEmits<{ 'update:modelValue': [value: ReportMode] }>();
const id = useId();
const modes = [
  { id: 'comment' as const, label: 'PR 评论草稿', icon: 'copy' },
  { id: 'user' as const, label: '详细审查报告', icon: 'report' },
  { id: 'audit' as const, label: '技术审计记录', icon: 'code' },
];
function move(event: KeyboardEvent, index: number) {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2
    : event.key === 'ArrowRight' ? (index + 1) % 3 : event.key === 'ArrowLeft' ? (index + 2) % 3 : -1;
  if (next < 0) return;
  event.preventDefault();
  emit('update:modelValue', modes[next]!.id);
  (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
}
</script>
<template>
  <div class="rd-shell-top">
    <div class="rd-tabs" role="tablist" aria-label="报告类型">
      <button v-for="(mode, index) in modes" :id="`${id}-${mode.id}`" :key="mode.id" type="button" role="tab"
        :aria-selected="modelValue === mode.id" :aria-controls="panelId" :tabindex="modelValue === mode.id ? 0 : -1"
        :class="{ active: modelValue === mode.id }" @click="emit('update:modelValue', mode.id)" @keydown="move($event, index)">
        <AppIcon :name="mode.icon" :size="16" />{{ mode.label }}
      </button>
    </div>
    <span class="rd-shell-caption">同一审查快照 · 不同阅读与交付场景</span>
  </div>
</template>
