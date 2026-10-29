<script setup lang="ts">
import type { AppPage } from '../../types/operations';
import { reviewPrimarySections, reviewSecondarySections, type ReviewSection } from '../../types/workspace';
import AppIcon from '../common/AppIcon.vue';
import brandSymbol from '../../assets/repoguardian-symbol-color.svg';
import brandSymbolWhite from '../../assets/repoguardian-symbol-white.svg';
import { usePreferences } from '../../composables/usePreferences';
const { theme } = usePreferences();
defineProps<{ modelValue: AppPage; section: ReviewSection; hasReview: boolean; hasPreview?: boolean; taskActive: boolean; mobileOpen: boolean; busy: boolean }>();
defineEmits<{ 'update:modelValue': [page: AppPage]; section: [value: ReviewSection]; create: []; close: [] }>();
</script>
<template>
  <aside id="workspace-sidebar" class="sidebar" :class="{ 'mobile-open': mobileOpen }" @keydown.esc="$emit('close')">
    <div class="brand"><div class="brand-icon"><img :src="theme === 'dark' ? brandSymbolWhite : brandSymbol" alt="" width="36" height="36" /></div><div class="brand-word"><strong>RepoGuardian</strong><small>Evidence-first review</small></div><button class="icon-btn mobile-close" aria-label="关闭导航" @click="$emit('close')"><AppIcon name="x" /></button></div>
    <nav class="side-scroll" aria-label="主导航">
      <div class="nav-heading">工作区</div>
      <button class="side-link" :class="{ active: modelValue === 'dashboard' && !hasReview && !hasPreview }" :disabled="taskActive || busy" @click="$emit('create')"><AppIcon name="plus" /><span class="nav-text">新建审查</span></button>
      <button v-if="hasPreview && !hasReview" class="side-link" :class="{ active: modelValue === 'dashboard' }" :aria-current="modelValue === 'dashboard' ? 'page' : undefined" @click="$emit('update:modelValue', 'dashboard')"><AppIcon name="branch" /><span class="nav-text">返回当前审查方案</span></button>
      <button v-for="item in ([['history','history','审查历史'],['settings','settings','系统设置']] as const)" :key="item[0]" class="side-link" :class="{ active: modelValue === item[0] }" :aria-current="modelValue === item[0] ? 'page' : undefined" @click="$emit('update:modelValue', item[0])"><AppIcon :name="item[1]" /><span class="nav-text">{{ item[2] }}</span></button>
      <div class="nav-divider" /><div class="nav-heading">当前 PR 审查</div>
      <button v-for="item in reviewPrimarySections" :key="item.id" class="side-link" :class="{ active: hasReview && modelValue === 'dashboard' && section === item.id }" :disabled="!hasReview" :aria-current="hasReview && modelValue === 'dashboard' && section === item.id ? 'page' : undefined" @click="$emit('section', item.id)"><AppIcon :name="item.icon" /><span class="nav-text">{{ item.label }}</span></button>
      <div class="nav-heading nav-subheading">补充证据</div>
      <button v-for="item in reviewSecondarySections" :key="item.id" class="side-link" :class="{ active: hasReview && modelValue === 'dashboard' && section === item.id }" :disabled="!hasReview" :aria-current="hasReview && modelValue === 'dashboard' && section === item.id ? 'page' : undefined" @click="$emit('section', item.id)"><AppIcon :name="item.icon" /><span class="nav-text">{{ item.label }}</span></button>
    </nav>
  </aside>
</template>
